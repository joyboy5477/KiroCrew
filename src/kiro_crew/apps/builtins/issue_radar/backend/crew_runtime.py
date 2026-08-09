"""Crew runtime — the session a crew lives in, the brief it carries, the nudge that
drives every turn, and the zero-LLM change detector that wakes it.

Four things live here and nothing else:

1. **Session launch/attach** (:func:`ensure_crew_session`, :func:`launch_crew`).
   A crew is an app-owned dashboard slot keyed ``crew-<id>``, which is the only
   worker shape without a wall-clock ceiling shorter than one issue's lifecycle.
   ``auto_research``'s campaign worker is the working precedent and is copied
   deliberately, including the parts that look redundant: the explicit title with
   ``_titled = True`` (the loop's messages never trigger the auto-titler), and
   re-establishing ``slot._trust`` from the watchdog every cycle because ``_trust``
   is in-memory only and a gateway restart drops it.

2. **Brief injection by presence check** (:func:`brief_is_present`). Not a
   schedule, not a turn counter — one rule that covers session start,
   post-compaction, gateway restart and any truncation mechanism nobody has
   written yet.

3. **Nudge composition** (:func:`compose_nudge`). A volatile snapshot the crew
   must not guess at, plus the brief's ``Never`` list compressed to ~80 words.

4. **The sweep** (:func:`sweep_repo`), driven from ``watch.py``'s poll loop — the
   only always-on loop in this app. Zero LLM, zero credits: it compares the six
   unblock signals against a stored fingerprint and wakes the owning crew when one
   moves.

Nothing here writes to the forge, and nothing here writes a work item: a change
detector that touched ``last_progress_at`` would renew the very claim TTL the
protocol measures from it. Its own state lives in ``crew-signals.json``, beside
the crews dir rather than inside it — ``crew_store.list_crews`` globs ``*.json``
there, so a state file dropped in that directory would read back as a phantom crew.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from functools import partial
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write

from . import crew_store, provider, store

try:  # the autonudge service is feature-flagged; the runtime degrades without it
    from kiro_crew.autonudge import get_instance as _autonudge_instance
except ImportError:  # pragma: no cover - defensive
    _autonudge_instance = None  # type: ignore[assignment]

try:
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    _HAS_SECURITY = True
except ImportError:  # pragma: no cover - defensive
    _HAS_SECURITY = False

logger = logging.getLogger("kirocrew.app.issue-radar")

APP_NAME = "issue-radar"

#: Crews get a turn on this idle gap when the sweep has nothing to report. The
#: watcher is the real scheduler (it fires on an actual signal); this is the
#: fallback clock that lets an idle crew pick up NEW work, so it is deliberately
#: slow. ``autonudge`` clamps it to [15s, 24h].
DEFAULT_IDLE_SECS = 300


# ── the brief ───────────────────────────────────────────────────────────────

#: First line of ``crew_brief.md``. The injection rule keys on this string.
BRIEF_SENTINEL = "<!-- kirocrew-crew-brief v1 -->"

_BRIEF_PATH = Path(__file__).with_name("crew_brief.md")
_brief_cache: str | None = None


def brief_text() -> str:
    """The brief, read once per process.

    A missing file is returned as an empty string rather than raising: a crew with
    no brief is a bad crew, but a crash in the always-on poll loop is worse.
    """
    global _brief_cache
    if _brief_cache is None:
        try:
            _brief_cache = _BRIEF_PATH.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - defensive
            logger.warning("crew brief unreadable at %s", _BRIEF_PATH, exc_info=True)
            _brief_cache = ""
    return _brief_cache


def brief_is_present(slot: Any) -> bool:
    """Whether this session still carries the brief.

    TWO conditions, and the second one is the whole point: a message must contain
    the sentinel AND be at least as long as the brief itself. A compaction summary
    routinely quotes a marker it saw ("the session opened with
    ``<!-- kirocrew-crew-brief v1 -->`` and a work list…"), and a sentinel-only
    check would read that as a hit and leave the crew running for the rest of the
    day on a paraphrase of its own instructions. The carrying message is always
    brief + nudge, so it is strictly longer than the brief; nothing that merely
    mentions the sentinel can be.

    One rule, four situations: session start, post-compaction, gateway restart,
    and whatever truncates a window next. No heuristic and no schedule to keep in
    sync with the truncation mechanism.

    COST, measured on this install: context runs ~0.154 credits per 1k tokens on
    claude-opus-5 and the brief is ~3.3k tokens, i.e. ~0.5 credits to inject.
    Anything appended to ``slot.messages`` ACCUMULATES — it is re-sent as context
    on every later turn — so re-sending the brief on all ~80 turns of a crew's day
    costs ~1650 credits/day/crew (80 turns × the growing prefix), against a handful
    of injections for the presence check. That is the entire reason this is a
    presence check and not "every turn" or "every N turns".
    """
    brief = brief_text()
    if not brief:
        return True  # nothing to inject; never loop on a missing file
    for msg in getattr(slot, "messages", None) or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, str) or BRIEF_SENTINEL not in content:
            continue
        if len(content) >= len(brief):
            return True
    return False


# ── the nudge ───────────────────────────────────────────────────────────────

#: The brief's ``Never`` list, compressed. Repeated on EVERY turn, unlike the
#: brief. It is cheap (~80 words) and it buys back authority the brief does not
#: have: an injected brief arrives as a USER message, not a system prompt, so it
#: is the weakest kind of instruction in the window and the furthest from the
#: turn's actual work. Prohibitions kept adjacent to the instruction are the
#: version that holds.
NEVER_BLOCK = (
    "Never: modify CI or gate configuration — `.github/` or whatever this repo "
    "uses, plus any rule file those gates read. They judge you. "
    "Never write a label outside the `crew:` prefix. "
    "Never push to main or whichever branch this repo defaults to, and never "
    "merge a PR yourself. Never edit another crew's "
    "claim comment. Never hold uncommitted changes in two worktrees. Never end a "
    "turn without writing the ledger. Never put an absolute path, a host name or "
    "anything else about this machine into a progress line — progress lines go "
    "public. Never report a gate as passing when you have not seen its exit code."
)

#: The only labels a crew may write. Named in the nudge as well as the brief
#: because it is the prohibition with a public blast radius on someone else's issue.
CREW_LABELS = ("crew: in progress", "crew: needs decision", "crew: awaiting reply")


def build_snapshot(
    owner: str, repo: str, crew: dict[str, Any], root: Path | None = None
) -> dict[str, Any]:
    """The volatile facts a crew must never guess — read from the store, no API calls."""
    crew_id = str(crew.get("id") or "")
    items = crew_store.list_work_items(owner, repo, crew_id, root, open_only=True)
    return {
        "name": crew.get("name") or crew_id,
        "id": crew_id,
        "owner": owner,
        "repo": repo,
        "labels": list(crew.get("labels") or []),
        "open_count": crew_store.open_slot_count(owner, repo, crew_id, root),
        "max_open": int(crew.get("max_open") or 0),
        "escalated_count": crew_store.escalated_count(owner, repo, crew_id, root),
        "max_escalated": int(crew.get("max_escalated") or 0),
        "items": [
            {
                "number": it.get("number"),
                "phase": it.get("phase") or "",
                "next": (it.get("next") or "").strip(),
                "pr_number": it.get("pr_number"),
            }
            for it in items
        ],
    }


def compose_nudge(snapshot: dict[str, Any]) -> str:
    """The per-turn message: a volatile snapshot (~120 words) then the Never block.

    Everything in the first part can change between two turns of the same session —
    the crew may have been renamed, re-scoped, re-limited, or had an item picked up
    by a human — which is why it is re-sent every turn instead of living in the
    brief. The brief says "your name, your repository, your label scope and your
    limits arrive in the nudge"; this is that promise.
    """
    scope = ", ".join(snapshot.get("labels") or []) or "(none — pick up nothing)"
    lines = [
        f"[crew turn] {snapshot.get('name')} · {snapshot.get('owner')}/"
        f"{snapshot.get('repo')} · id {snapshot.get('id')}",
        f"Label scope: {scope}.",
        "Writable labels: "
        + ", ".join(f"`{lab}`" for lab in CREW_LABELS)
        + " — no others, ever.",
        f"Open {snapshot.get('open_count')}/{snapshot.get('max_open')} · "
        f"escalated {snapshot.get('escalated_count')}/{snapshot.get('max_escalated')}",
    ]
    items = snapshot.get("items") or []
    if items:
        lines.append("Open work items:")
        for it in items:
            pr = f" (PR #{it['pr_number']})" if it.get("pr_number") else ""
            nxt = it.get("next") or "no next step recorded — decide one and record it"
            lines.append(f"- #{it.get('number')} {it.get('phase')}{pr} — next: {nxt}")
    else:
        lines.append("Open work items: none. Pick up new work if you are under your limit.")
    lines.append(
        "Read the ledger first, reconcile every open item against the six unblock "
        "signals, advance ONE item, and write the ledger before the turn ends."
    )
    return "\n".join(lines) + "\n\n" + NEVER_BLOCK


def compose_turn_prompt(
    slot: Any, owner: str, repo: str, crew: dict[str, Any], root: Path | None = None
) -> str:
    """The full prompt for the next turn: the brief when it is missing, then the nudge.

    Injection rides on the prompt rather than being appended to ``slot.messages``
    on its own, because an appended message is transcript only — the agent process
    holds its own context and sees a prompt. One consequence worth knowing: the
    message that carries the brief IS the nudge message, which is what makes the
    length guard in :func:`brief_is_present` sound.
    """
    nudge = compose_nudge(build_snapshot(owner, repo, crew, root))
    if brief_is_present(slot):
        return nudge
    return brief_text() + "\n\n---\n\n" + nudge


# ── session launch / attach ─────────────────────────────────────────────────


def stop_sentinel_path(
    owner: str, repo: str, crew_id: str, root: Path | None = None
) -> Path:
    """Kill switch for the crew's loop. Lives in the crew's own item dir, which
    ``list_work_items`` globs for ``*.json`` only — so this file is invisible to it."""
    d = crew_store.crews_dir(owner, repo, root) / crew_id
    d.mkdir(parents=True, exist_ok=True)
    return d / "STOP"


def _slot_title(owner: str, repo: str, crew: dict[str, Any], slot_key: str) -> str:
    """A human title for the worker slot, redacted.

    The crew name is FREE TEXT in the create dialog, and a title is persisted and
    broadcast, so it gets the same treatment as ``auto_research``'s campaign name:
    redact, and if the redactors are unavailable fail CLOSED to the slot key, which
    carries no user content.
    """
    raw = f"{crew.get('name') or slot_key} · {owner}/{repo}"
    if not _HAS_SECURITY:
        return slot_key
    raw, _ = redact_exfiltration_urls(raw)
    raw, _ = redact_credentials(raw)
    return raw


def sync_trust(slot: Any, crew: dict[str, Any]) -> bool:
    """Set the slot's per-slot trust from the crew's ``unattended`` flag.

    Called at launch AND from the watchdog every cycle. ``slot._trust`` is
    in-memory only — deliberately, since it is a grant, not a setting — so a
    gateway restart drops it and an unattended crew would sit in an approval
    prompt for two hours and then be denied. ``auto_research`` re-sets it per
    cycle for exactly this reason.

    Assignment, not a one-way set: flipping ``unattended`` off must REVOKE trust
    within one cycle. This is the per-slot flag (the same one the interactive
    "trust this session" writes), never the process-wide yolo toggle, which cannot
    express a per-crew grant.
    """
    want = bool(crew.get("unattended"))
    had = bool(getattr(slot, "_trust", False))
    slot._trust = want
    if want != had:
        logger.info(
            "issue-radar crew %s: trust %s",
            crew.get("id"),
            "established" if want else "revoked",
        )
    return want


async def ensure_crew_session(
    state: Any, owner: str, repo: str, crew: dict[str, Any]
) -> Any:
    """Attach to (or create) the crew's app-owned slot and return it.

    Agent, workspace and model all come from the crew record. ``model`` OVERRIDES
    whatever the chosen agent pins, because ``get_or_create_slot`` takes it as an
    explicit argument — that is the intended precedence: the crew's config is the
    operator's last word.
    """
    slot_key = str(crew.get("slot_key") or f"crew-{crew.get('id')}")
    slot = state.get_or_create_slot(
        name=slot_key,
        agent=str(crew.get("agent") or "kirocrew"),
        workspace=str(crew.get("workspace") or "default"),
        model=str(crew.get("model") or ""),
        app=APP_NAME,
    )
    title = _slot_title(owner, repo, crew, slot_key)
    if slot.title != title or not getattr(slot, "_titled", False):
        slot.title = title
        # Lock the title: the loop's messages arrive as nudge/user rows on an
        # app-owned slot nobody named, and an auto-titler that fired here would
        # rename the crew's session after whatever the first turn happened to do.
        slot._titled = True
        log = getattr(state, "conversation_log", None)
        if log is not None:
            try:
                from kiro_crew.dashboard.chat_utils import slot_history_key

                await asyncio.to_thread(log.set_title, slot_history_key(slot), title)
            except Exception:  # pragma: no cover - persistence is best-effort
                logger.warning(
                    "issue-radar: could not persist crew slot title for %s",
                    slot_key,
                    exc_info=True,
                )
        _call_if_present(state, "push_slot_title", slot.key, title)
    sync_trust(slot, crew)
    _call_if_present(state, "push_slots_update")
    return slot


async def launch_crew(
    state: Any, owner: str, repo: str, crew: dict[str, Any], root: Path | None = None
) -> Any:
    """Bring a crew online: ensure its session, then arm its nudge loop.

    ``max_cycles=0`` — a crew is not a bounded errand. Its brakes are the record's
    ``enabled``/``retired_at`` flags, the STOP sentinel, and the app's own enabled
    gate, all of which the watchdog re-reads every cycle.
    """
    slot = await ensure_crew_session(state, owner, repo, crew)
    svc = _autonudge_instance() if _autonudge_instance is not None else None
    if svc is None:
        logger.warning(
            "issue-radar crew %s: autonudge unavailable — session exists but no loop",
            crew.get("id"),
        )
        return slot
    await svc.add(
        slot_key=slot.key,
        message=compose_turn_prompt(slot, owner, repo, crew, root),
        idle_secs=DEFAULT_IDLE_SECS,
        max_cycles=0,
        stop_sentinel_path=str(stop_sentinel_path(owner, repo, str(crew.get("id")), root)),
    )
    return slot


async def wake_crew(
    state: Any,
    owner: str,
    repo: str,
    crew: dict[str, Any],
    reason: str = "",
    root: Path | None = None,
) -> bool:
    """Give the crew a turn NOW because a signal moved. Returns whether a turn started.

    Two writes, both needed. The armed loop's message is refreshed so an idle-timer
    fire that lands later carries the CURRENT snapshot instead of the one composed
    at launch; and the prompt is dispatched immediately, because the whole point of
    the sweep is that the crew does not wait out an idle gap after CI turns red.
    ``enqueue_or_run_prompt`` queues instead of racing when the crew is mid-turn.
    """
    slot = state.get_slot(str(crew.get("slot_key") or f"crew-{crew.get('id')}"))
    if slot is None:
        slot = await _rehydrate(state, str(crew.get("slot_key") or ""))
    if slot is None:
        logger.info(
            "issue-radar crew %s: no session to wake (%s)", crew.get("id"), reason or "signal"
        )
        return False
    sync_trust(slot, crew)
    prompt = compose_turn_prompt(slot, owner, repo, crew, root)
    svc = _autonudge_instance() if _autonudge_instance is not None else None
    if svc is not None:
        loop = svc.get_by_slot(slot.key)
        if loop is not None:
            try:
                await svc.update(loop.id, message=prompt)
            except Exception:  # pragma: no cover - refresh is best-effort
                logger.debug("issue-radar: nudge refresh failed", exc_info=True)
    if getattr(slot, "running", False):
        # DROPPED, not queued — the same call autonudge's own fire path makes, and
        # for a stronger reason here: a queued prompt can carry the whole brief, so
        # a crew that stayed busy across three sweeps would come back to three
        # stacked copies of its own instructions. Nothing is lost by dropping it.
        # The loop's message was just refreshed with the current snapshot, and the
        # crew reconciles EVERY open item against the six signals at the top of each
        # turn anyway — the wake buys latency, it does not carry information.
        logger.info(
            "issue-radar crew %s: mid-turn, wake dropped (%s)",
            crew.get("id"),
            reason or "signal",
        )
        return False
    tagged = f"[crew wake: {reason}]\n{prompt}" if reason else prompt
    try:
        from kiro_crew.dashboard.chat_runner import _run_chat
    except ImportError:  # pragma: no cover - defensive
        return False
    started = slot.enqueue_or_run_prompt(tagged, _run_chat, state)
    _call_if_present(state, "push_slots_update")
    logger.info(
        "issue-radar crew %s woken (%s): turn %s",
        crew.get("id"),
        reason or "signal",
        "started" if started else "queued",
    )
    return bool(started)


async def _rehydrate(state: Any, slot_key: str) -> Any:
    """Rebuild a slot the gateway no longer holds in memory (tab closed, restart).

    ``autonudge`` cannot do this for us: arming and firing both gate on the slot
    being resident, so a crew whose slot left ``_slots`` is unreachable by nudge
    alone. Reads are hoisted off the event loop by the async form.
    """
    if not slot_key:
        return None
    try:
        from kiro_crew.dashboard.chat_persistence import rehydrate_slot_from_history_async
    except ImportError:  # pragma: no cover - defensive
        return None
    try:
        return await rehydrate_slot_from_history_async(state, slot_key)
    except Exception:  # pragma: no cover - defensive
        logger.debug("issue-radar: rehydrate failed for %s", slot_key, exc_info=True)
        return None


# ── the watchdog (one pass per poll cycle) ──────────────────────────────────


def is_live(crew: dict[str, Any]) -> bool:
    """Whether this crew should be worked at all.

    ``paused_reason`` counts as not-live even though the crew record is enabled: the
    brief tells a paused crew to read the ledger and end the turn immediately, so
    waking one buys a turn whose only outcome is the cost of the turn.
    """
    return bool(
        crew.get("enabled") and not crew.get("retired_at") and not crew.get("paused_reason")
    )


async def watchdog_cycle(
    state: Any,
    owner: str,
    repo: str,
    crews: list[dict[str, Any]],
    root: Path | None = None,
) -> None:
    """Re-establish what the process does not persist. Zero API calls, zero LLM.

    Three things, every cycle, for the same reason ``auto_research``'s watchdog does
    them: they are all in-memory state that a restart drops.

    * **Trust.** ``slot._trust`` lives only in the slot object, so an unattended
      crew silently loses its grant on restart and the next tool call parks it in an
      approval prompt nobody is watching.
    * **The loop.** A crew whose autonudge loop is missing or deactivated has no
      clock at all; the sweep still wakes it on a signal, but it would never pick up
      new work again. Re-arming here makes "enabled" mean enabled.
    * **Revocation.** A crew that was disabled, retired or paused while its slot was
      resident keeps its trust until something takes it away.
    """
    svc = _autonudge_instance() if _autonudge_instance is not None else None
    for crew in crews:
        slot_key = str(crew.get("slot_key") or f"crew-{crew.get('id')}")
        slot = state.get_slot(slot_key) if hasattr(state, "get_slot") else None
        if not is_live(crew):
            if slot is not None and getattr(slot, "_trust", False):
                slot._trust = False
                logger.info("issue-radar crew %s: trust revoked (not live)", crew.get("id"))
            if svc is not None:
                loop = svc.get_by_slot(slot_key)
                if loop is not None and loop.active:
                    await svc.update(loop.id, active=False)
            continue
        if slot is not None:
            sync_trust(slot, crew)
        if svc is None:
            continue
        loop = svc.get_by_slot(slot_key)
        if loop is None:
            # No loop for a live crew: either it has never been launched or a
            # restart lost it. Launching is idempotent on the slot key.
            await launch_crew(state, owner, repo, crew, root)
        elif not loop.active:
            await svc.update(loop.id, active=True)


async def suspend_crews(state: Any) -> int:
    """Turning the app off must STOP the crews. Returns how many slots were cleared.

    Called from the poll loop's disabled branch, because that branch returns before
    any per-crew code runs — and the two things that keep a crew going are both
    outside this module's reach once armed: ``slot._trust`` sits on the slot, and
    autonudge loops fire regardless of any app's enabled flag. Without this, a
    disabled Issue Radar keeps running unattended, auto-approved turns.

    Works entirely from IN-MEMORY state — the loop registry and the resident slots —
    and reads nothing from disk. A disabled app must stay silent, and that includes
    not walking the connected-repo config on every one of its idle cycles. It also
    catches a crew slot whose record was deleted while the session was live, which a
    record-driven walk would miss. Idempotent, as the precedent in
    ``auto_research`` is.
    """
    if state is None:
        return 0
    prefix = "crew-"
    cleared = 0
    svc = _autonudge_instance() if _autonudge_instance is not None else None
    if svc is not None:
        for loop in svc.list_all():
            if not loop.slot_key.startswith(prefix):
                continue
            if loop.active:
                try:
                    await svc.update(loop.id, active=False)
                except Exception:  # pragma: no cover - cleanup must not raise
                    logger.warning(
                        "issue-radar: could not deactivate crew loop %s on disable", loop.id
                    )
    for key, slot in list((getattr(state, "_slots", None) or {}).items()):
        if not str(key).startswith(prefix) or getattr(slot, "_app", "") != APP_NAME:
            continue
        if getattr(slot, "_trust", False):
            slot._trust = False
            cleared += 1
    if cleared:
        logger.info("issue-radar disabled — cleared trust on %d crew session(s)", cleared)
    return cleared


def _call_if_present(obj: Any, name: str, *args: Any) -> None:
    """Call an optional method on the dashboard state (test stubs omit most of them)."""
    fn = getattr(obj, name, None)
    if callable(fn):
        try:
            fn(*args)
        except Exception:  # pragma: no cover - UI push is never load-bearing
            logger.debug("issue-radar: %s failed", name, exc_info=True)


# ── unblock signals ─────────────────────────────────────────────────────────
#
# The six signals from the brief's table, and the field each one is read from:
#
#   requester replied      issue comment count            (issue detail)
#   CI state changed       check rollup + bucket counts   (batched enrichment)
#   approved / changes req review verdict                 (PR timeline, gated)
#   merge conflict         mergeable / merge state        (batched enrichment)
#   PR merged              merged flag                    (PR detail)
#   post-merge comment     PR comment count while merged  (PR detail)
#
# Missing one means an item silently stalls forever, which is why they are named
# individually here rather than collapsed into "something changed".

SIG_REPLY = "requester-replied"
SIG_CI = "ci-changed"
SIG_REVIEW = "review-verdict"
SIG_CONFLICT = "merge-conflict"
SIG_MERGED = "pr-merged"
SIG_POST_MERGE = "post-merge-comment"

UNBLOCK_SIGNALS = (
    SIG_REPLY,
    SIG_CI,
    SIG_REVIEW,
    SIG_CONFLICT,
    SIG_MERGED,
    SIG_POST_MERGE,
)

SIGNALS_SCHEMA = 1

#: How often an item in each phase is re-read. The forge is the mover for some
#: phases and the crew is the mover for others, and paying a CI-grade cadence for
#: a phase waiting on a human is pure rate limit. ``selected`` is pre-claim and
#: purely local — there is nothing public to watch yet.
RECHECK_SEC = {
    "awaiting-ci": 60,
    "addressing-review": 60,
    "awaiting-merge": 120,
    "claimed": 300,
    "investigating": 300,
    "implementing": 300,
    "awaiting-reply": 300,
    "escalated": 300,
}
_DEFAULT_RECHECK_SEC = 300

#: Phases where a fresh review verdict is worth one extra (paginated) timeline
#: read. Every other signal is already summarized on the issue/PR object; a review
#: is not, and a bodyless approval moves no counter at all.
_REVIEW_PHASES = frozenset({"awaiting-ci", "addressing-review", "awaiting-merge"})


def signals_path(owner: str, repo: str, root: Path | None = None) -> Path:
    """Where the fingerprints live — NOT inside ``crews/`` (see the module docstring)."""
    return store.repo_data_dir(owner, repo, root) / "crew-signals.json"


def read_signals(owner: str, repo: str, root: Path | None = None) -> dict[str, Any]:
    path = signals_path(owner, repo, root)
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(doc, dict) or doc.get("schema") != SIGNALS_SCHEMA:
        # A schema mismatch is a cache miss here, exactly as it is for the issue
        # caches: the next sweep re-seeds and reports nothing, which loses at most
        # one cycle of latency and cannot report a stale signal.
        return {}
    items = doc.get("items")
    return items if isinstance(items, dict) else {}


def write_signals(
    owner: str, repo: str, items: dict[str, Any], root: Path | None = None
) -> None:
    atomic_write(
        signals_path(owner, repo, root),
        json.dumps({"schema": SIGNALS_SCHEMA, "items": items}, indent=2),
    )


def _item_key(crew_id: str, number: Any) -> str:
    return f"{crew_id}:{number}"


def detect_unblocks(prev: dict[str, Any] | None, cur: dict[str, Any]) -> list[str]:
    """Which of the six signals moved between two fingerprints.

    A FIRST observation reports nothing: it seeds the mark, the same discipline the
    new-issue watcher uses so connecting a repo does not announce its whole
    backlog. Here the equivalent mistake would wake every crew on every item the
    moment the gateway restarts.
    """
    if not prev:
        return []
    out: list[str] = []
    if _as_int(cur.get("issue_comments")) > _as_int(prev.get("issue_comments")):
        out.append(SIG_REPLY)
    if (cur.get("checks"), cur.get("check_counts")) != (
        prev.get("checks"),
        prev.get("check_counts"),
    ):
        # Only when the current reading is a real one — a failed enrichment call
        # reports None, and None-vs-known must not masquerade as "CI moved".
        if cur.get("checks") is not None:
            out.append(SIG_CI)
    verdict = cur.get("review_decision") or ""
    if verdict and verdict != (prev.get("review_decision") or ""):
        out.append(SIG_REVIEW)
    if cur.get("conflicted") and not prev.get("conflicted"):
        out.append(SIG_CONFLICT)
    if cur.get("merged") and not prev.get("merged"):
        out.append(SIG_MERGED)
    if cur.get("merged") and _as_int(cur.get("pr_comments")) > _as_int(
        prev.get("pr_comments")
    ):
        out.append(SIG_POST_MERGE)
    return out


def _as_int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _is_conflicted(mergeable: Any, merge_state: Any) -> bool:
    """A REAL conflict, not an uncomputed one.

    GitHub answers ``mergeable: null`` / ``mergeable_state: "unknown"`` on a cold
    read and computes the merge commit in the background, so truthiness cannot be
    used here: ``not mergeable`` would report every cold read as a conflict and
    wake the crew to resolve one that does not exist.
    """
    if mergeable is False:
        return True
    return str(merge_state or "").lower() == "dirty"


def fingerprint_item(
    key: provider.RepoKey,
    item: dict[str, Any],
    enriched: dict[int, dict[str, Any]],
    prev: dict[str, Any] | None,
) -> dict[str, Any]:
    """Read the six signals for ONE work item. Blocking ``gh`` calls — run off-loop.

    Cost is one REST call for the issue, one more for the PR when the item has one,
    and (only when the PR's ``updated_at`` moved and the phase makes a review
    plausible) one paginated timeline read. The check rollup and merge state come
    from ``enriched``, which the caller fetched for the whole repo in two batched
    GraphQL calls.
    """
    client = provider.client_for(key)
    kwargs = provider.call_kwargs(key)
    number = item.get("number")
    fp: dict[str, Any] = {"phase": item.get("phase") or ""}
    if not isinstance(number, int) or number <= 0:
        return fp  # a record with no issue number has nothing on the forge to read

    issue = client.get_issue_detail(key.owner, key.repo, int(number), **kwargs)
    if isinstance(issue, dict):
        fp["issue_comments"] = _as_int(issue.get("comments"))
        fp["issue_state"] = issue.get("state")

    pr = item.get("pr_number")
    if not isinstance(pr, int) or pr <= 0:
        return fp

    fp["pr_number"] = pr
    row = enriched.get(pr) or {}
    fp["checks"] = row.get("checks_state")
    counts = row.get("checks_counts")
    fp["check_counts"] = counts if isinstance(counts, dict) else None

    # ``resolve_mergeable=False``: the batched readiness call above already supplies
    # a COMPUTED merge state, so paying this call's retry-plus-sleep for the lazy
    # one would buy a second answer to a question already answered.
    detail = client.get_pr_detail(
        key.owner, key.repo, pr, resolve_mergeable=False, **kwargs
    )
    detail = detail if isinstance(detail, dict) else {}
    fp["pr_comments"] = _as_int(detail.get("comments"))
    fp["pr_updated_at"] = detail.get("updated_at")
    fp["merged"] = bool(detail.get("merged") or detail.get("merged_at"))
    mergeable = (
        row.get("mergeable") if row.get("mergeable") is not None else detail.get("mergeable")
    )
    merge_state = row.get("mergeable_state") or detail.get("mergeable_state")
    fp["conflicted"] = _is_conflicted(mergeable, merge_state)
    fp["merge_state"] = merge_state

    # Carry the previous verdict forward when the read is skipped, so a skipped
    # cycle reads as "unchanged" instead of as a verdict being withdrawn.
    fp["review_decision"] = (prev or {}).get("review_decision") or ""
    moved = prev is not None and detail.get("updated_at") != prev.get("pr_updated_at")
    if not fp["merged"] and fp["phase"] in _REVIEW_PHASES and (prev is None or moved):
        fp["review_decision"] = _latest_review_decision(client, key, pr, kwargs)
    return fp


def _latest_review_decision(
    client: Any, key: provider.RepoKey, pr: int, kwargs: dict[str, str]
) -> str:
    """``approved`` / ``changes_requested`` / ``""`` from the PR's newest review.

    The one signal REST does not summarize anywhere on the PR object: an approval
    with no body increments no counter, and ``mergeable_state`` reports both "no
    review yet" and "changes requested" as ``blocked``. So it is read from the
    timeline, and the read is gated on a cheap field having moved first.
    """
    try:
        events = client.list_issue_timeline(key.owner, key.repo, pr, **kwargs)
    except Exception:
        logger.debug("issue-radar: review read failed for #%s", pr, exc_info=True)
        return ""
    latest = ""
    for ev in events or []:
        if not isinstance(ev, dict) or ev.get("kind") != "reviewed":
            continue
        state = str(ev.get("review_state") or "").lower()
        if state in ("approved", "changes_requested"):
            latest = state  # timeline is chronological; the last one wins
    return latest


# ── the sweep ───────────────────────────────────────────────────────────────


def _is_due(item: dict[str, Any], stored: dict[str, Any] | None, now: float) -> bool:
    """Whether this item's phase is due for a re-read on this tick."""
    phase = str(item.get("phase") or "")
    if phase == "selected":
        return False  # pre-claim, local only — nothing public to watch
    if not stored:
        return True
    interval = RECHECK_SEC.get(phase, _DEFAULT_RECHECK_SEC)
    last = stored.get("checked_at")
    if not isinstance(last, (int, float)):
        return True
    # A mark in the FUTURE is due immediately. The mark is wall-clock, so a clock
    # correction (or a machine that resumed with a bad clock) can leave one ahead
    # of now — and an unconditional ``elapsed >= interval`` would then park that
    # item forever, which is the exact silent stall this sweep exists to prevent.
    if now < float(last):
        return True
    return (now - float(last)) >= interval


async def sweep_repo(app: Any, key: provider.RepoKey, root: Path | None = None) -> dict[str, Any]:
    """One zero-LLM pass over every crew's open work items in ``key``'s repo.

    Returns ``{crew_id: [signal, ...]}`` for the crews that were woken (the return
    value is what the tests assert on; the loop ignores it).

    Deliberately NOT gated on the per-repo ``notify_on_new_issue`` setting. That
    flag is a notification preference — whether the bell rings for a new issue —
    and a crew that stopped reconciling its own PRs because the user muted
    notifications would stall every open item with no trace. The app's enabled gate
    is the switch that stops crews, and it stays in ``watch.py``.
    """
    scope = root if root is not None else store.provider_root(
        root=None, provider=key.provider, host=key.host
    )
    crews = await asyncio.to_thread(
        partial(crew_store.list_crews, key.owner, key.repo, scope)
    )
    if not crews:
        return {}
    state = app.get("state") if hasattr(app, "get") else None
    if state is not None:
        # Runs BEFORE the signal pass and over ALL crews, not just the ones with a
        # due item: re-establishing trust and re-arming a lost loop is exactly what
        # a crew with nothing due needs after a restart.
        await watchdog_cycle(state, key.owner, key.repo, crews, scope)
    live = [c for c in crews if is_live(c)]
    if not live:
        return {}

    stored = await asyncio.to_thread(
        partial(read_signals, key.owner, key.repo, scope)
    )
    # WALL clock, not the loop's monotonic one: this value is persisted, and a
    # monotonic reading restarts near zero on the next gateway launch — every
    # stored mark would then sit in the future and no item would ever come due
    # again until the loop clock caught up.
    now = time.time()

    # Pass 1 — which items are due, and which PRs need the batched enrichment.
    due: list[tuple[dict[str, Any], dict[str, Any]]] = []
    open_keys: set[str] = set()
    for crew in live:
        items = await asyncio.to_thread(
            partial(
                crew_store.list_work_items,
                key.owner,
                key.repo,
                str(crew.get("id")),
                scope,
                open_only=True,
            )
        )
        for item in items:
            ikey = _item_key(str(crew.get("id")), item.get("number"))
            open_keys.add(ikey)
            entry = stored.get(ikey)
            if _is_due(item, entry if isinstance(entry, dict) else None, now):
                due.append((crew, item))

    # A mark for an item that is no longer open is dead weight — a crew that works
    # for a month would otherwise carry every issue it ever finished in a file it
    # rewrites every minute. Dropping it also makes a REOPENED item re-seed, which
    # is right: its old fingerprint describes a different state of the world.
    stale = [k for k in stored if k not in open_keys]
    for k in stale:
        stored.pop(k, None)

    if not due:
        if stale:
            await asyncio.to_thread(partial(write_signals, key.owner, key.repo, stored, scope))
        return {}

    prs = sorted(
        {
            pr
            for _, it in due
            if isinstance(pr := it.get("pr_number"), int) and pr > 0
        }
    )
    enriched = await _enrich(key, prs) if prs else {}

    # Pass 2 — fingerprint each due item, compare, then wake each crew ONCE.
    woken: dict[str, Any] = {}
    reasons: dict[str, list[str]] = {}
    crews_by_id = {str(c.get("id")): c for c in live}
    for crew, item in due:
        crew_id = str(crew.get("id"))
        ikey = _item_key(crew_id, item.get("number"))
        entry = stored.get(ikey) if isinstance(stored.get(ikey), dict) else None
        prev = (entry or {}).get("fp") if isinstance((entry or {}).get("fp"), dict) else None
        try:
            fp = await asyncio.to_thread(
                partial(fingerprint_item, key, item, enriched, prev)
            )
        except Exception:
            # A per-item failure leaves its mark UNTOUCHED, so the change is still
            # pending next cycle rather than being silently consumed by the error.
            logger.warning(
                "issue-radar crew sweep failed for %s#%s",
                key.owner,
                item.get("number"),
                exc_info=True,
            )
            continue
        signals = detect_unblocks(prev, fp)
        stored[ikey] = {"fp": fp, "checked_at": now}
        if not signals:
            continue
        woken.setdefault(crew_id, []).extend(signals)
        reasons.setdefault(crew_id, []).append(
            f"#{item.get('number')} {', '.join(signals)}"
        )

    await asyncio.to_thread(
        partial(write_signals, key.owner, key.repo, stored, scope)
    )

    # ONE wake per crew, carrying every reason. Two items signalling in the same
    # sweep is one turn's worth of work, and the second call would be dropped as
    # mid-turn anyway — so the crew would have been told about only the first.
    if state is not None:
        for crew_id, why in reasons.items():
            try:
                await wake_crew(
                    state, key.owner, key.repo, crews_by_id[crew_id], "; ".join(why), scope
                )
            except Exception:  # pragma: no cover - defensive
                logger.warning("issue-radar: waking crew %s failed", crew_id, exc_info=True)
    return woken


async def _enrich(key: provider.RepoKey, prs: list[int]) -> dict[int, dict[str, Any]]:
    """Check rollup + merge state for every watched PR in the repo, batched.

    Two GraphQL calls for the whole repo, not two per PR — and it is the only way
    to see CI at all: a check-run completing does not touch the PR record, so no
    ``updated_at`` anywhere reflects it.
    """
    client = provider.client_for(key)
    kwargs = provider.call_kwargs(key)
    rows = [{"number": n} for n in prs]
    try:
        enriched = await asyncio.to_thread(
            partial(client.enrich_pulls_by_number, key.owner, key.repo, rows, **kwargs)
        )
    except Exception:
        # Best-effort, like every other enrichment caller: an unknown rollup reads
        # as None, which `detect_unblocks` refuses to interpret as a CI change.
        logger.debug("issue-radar: crew sweep enrichment failed", exc_info=True)
        return {}
    out: dict[int, dict[str, Any]] = {}
    for row in enriched or []:
        num = row.get("number") if isinstance(row, dict) else None
        if isinstance(num, int):
            out[num] = row
    return out
