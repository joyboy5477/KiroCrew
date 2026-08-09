"""Tests for the crew runtime — session, brief injection, nudge, watcher sweep.

Nothing here spawns a real session or touches the network: the dashboard state is a
fake that records what was asked of it, the provider client is patched, and every
store read/write is scoped to ``tmp_path``.

The coverage is weighted toward the failures that are SILENT, because a crew runs
with nobody watching:

  * **The length guard on brief injection.** A compaction summary that merely
    quotes the sentinel is the failure mode that matters: a sentinel-only check
    reads it as a hit and the crew spends the rest of the day running on a
    paraphrase of its own instructions, with no error anywhere. So the guard has a
    test of its own, and it is one of the two tests falsified below.
  * **Trust.** Granting it to an attended crew is an unattended-tool-execution
    bug; failing to re-establish it for an unattended one parks the crew in an
    approval prompt for two hours and then denies it.
  * **First observation.** A cold fingerprint must report NOTHING, or every
    gateway restart wakes every crew on every open item at once.
  * **Each of the six signals.** Missing one means an item stalls forever with no
    trace, which is exactly what the sweep exists to prevent.
"""

import contextlib
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

from kiro_crew.apps.builtins.issue_radar.backend import crew_runtime as cr
from kiro_crew.apps.builtins.issue_radar.backend import crew_store as cs
from kiro_crew.apps.builtins.issue_radar.backend import provider

OWNER, REPO = "o", "r"
_KEY = provider.key_from_parts(OWNER, REPO)


# ── fakes ───────────────────────────────────────────────────────────────────


class _FakeSlot:
    """Stand-in for _ChatSlot: records the prompt a turn would have run with."""

    def __init__(self, key: str = "crew-c_1", agent: str = "", model: str = "", workspace: str = ""):
        self.key = key
        self.title = ""
        self._titled = False
        self._trust = False
        self.agent = agent
        self.model = model
        self.workspace = workspace
        self.messages: list[dict[str, Any]] = []
        self.running = False
        self.prompts: list[str] = []

    def append(self, role: str, content: str, cls: str = "", **kw: Any) -> None:
        self.messages.append({"role": role, "content": content, "cls": cls})

    def enqueue_or_run_prompt(self, prompt: str, run_chat_coro: Any, state: Any) -> bool:
        self.prompts.append(prompt)
        self.append("user", prompt)
        return not self.running


class _FakeState:
    """Minimal DashboardState: slot registry plus the calls the runtime makes."""

    def __init__(self) -> None:
        self.slots: dict[str, _FakeSlot] = {}
        self.created: list[dict[str, Any]] = []
        self.pushes = 0

    def get_slot(self, key: str) -> _FakeSlot | None:
        return self.slots.get(key)

    def get_or_create_slot(
        self,
        name: str = "",
        agent: str = "",
        workspace: str = "default",
        model: str = "",
        app: str = "",
        **kw: Any,
    ) -> _FakeSlot:
        self.created.append(
            {"name": name, "agent": agent, "workspace": workspace, "model": model, "app": app}
        )
        slot = self.slots.get(name)
        if slot is None:
            slot = _FakeSlot(name, agent=agent, model=model, workspace=workspace)
            self.slots[name] = slot
        return slot

    def push_slots_update(self) -> None:
        self.pushes += 1

    def push_slot_title(self, key: str, title: str) -> None:
        self.pushes += 1


def _app(state: _FakeState | None) -> Any:
    return cast(Any, {"state": state})


def _crew(root, name="Andromeda", **spec) -> dict[str, Any]:
    return cs.create_crew(OWNER, REPO, {"name": name, **spec}, root)


def _item(root, crew_id, number, **patch) -> dict[str, Any]:
    return cs.upsert_work_item(OWNER, REPO, crew_id, number, patch, root)


# ── brief injection ─────────────────────────────────────────────────────────


