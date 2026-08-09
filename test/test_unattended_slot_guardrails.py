"""Phase 0 guardrails for unattended, app-owned chat slots.

Three defects, each of which makes a fleet of unattended worker slots unusable.
Every test here is written to go RED if its own fix is reverted.

FIX 1 — the dashboard approval path parked for two hours.
    ``chat_runner._run_chat`` waited on its OWN per-slot approval future with a
    hardcoded ``timeout=7200.0``, so it never reached the deny-fast branch that
    ``DashboardState.request_approval`` already had. An unattended worker that
    tripped one untrusted tool held its slot for 2h and then denied anyway.

FIX 2 — nothing capped concurrency.
    Chat slots and concurrent turns were both uncapped; the only real ceiling
    was a 4-wide semaphore on agent cold starts plus host memory.

FIX 3 — idle cleanup permanently destroyed an autonudge loop.
    ``api_chat_slots_cleanup`` marked an idle slot closed; the nudge fire path
    then rehydrated WITHOUT ``adopt_closed=True``, could not reach a closed
    slot, and REMOVED the loop — terminally. An unattended worker is idle by
    nature between cycles, so the 3-day heuristic shot the longest-lived loops.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard.state import DashboardState

# ── FIX 1 ────────────────────────────────────────────────────────────────────


class TestUnattendedApprovalWindow:
    def test_unattended_app_slot_gets_the_deny_fast_window(self, tmp_path) -> None:
        """The named FIX 1 test. Red if either half of the fix is reverted.

        Half one is ``DashboardState.approval_timeout_for`` (the decision, using
        the same two constants ``request_approval`` uses so the windows cannot
        drift). Half two is the runner call site actually asking for it instead
        of hardcoding a literal.
        """
        state = _make_state(tmp_path)

        worker = state.get_or_create_slot("worker-1", app="issue-radar")
        human = state.get_or_create_slot("chat-1-1785")

        assert worker.unattended is True
        assert human.unattended is False

        assert state.approval_timeout_for(worker) == float(
            DashboardState._BACKGROUND_APPROVAL_TIMEOUT_SECS
        )
        # Interactive behaviour is unchanged: a human session keeps the 2h window.
        assert state.approval_timeout_for(human) == float(DashboardState._APPROVAL_TIMEOUT)
        assert state.approval_timeout_for(worker) < state.approval_timeout_for(human)

        # …and the runner must USE it. Without this assertion the fix could be
        # reverted at the call site (back to a hardcoded 7200.0) while the
        # method above still answered correctly, and nothing would fail.
        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner._run_chat)
        assert "state.approval_timeout_for(slot)" in src, (
            "the runner's approval await must take its window from DashboardState"
        )
        assert "timeout=_approval_window" in src
        assert "timeout=7200.0" not in src, "the hardcoded 2h window is back"

    def test_a_human_typing_into_an_app_tab_restores_the_full_window(self, tmp_path) -> None:
        """``_human_seen`` is the escape hatch, and only a dashboard user sets it."""
        state = _make_state(tmp_path)
        worker = state.get_or_create_slot("worker-2", app="issue-radar")
        assert state.approval_timeout_for(worker) == 180.0

        worker._human_seen = True  # what the api_chat dashboard-user branch does

        assert worker.unattended is False
        assert state.approval_timeout_for(worker) == float(DashboardState._APPROVAL_TIMEOUT)

    def test_only_the_dashboard_user_branch_marks_attendance(self) -> None:
        """An app token must not be able to forge attendance for its own worker.

        Pins the placement of the write: inside ``api_chat``'s ``else`` (empty
        ``request_app``), not in a branch an app-scoped caller can reach.
        """
        from kiro_crew.dashboard import chat_handlers

        src = inspect.getsource(chat_handlers.api_chat)
        writes = [ln for ln in src.splitlines() if "_human_seen = True" in ln]
        assert len(writes) == 1, "attendance must be recorded in exactly one place"
        gate = src.index("request_app = request.get")
        assert src.index("_human_seen = True") > gate, (
            "attendance must be recorded only after the app-ownership gate"
        )

    def test_trust_is_not_the_detector(self, tmp_path) -> None:
        """Why ``_app`` and not ``_trust``: trust is False wherever this is read.

        The runner auto-approves and ``continue``s while trust holds, so a tool
        only reaches the interactive wait once trust is absent — and trust is
        in-memory, so a restart clears it on every app worker. A trust-based
        detector reads False in exactly the case it exists to detect, which is
        why the predicate must not consult it.
        """
        state = _make_state(tmp_path)
        worker = state.get_or_create_slot("worker-3", app="issue-radar")
        assert worker._trust is False  # the state every unattended wait is in
        assert worker.unattended is True

        worker._trust = True
        assert worker.unattended is True, "trust must not change the window decision"


# ── FIX 2 ────────────────────────────────────────────────────────────────────


class TestBackgroundTurnCap:
    @pytest.mark.asyncio
    async def test_cap_queues_the_extra_unattended_turn(self, tmp_path) -> None:
        """The named FIX 2 test: at the cap a turn QUEUES and the wait is visible.

        Queue rather than reject: a rejected crew turn loses the issue it was
        working; a queued one only starts late.
        """
        state = _make_state(tmp_path)
        state.effective_max_background_turns = lambda: 1  # type: ignore[method-assign]

        w1 = state.get_or_create_slot("w1", app="issue-radar")
        w2 = state.get_or_create_slot("w2", app="issue-radar")

        started = asyncio.Event()
        release = asyncio.Event()
        order: list[str] = []

        async def _first() -> None:
            order.append("first-start")
            started.set()
            await release.wait()
            order.append("first-end")

        async def _second() -> None:
            order.append("second-start")

        t1 = asyncio.ensure_future(state.run_background_turn(w1, _first()))
        await started.wait()
        t2 = asyncio.ensure_future(state.run_background_turn(w2, _second()))
        await asyncio.sleep(0)  # let t2 reach the semaphore and block

        stats = state.background_turn_stats()
        assert stats["cap"] == 1
        assert stats["running"] == 1
        assert stats["waiting"] == 1, "a turn held at the cap must be observable"
        assert order == ["first-start"], "the second turn ran despite the cap"

        release.set()
        await asyncio.gather(t1, t2)

        assert order == ["first-start", "first-end", "second-start"]
        assert state.background_turn_stats() == {"cap": 1, "running": 0, "waiting": 0}

    @pytest.mark.asyncio
    async def test_attended_turns_bypass_the_cap_entirely(self, tmp_path) -> None:
        """Interactive turns must not queue behind a busy fleet."""
        state = _make_state(tmp_path)
        state.effective_max_background_turns = lambda: 1  # type: ignore[method-assign]

        worker = state.get_or_create_slot("w1", app="issue-radar")
        human = state.get_or_create_slot("chat-1-1785")

        started = asyncio.Event()
        release = asyncio.Event()

        async def _held() -> None:
            started.set()
            await release.wait()

        async def _human_turn() -> str:
            return "ran"

        t1 = asyncio.ensure_future(state.run_background_turn(worker, _held()))
        await started.wait()

        # Cap is full, yet the human turn completes immediately.
        assert await state.run_background_turn(human, _human_turn()) == "ran"
        assert state.background_turn_stats()["waiting"] == 0

        release.set()
        await t1

    def test_config_can_widen_the_cap_but_never_unbound_it(self, tmp_path, monkeypatch) -> None:
        """Same clamp shape as code_review_sage's MAX_CONCURRENT_CEIL."""
        state = _make_state(tmp_path)
        raw: dict = {}
        monkeypatch.setattr("kiro_crew.config.loader._raw_config", lambda: raw)

        assert state.effective_max_background_turns() == DashboardState.MAX_BACKGROUND_TURNS

        raw["dashboard"] = {"max_background_turns": 9}
        assert state.effective_max_background_turns() == 9

        raw["dashboard"] = {"max_background_turns": 10_000}
        assert (
            state.effective_max_background_turns() == DashboardState.MAX_BACKGROUND_TURNS_CEIL
        ), "the cap must never be configurable above its ceiling"

        raw["dashboard"] = {"max_background_turns": 0}
        assert state.effective_max_background_turns() == 1, "the cap cannot be disabled"

        raw["dashboard"] = {"max_background_turns": "nonsense"}
        assert state.effective_max_background_turns() == DashboardState.MAX_BACKGROUND_TURNS

    @pytest.mark.asyncio
    async def test_a_cancelled_queued_turn_does_not_leak_its_coroutine(self, tmp_path) -> None:
        """Cancelled while queued: the turn never ran, so close its coroutine."""
        state = _make_state(tmp_path)
        state.effective_max_background_turns = lambda: 1  # type: ignore[method-assign]
        w1 = state.get_or_create_slot("w1", app="issue-radar")
        w2 = state.get_or_create_slot("w2", app="issue-radar")

        started = asyncio.Event()
        release = asyncio.Event()
        ran = False

        async def _held() -> None:
            started.set()
            await release.wait()

        async def _never() -> None:
            nonlocal ran
            ran = True

        t1 = asyncio.ensure_future(state.run_background_turn(w1, _held()))
        await started.wait()
        queued = _never()
        t2 = asyncio.ensure_future(state.run_background_turn(w2, queued))
        await asyncio.sleep(0)
        t2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t2

        assert ran is False
        assert queued.cr_running is False and queued.cr_frame is None, "coroutine left open"
        assert state.background_turn_stats()["waiting"] == 0

        release.set()
        await t1

    @pytest.mark.asyncio
    async def test_a_queued_turn_that_never_gets_a_permit_fails_with_the_real_reason(
        self, tmp_path
    ) -> None:
        """The queue wait is bounded, and names itself when it expires.

        The wait happens inside the coroutine ``spawn_guarded_turn`` already
        bounds at the 7200s turn ceiling, so an unbounded wait would eventually
        be killed as "turn exceeded the ceiling" — true, but the wrong cause.
        """
        state = _make_state(tmp_path)
        state.effective_max_background_turns = lambda: 1  # type: ignore[method-assign]
        state._BACKGROUND_QUEUE_WAIT_SECS = 0.05  # type: ignore[misc]
        w1 = state.get_or_create_slot("w1", app="issue-radar")
        w2 = state.get_or_create_slot("w2", app="issue-radar")

        started = asyncio.Event()
        release = asyncio.Event()
        ran = False

        async def _held() -> None:
            started.set()
            await release.wait()

        async def _never() -> None:
            nonlocal ran
            ran = True

        t1 = asyncio.ensure_future(state.run_background_turn(w1, _held()))
        await started.wait()

        with pytest.raises(TimeoutError, match="background-turn cap"):
            await state.run_background_turn(w2, _never())

        assert ran is False
        assert state.background_turn_stats()["waiting"] == 0
        release.set()
        await t1

    def test_the_cap_is_published_in_the_status_payload(self, tmp_path) -> None:
        """The wait has to be readable from outside the process."""
        state = _make_state(tmp_path)
        stats = state.background_turn_stats()
        assert set(stats) == {"cap", "running", "waiting"}
        assert "background_turns" in inspect.getsource(DashboardState.status_snapshot)

    def test_both_unattended_dispatch_sites_go_through_the_cap(self) -> None:
        """A new dispatch site that skips the gate reintroduces the uncapped fleet."""
        from kiro_crew.dashboard import chat_handlers
        from kiro_crew.slack import gateway as gw

        assert "run_background_turn" in inspect.getsource(chat_handlers.api_chat)
        assert "run_background_turn" in inspect.getsource(
            gw.GatewayOrchestrator._fire_dashboard_nudge
        )