class TestBriefInjection(unittest.TestCase):
    def test_brief_carries_the_sentinel(self):
        self.assertTrue(cr.brief_text().startswith(cr.BRIEF_SENTINEL))

    def test_injects_when_sentinel_absent(self):
        slot = _FakeSlot()
        slot.append("nudge", "[crew turn] Andromeda · o/r — advance one item")
        self.assertFalse(cr.brief_is_present(slot))

    def test_does_not_inject_when_brief_present(self):
        slot = _FakeSlot()
        slot.append("user", cr.brief_text() + "\n\n---\n\nnudge body")
        self.assertTrue(cr.brief_is_present(slot))

    def test_injects_when_only_a_short_quote_of_the_sentinel_is_present(self):
        """THE length guard.

        A compaction summary quotes the marker it saw. It contains the sentinel and
        it is far shorter than the brief, so it must NOT count as a hit — otherwise
        the crew keeps running on a summary of its own instructions.
        """
        slot = _FakeSlot()
        slot.append(
            "assistant",
            "Summary of earlier turns: the session opened with "
            f"{cr.BRIEF_SENTINEL} and a work list, then claimed #2201.",
        )
        self.assertFalse(cr.brief_is_present(slot))

    def test_a_padded_summary_still_needs_the_sentinel(self):
        """The guard is length AND sentinel, not length alone."""
        slot = _FakeSlot()
        slot.append("assistant", "x" * (len(cr.brief_text()) + 500))
        self.assertFalse(cr.brief_is_present(slot))

    def test_turn_prompt_carries_the_brief_only_on_a_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            crew = _crew(root)
            slot = _FakeSlot()
            first = cr.compose_turn_prompt(slot, OWNER, REPO, crew, root)
            self.assertIn(cr.BRIEF_SENTINEL, first)
            # The carrying message is brief + nudge, so it satisfies its own guard.
            slot.append("user", first)
            second = cr.compose_turn_prompt(slot, OWNER, REPO, crew, root)
            self.assertNotIn(cr.BRIEF_SENTINEL, second)
            self.assertIn("[crew turn]", second)


# ── nudge composition ───────────────────────────────────────────────────────


class TestNudge(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_nudge_carries_the_volatile_fields(self):
        crew = _crew(self.root, labels=["bug", "area:cli"], max_open=3, max_escalated=2)
        cid = crew["id"]
        _item(self.root, cid, 2201, phase="implementing", next="add the Windows branch")
        _item(self.root, cid, 2244, phase="escalated", next="waiting on a naming decision")
        nudge = cr.compose_nudge(cr.build_snapshot(OWNER, REPO, crew, self.root))

        self.assertIn("Andromeda", nudge)
        self.assertIn(f"{OWNER}/{REPO}", nudge)
        self.assertIn(cid, nudge)                       # crew id, not just the name
        self.assertIn("bug, area:cli", nudge)           # label scope
        self.assertIn("Open 1/3", nudge)                # escalated does NOT take a slot
        self.assertIn("escalated 1/2", nudge)
        self.assertIn("#2201 implementing", nudge)
        self.assertIn("add the Windows branch", nudge)  # the `next` of every open item
        self.assertIn("#2244 escalated", nudge)
        self.assertIn("waiting on a naming decision", nudge)
        for label in cr.CREW_LABELS:
            self.assertIn(label, nudge)

    def test_nudge_carries_the_never_block(self):
        crew = _crew(self.root)
        nudge = cr.compose_nudge(cr.build_snapshot(OWNER, REPO, crew, self.root))
        self.assertIn(cr.NEVER_BLOCK, nudge)
        for fragment in (
            "CI or gate configuration",
            "outside the `crew:` prefix",
            "Never push to main",
            "two worktrees",
            "without writing the ledger",
            "absolute path",
            "exit code",
        ):
            self.assertIn(fragment, nudge)

    def test_never_block_is_compressed(self):
        # ~80 words. It rides on EVERY turn, so its size is a running cost.
        self.assertLess(len(cr.NEVER_BLOCK.split()), 110)

    def test_item_without_a_next_says_so(self):
        crew = _crew(self.root)
        _item(self.root, crew["id"], 7, phase="claimed")
        nudge = cr.compose_nudge(cr.build_snapshot(OWNER, REPO, crew, self.root))
        self.assertIn("no next step recorded", nudge)

    def test_empty_label_scope_reads_as_pick_up_nothing(self):
        crew = _crew(self.root, labels=[])
        nudge = cr.compose_nudge(cr.build_snapshot(OWNER, REPO, crew, self.root))
        self.assertIn("pick up nothing", nudge)


# ── session launch / trust ──────────────────────────────────────────────────


class TestSession(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    async def test_session_key_agent_workspace_and_model_come_from_the_record(self):
        crew = _crew(self.root, agent="kirocrew", model="claude-opus-5")
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        self.assertEqual(slot.key, f"crew-{crew['id']}")
        created = state.created[-1]
        self.assertEqual(created["name"], f"crew-{crew['id']}")
        self.assertEqual(created["agent"], "kirocrew")
        self.assertEqual(created["app"], "issue-radar")
        # The record's model is passed EXPLICITLY, which is what overrides the
        # agent's own pin.
        self.assertEqual(created["model"], "claude-opus-5")

    async def test_title_is_locked_so_the_auto_titler_never_fires(self):
        crew = _crew(self.root)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        self.assertTrue(slot._titled)
        self.assertIn("Andromeda", slot.title)
        self.assertIn(f"{OWNER}/{REPO}", slot.title)

    async def test_trust_only_when_unattended(self):
        state = _FakeState()
        unattended = _crew(self.root, name="Whirlpool", unattended=True)
        attended = _crew(self.root, name="Draco", unattended=False)
        hot = await cr.ensure_crew_session(state, OWNER, REPO, unattended)
        cold = await cr.ensure_crew_session(state, OWNER, REPO, attended)
        self.assertTrue(hot._trust)
        self.assertFalse(cold._trust)

    async def test_trust_is_reestablished_every_cycle(self):
        """``_trust`` is in-memory only, so a restart drops it — the watchdog is
        what makes the grant restart-durable."""
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        slot._trust = False  # as a gateway restart leaves it
        await cr.watchdog_cycle(state, OWNER, REPO, [crew], self.root)
        self.assertTrue(slot._trust)

    async def test_trust_is_revoked_when_unattended_is_turned_off(self):
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        self.assertTrue(slot._trust)
        flipped = cs.update_crew(OWNER, REPO, crew["id"], {"unattended": False}, self.root)
        await cr.watchdog_cycle(state, OWNER, REPO, [flipped], self.root)
        self.assertFalse(slot._trust)

    async def test_watchdog_revokes_trust_for_a_paused_or_retired_crew(self):
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        paused = cs.update_crew(
            OWNER, REPO, crew["id"], {"paused_reason": "operator paused"}, self.root
        )
        self.assertFalse(cr.is_live(paused))
        await cr.watchdog_cycle(state, OWNER, REPO, [paused], self.root)
        self.assertFalse(slot._trust)

    async def test_wake_runs_a_turn_carrying_the_brief_and_the_nudge(self):
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        _item(self.root, crew["id"], 2201, phase="awaiting-ci", next="round 3")
        with mock.patch.dict(
            "sys.modules",
            {"kiro_crew.dashboard.chat_runner": mock.Mock(_run_chat=mock.Mock())},
        ):
            started = await cr.wake_crew(
                state, OWNER, REPO, crew, "#2201 ci-changed", self.root
            )
        self.assertTrue(started)
        self.assertEqual(len(slot.prompts), 1)
        prompt = slot.prompts[0]
        self.assertIn("[crew wake: #2201 ci-changed]", prompt)
        self.assertIn(cr.BRIEF_SENTINEL, prompt)     # first turn — brief injected
        self.assertIn("#2201 awaiting-ci", prompt)
        self.assertIn(cr.NEVER_BLOCK, prompt)

    async def test_wake_is_dropped_not_queued_while_the_crew_is_mid_turn(self):
        """A queued wake can carry the whole brief. Three busy sweeps would hand the
        crew three stacked copies of its own instructions, so a wake it cannot use
        is dropped — the refreshed loop message and the crew's own per-turn
        reconciliation both still cover the signal."""
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        slot.running = True
        with mock.patch.dict(
            "sys.modules",
            {"kiro_crew.dashboard.chat_runner": mock.Mock(_run_chat=mock.Mock())},
        ):
            started = await cr.wake_crew(state, OWNER, REPO, crew, "ci-changed", self.root)
        self.assertFalse(started)
        self.assertEqual(slot.prompts, [])

    async def test_wake_without_a_session_is_not_a_crash(self):
        crew = _crew(self.root)
        state = _FakeState()
        with mock.patch.object(cr, "_rehydrate", return_value=None):
            self.assertFalse(
                await cr.wake_crew(state, OWNER, REPO, crew, "signal", self.root)
            )


# ── unblock signal detection (pure) ─────────────────────────────────────────


class TestDetectUnblocks(unittest.TestCase):
    BASE = {
        "issue_comments": 2,
        "checks": "failure",
        "check_counts": {"failure": 1, "success": 40, "running": 0, "other": 2},
        "review_decision": "",
        "conflicted": False,
        "merged": False,
        "pr_comments": 3,
    }

    def _detect(self, **changes):
        return cr.detect_unblocks(dict(self.BASE), {**self.BASE, **changes})

    def test_first_observation_reports_nothing(self):
        # Cold start seeds the mark. Reporting here would wake every crew on every
        # open item the moment the gateway restarts.
        self.assertEqual(cr.detect_unblocks(None, dict(self.BASE)), [])
        self.assertEqual(cr.detect_unblocks({}, dict(self.BASE)), [])

    def test_no_change_reports_nothing(self):
        self.assertEqual(self._detect(), [])

    def test_requester_replied(self):
        self.assertEqual(self._detect(issue_comments=3), [cr.SIG_REPLY])

    def test_ci_state_changed(self):
        self.assertEqual(self._detect(checks="success"), [cr.SIG_CI])

    def test_ci_counts_changed_without_the_rollup_moving(self):
        counts = {"failure": 1, "success": 41, "running": 0, "other": 2}
        self.assertEqual(self._detect(check_counts=counts), [cr.SIG_CI])

    def test_unknown_ci_is_not_a_ci_change(self):
        # A failed enrichment call reports None. Treating unknown-vs-known as
        # movement would wake the crew every time the GraphQL leg flakes.
        self.assertEqual(self._detect(checks=None, check_counts=None), [])

    def test_review_approved_and_changes_requested(self):
        self.assertEqual(self._detect(review_decision="approved"), [cr.SIG_REVIEW])
        self.assertEqual(
            self._detect(review_decision="changes_requested"), [cr.SIG_REVIEW]
        )

    def test_a_withdrawn_verdict_is_not_a_signal(self):
        prev = {**self.BASE, "review_decision": "approved"}
        self.assertEqual(cr.detect_unblocks(prev, dict(self.BASE)), [])

    def test_merge_conflict_appeared(self):
        self.assertEqual(self._detect(conflicted=True), [cr.SIG_CONFLICT])

    def test_conflict_already_known_is_not_re_reported(self):
        prev = {**self.BASE, "conflicted": True}
        cur = {**self.BASE, "conflicted": True}
        self.assertEqual(cr.detect_unblocks(prev, cur), [])

    def test_pr_merged(self):
        self.assertEqual(self._detect(merged=True), [cr.SIG_MERGED])

    def test_post_merge_comment(self):
        prev = {**self.BASE, "merged": True}
        cur = {**self.BASE, "merged": True, "pr_comments": 4}
        self.assertEqual(cr.detect_unblocks(prev, cur), [cr.SIG_POST_MERGE])

    def test_uncomputed_mergeability_is_not_a_conflict(self):
        # GitHub answers mergeable: null on a cold read and computes it in the
        # background; truthiness would report every cold read as a conflict.
        self.assertFalse(cr._is_conflicted(None, "unknown"))
        self.assertTrue(cr._is_conflicted(False, "unknown"))
        self.assertTrue(cr._is_conflicted(None, "dirty"))

    def test_every_signal_has_a_detector(self):
        # Guards against a signal being added to the table and never wired up.
        seen = set()
        for changes in (
            {"issue_comments": 9},
            {"checks": "success"},
            {"review_decision": "approved"},
            {"conflicted": True},
            {"merged": True},
        ):
            seen.update(self._detect(**changes))
        prev = {**self.BASE, "merged": True}
        seen.update(cr.detect_unblocks(prev, {**prev, "pr_comments": 99}))
        self.assertEqual(seen, set(cr.UNBLOCK_SIGNALS))


# ── the sweep ───────────────────────────────────────────────────────────────


class _FakeClient:
    """The gh layer, stubbed. Counts calls so the sweep's API cost is assertable."""

    def __init__(self, issue=None, pr=None, timeline=None, enriched=None):
        self.issue = issue or {"comments": 0, "state": "open"}
        self.pr = pr or {}
        self.timeline = timeline or []
        self.enriched = enriched or []
        self.calls: list[str] = []

    def get_issue_detail(self, owner, repo, number, **kw):
        self.calls.append(f"issue:{number}")
        return dict(self.issue)

    def get_pr_detail(self, owner, repo, number, **kw):
        self.calls.append(f"pr:{number}")
        return dict(self.pr)

    def list_issue_timeline(self, owner, repo, number, **kw):
        self.calls.append(f"timeline:{number}")
        return list(self.timeline)

    def enrich_pulls_by_number(self, owner, repo, pulls, **kw):
        self.calls.append("enrich")
        return list(self.enriched)


class TestSweep(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    async def _sweep(self, client, state=None):
        with mock.patch.object(provider, "client_for", return_value=client), \
             mock.patch.object(cr.provider, "client_for", return_value=client), \
             mock.patch.object(cr, "wake_crew", new=mock.AsyncMock(return_value=True)) as wake:
            woken = await cr.sweep_repo(_app(state), _KEY, self.root)
        return woken, wake

    async def test_first_sweep_seeds_without_waking(self):
        crew = _crew(self.root, unattended=True)
        _item(self.root, crew["id"], 2201, phase="awaiting-ci")
        client = _FakeClient(issue={"comments": 2, "state": "open"})
        woken, wake = await self._sweep(client, _FakeState())
        self.assertEqual(woken, {})
        wake.assert_not_awaited()
        # The mark is stored, so the SECOND sweep has something to compare against.
        stored = cr.read_signals(OWNER, REPO, self.root)
        self.assertIn(f"{crew['id']}:2201", stored)

    async def test_second_sweep_wakes_the_owning_crew_on_a_reply(self):
        crew = _crew(self.root, unattended=True)
        _item(self.root, crew["id"], 2201, phase="awaiting-reply")
        client = _FakeClient(issue={"comments": 2, "state": "open"})
        await self._sweep(client, _FakeState())
        # Backdate the mark so the phase's recheck interval has elapsed.
        stored = cr.read_signals(OWNER, REPO, self.root)
        stored[f"{crew['id']}:2201"]["checked_at"] = 0
        cr.write_signals(OWNER, REPO, stored, self.root)

        client.issue = {"comments": 3, "state": "open"}
        woken, wake = await self._sweep(client, _FakeState())
        self.assertEqual(woken, {crew["id"]: [cr.SIG_REPLY]})
        wake.assert_awaited_once()
        self.assertIn("requester-replied", wake.await_args.args[4])

    async def test_selected_items_are_never_fetched(self):
        # Pre-claim and local only: there is nothing public to watch, and reading
        # it would cost an API call per crew per minute for every shortlisted issue.
        crew = _crew(self.root)
        _item(self.root, crew["id"], 42, phase="selected")
        client = _FakeClient()
        woken, _ = await self._sweep(client, _FakeState())
        self.assertEqual(woken, {})
        self.assertEqual(client.calls, [])

    async def test_a_retired_crew_is_not_swept(self):
        crew = _crew(self.root)
        _item(self.root, crew["id"], 2201, phase="awaiting-ci")
        cs.retire_crew(OWNER, REPO, crew["id"], self.root)
        client = _FakeClient()
        woken, _ = await self._sweep(client, _FakeState())
        self.assertEqual(woken, {})
        self.assertEqual(client.calls, [])

    async def test_api_cost_per_item_is_two_reads_plus_one_batched_enrichment(self):
        crew = _crew(self.root)
        cid = crew["id"]
        _item(self.root, cid, 2201, phase="awaiting-ci", pr_number=101)
        _item(self.root, cid, 2202, phase="awaiting-ci", pr_number=102)
        client = _FakeClient(
            issue={"comments": 1, "state": "open"},
            pr={"comments": 0, "updated_at": "t0", "merged": False, "mergeable": True},
            enriched=[
                {"number": 101, "checks_state": "success", "checks_counts": {}},
                {"number": 102, "checks_state": "success", "checks_counts": {}},
            ],
        )
        await self._sweep(client, _FakeState())
        # One issue read + one PR read per item, and ONE batched enrichment for the
        # whole repo (two GraphQL round-trips inside it) — not one per PR.
        self.assertEqual(client.calls.count("enrich"), 1)
        self.assertEqual(client.calls.count("issue:2201"), 1)
        self.assertEqual(client.calls.count("pr:101"), 1)
        self.assertEqual(len([c for c in client.calls if c.startswith("timeline")]), 2)

    async def test_review_read_is_skipped_when_the_pr_did_not_move(self):
        crew = _crew(self.root)
        _item(self.root, crew["id"], 2201, phase="awaiting-ci", pr_number=101)
        client = _FakeClient(
            issue={"comments": 1, "state": "open"},
            pr={"comments": 0, "updated_at": "t0", "merged": False, "mergeable": True},
            enriched=[{"number": 101, "checks_state": "success", "checks_counts": {}}],
        )
        await self._sweep(client, _FakeState())
        stored = cr.read_signals(OWNER, REPO, self.root)
        stored[f"{crew['id']}:2201"]["checked_at"] = 0
        cr.write_signals(OWNER, REPO, stored, self.root)
        client.calls.clear()
        await self._sweep(client, _FakeState())
        # updated_at unchanged -> the paginated timeline read is not paid again.
        self.assertNotIn("timeline:101", client.calls)

    async def test_review_verdict_is_read_when_the_pr_moved(self):
        crew = _crew(self.root, unattended=True)
        _item(self.root, crew["id"], 2201, phase="addressing-review", pr_number=101)
        client = _FakeClient(
            issue={"comments": 1, "state": "open"},
            pr={"comments": 0, "updated_at": "t0", "merged": False, "mergeable": True},
            enriched=[{"number": 101, "checks_state": "success", "checks_counts": {}}],
        )
        await self._sweep(client, _FakeState())
        stored = cr.read_signals(OWNER, REPO, self.root)
        stored[f"{crew['id']}:2201"]["checked_at"] = 0
        cr.write_signals(OWNER, REPO, stored, self.root)

        client.pr = {"comments": 0, "updated_at": "t1", "merged": False, "mergeable": True}
        client.timeline = [
            {"kind": "reviewed", "review_state": "APPROVED", "created_at": "t1"},
        ]
        woken, wake = await self._sweep(client, _FakeState())
        self.assertEqual(woken, {crew["id"]: [cr.SIG_REVIEW]})
        wake.assert_awaited_once()

    async def test_a_failed_item_read_leaves_its_mark_untouched(self):
        crew = _crew(self.root)
        _item(self.root, crew["id"], 2201, phase="awaiting-ci")
        client = _FakeClient()
        client.get_issue_detail = mock.Mock(side_effect=RuntimeError("gh exploded"))
        woken, _ = await self._sweep(client, _FakeState())
        self.assertEqual(woken, {})
        # No mark: the change (whatever it was) is still pending next cycle rather
        # than being silently consumed by the error.
        self.assertEqual(cr.read_signals(OWNER, REPO, self.root), {})

    async def test_recheck_cadence_is_phase_aware(self):
        self.assertLess(cr.RECHECK_SEC["awaiting-ci"], cr.RECHECK_SEC["awaiting-reply"])
        stored = {"checked_at": 1000.0}
        self.assertTrue(cr._is_due({"phase": "awaiting-ci"}, stored, 1000.0 + 61))
        self.assertFalse(cr._is_due({"phase": "awaiting-reply"}, stored, 1000.0 + 61))
        # A mark in the future (clock correction) must not park the item forever.
        self.assertTrue(cr._is_due({"phase": "awaiting-reply"}, stored, 900.0))

    async def test_two_signalling_items_wake_the_crew_once_with_both_reasons(self):
        crew = _crew(self.root, unattended=True)
        cid = crew["id"]
        _item(self.root, cid, 2201, phase="awaiting-reply")
        _item(self.root, cid, 2202, phase="awaiting-reply")
        client = _FakeClient(issue={"comments": 1, "state": "open"})
        await self._sweep(client, _FakeState())
        stored = cr.read_signals(OWNER, REPO, self.root)
        for k in stored:
            stored[k]["checked_at"] = 0
        cr.write_signals(OWNER, REPO, stored, self.root)

        client.issue = {"comments": 5, "state": "open"}
        woken, wake = await self._sweep(client, _FakeState())
        self.assertEqual(woken, {cid: [cr.SIG_REPLY, cr.SIG_REPLY]})
        # ONE turn, both reasons — the second call would have been dropped as
        # mid-turn, so the crew would only have heard about the first item.
        wake.assert_awaited_once()
        reason = wake.await_args.args[4]
        self.assertIn("#2201", reason)
        self.assertIn("#2202", reason)

    async def test_marks_for_finished_items_are_pruned(self):
        crew = _crew(self.root, unattended=True)
        cid = crew["id"]
        _item(self.root, cid, 2201, phase="awaiting-ci")
        client = _FakeClient(issue={"comments": 1, "state": "open"})
        await self._sweep(client, _FakeState())
        self.assertIn(f"{cid}:2201", cr.read_signals(OWNER, REPO, self.root))
        # Resolved items leave the open set, so their fingerprints must go too —
        # otherwise a long-lived crew rewrites every issue it ever closed, every
        # minute, forever.
        _item(self.root, cid, 2201, phase="resolved")
        await self._sweep(client, _FakeState())
        self.assertEqual(cr.read_signals(OWNER, REPO, self.root), {})

    async def test_schema_mismatch_is_a_cache_miss(self):
        cr.signals_path(OWNER, REPO, self.root).write_text(
            '{"schema": 999, "items": {"c_x:1": {"fp": {}}}}'
        )
        self.assertEqual(cr.read_signals(OWNER, REPO, self.root), {})


# ── how the sweep is gated in the poll loop ─────────────────────────────────


class TestWatchGating(unittest.IsolatedAsyncioTestCase):
    """The two gates in ``watch.py`` and the difference between them."""

    def _watch(self):
        from kiro_crew.apps.builtins.issue_radar.backend import watch

        watch._crews_suspended = False
        return watch

    async def test_sweep_does_not_inherit_the_notify_preference(self):
        watch = self._watch()
        entries = [{"owner": OWNER, "repo": REPO, "provider": "github", "host": "github.com"}]
        with contextlib.ExitStack() as stack:
            use = stack.enter_context
            use(mock.patch.object(watch, "is_app_enabled", return_value=True))
            use(mock.patch.object(watch.store, "list_connected_repos", return_value=entries))
            use(
                mock.patch.object(
                    watch.store,
                    "read_repo_settings",
                    return_value={"notify_on_new_issue": False},
                )
            )
            poll = use(mock.patch.object(watch, "_poll_repo", new=mock.AsyncMock()))
            sweep = use(
                mock.patch.object(
                    watch.crew_runtime, "sweep_repo", new=mock.AsyncMock(return_value={})
                )
            )
            await watch._poll_once(_app(_FakeState()))
        # Muting the bell must not stop a crew reconciling its own pull requests.
        poll.assert_not_awaited()
        sweep.assert_awaited_once()

    async def test_a_failing_new_issue_poll_still_runs_the_sweep(self):
        watch = self._watch()
        entries = [{"owner": OWNER, "repo": REPO}]
        with contextlib.ExitStack() as stack:
            use = stack.enter_context
            use(mock.patch.object(watch, "is_app_enabled", return_value=True))
            use(mock.patch.object(watch.store, "list_connected_repos", return_value=entries))
            use(
                mock.patch.object(
                    watch.store,
                    "read_repo_settings",
                    return_value={"notify_on_new_issue": True},
                )
            )
            use(
                mock.patch.object(
                    watch, "_poll_repo", new=mock.AsyncMock(side_effect=RuntimeError("gh down"))
                )
            )
            sweep = use(
                mock.patch.object(
                    watch.crew_runtime, "sweep_repo", new=mock.AsyncMock(return_value={})
                )
            )
            await watch._poll_once(_app(_FakeState()))
        sweep.assert_awaited_once()

    async def test_disabling_the_app_suspends_the_crews(self):
        watch = self._watch()
        with contextlib.ExitStack() as stack:
            use = stack.enter_context
            use(mock.patch.object(watch, "is_app_enabled", return_value=False))
            suspend = use(
                mock.patch.object(
                    watch.crew_runtime, "suspend_crews", new=mock.AsyncMock(return_value=0)
                )
            )
            repos = use(mock.patch.object(watch.store, "list_connected_repos"))
            await watch._poll_once(_app(_FakeState()))
            await watch._poll_once(_app(_FakeState()))
        # A disabled app stays silent — no config walk — and suspends only ONCE,
        # because nothing re-establishes trust while the app is off.
        repos.assert_not_called()
        suspend.assert_awaited_once()

    async def test_suspension_clears_trust_on_resident_crew_slots(self):
        state = _FakeState()
        slot = _FakeSlot("crew-c_abc")
        slot._app = "issue-radar"
        slot._trust = True
        other = _FakeSlot("chat-1")
        other._app = ""
        other._trust = True
        state._slots = {"crew-c_abc": slot, "chat-1": other}
        cleared = await cr.suspend_crews(state)
        self.assertEqual(cleared, 1)
        self.assertFalse(slot._trust)
        self.assertTrue(other._trust)  # a user's own session is not ours to touch


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