# ── FIX 3 ────────────────────────────────────────────────────────────────────


def _fake_autonudge(loops: list) -> MagicMock:
    svc = MagicMock()
    svc.list_all = MagicMock(return_value=loops)
    return svc


class _Loop:
    """Stand-in carrying only the fields the two call sites read."""

    def __init__(self, slot_key: str, *, active: bool = True, loop_id: str = "loop-1") -> None:
        self.id = loop_id
        self.slot_key = slot_key
        self.active = active
        self.message = "check CI"
        self.idle_secs = 300
        self.max_cycles = 24
        self.cycle_count = 3
        self.stop_sentinel_path = ""


def _bg_slot(key: str) -> MagicMock:
    slot = MagicMock()
    slot.key = key
    slot.running = False
    return slot


def _closing_spawn():
    """``spawn_guarded_turn`` stand-in that closes the coroutine it is handed."""

    def _spawn(state, slot, coro, **kwargs):
        coro.close()
        return MagicMock(name="turn-task")

    return _spawn


def _nudge_orchestrator():
    """Minimal GatewayOrchestrator for the dashboard nudge fire path."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.slack import gateway as gw

    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
        orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    orch.dashboard_state = SimpleNamespace(
        get_slot=MagicMock(return_value=None),
        push_slots_update=MagicMock(),
        _background_tasks=set(),
        run_background_turn=MagicMock(side_effect=lambda _slot, coro: coro),
    )
    orch.autonudge_svc = MagicMock()
    orch.autonudge_svc.remove = AsyncMock()
    orch._session_tasks = {}
    return orch


class TestIdleCleanupSparesArmedLoops:
    @pytest.mark.asyncio
    async def test_idle_cleanup_spares_a_slot_with_an_armed_loop(
        self, tmp_path, monkeypatch
    ) -> None:
        """The named FIX 3 test. Red if either half of the fix is reverted.

        Half one: cleanup must skip a slot owning an armed loop, so the loop's
        session is never marked closed in the first place. Half two: the fire
        path must adopt a session that IS closed, so a slot archived by any
        other automatic closer (or before this landed) is still reachable
        instead of retiring the loop terminally.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()

        babysat = state.get_or_create_slot("worker-1", app="issue-radar")
        babysat.append("user", "watching CI", ts=old_ts)
        babysat.drain()

        abandoned = state.get_or_create_slot("worker-2", app="issue-radar")
        abandoned.append("user", "nothing armed here", ts=old_ts)
        abandoned.drain()

        monkeypatch.setattr(
            "kiro_crew.autonudge.get_instance",
            lambda: _fake_autonudge([_Loop("worker-1")]),
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup", json={"max_inactive_days": 3}
            )
            data = await resp.json()

        assert data["keys"] == ["worker-2"], "cleanup archived a slot owning an armed loop"
        assert "worker-1" in state._slots
        assert "worker-2" not in state._slots

        # Half two: the fire path must actually REACH a session that was closed.
        # Asserted behaviourally, not by substring — the explanatory comment in
        # that function contains the words "adopt_closed=True" and would satisfy
        # a source scan even with the argument deleted.
        from kiro_crew.slack import gateway as gw

        orch = _nudge_orchestrator()
        rehydrate = AsyncMock(return_value=_bg_slot("worker-1"))
        with (
            patch.object(gw, "rehydrate_slot_from_history_async", new=rehydrate),
            patch.object(gw, "spawn_guarded_turn", _closing_spawn()),
            patch("kiro_crew.dashboard.chat._run_chat", new=AsyncMock()),
        ):
            assert await orch._fire_dashboard_nudge(_Loop("worker-1")) is True
        assert rehydrate.await_args.kwargs.get("adopt_closed") is True, (
            "the nudge fire path must reach a session that idle cleanup closed"
        )

    @pytest.mark.asyncio
    async def test_an_inactive_loop_does_not_pin_a_dead_slot_forever(
        self, tmp_path, monkeypatch
    ) -> None:
        """Only ARMED loops are protected — a paused one must not leak slots."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
        slot = state.get_or_create_slot("worker-1", app="issue-radar")
        slot.append("user", "paused loop", ts=old_ts)
        slot.drain()

        monkeypatch.setattr(
            "kiro_crew.autonudge.get_instance",
            lambda: _fake_autonudge([_Loop("worker-1", active=False)]),
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup", json={"max_inactive_days": 3}
            )
            data = await resp.json()

        assert data["keys"] == ["worker-1"]

    @pytest.mark.asyncio
    async def test_an_unreadable_registry_archives_nothing(self, tmp_path, monkeypatch) -> None:
        """Fail CLOSED: not knowing which slots are protected must not destroy one."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
        slot = state.get_or_create_slot("worker-1", app="issue-radar")
        slot.append("user", "stale", ts=old_ts)
        slot.drain()

        def _boom():
            raise RuntimeError("autonudge.json unreadable")

        monkeypatch.setattr("kiro_crew.autonudge.get_instance", _boom)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup", json={"max_inactive_days": 3}
            )
            data = await resp.json()

        assert data["archived"] == 0
        assert data["skipped"] == "autonudge_unknown"
        assert "worker-1" in state._slots

    @pytest.mark.asyncio
    async def test_the_users_close_still_retires_the_loop(self, tmp_path, monkeypatch) -> None:
        """"Respect the close" survives adopt_closed=True.

        The rule used to be an emergent property of the fire path's rehydrate
        miss. Now that the fire path adopts a closed session, the ✕ handler has
        to retire the loop itself — otherwise a dismissed tab would be
        resurrected by its own loop on the next cycle.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("chat-1-1785")

        removed: list[str] = []
        svc = MagicMock()
        svc.get_by_slot = MagicMock(return_value=_Loop("chat-1-1785", loop_id="loop-9"))

        async def _remove(loop_id: str) -> None:
            removed.append(loop_id)

        svc.remove = _remove
        monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: svc)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/chat/slots/chat-1-1785")
            assert resp.status == 200

        assert removed == ["loop-9"], "the user's ✕ must retire the slot's nudge loop"
