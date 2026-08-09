"""Crew HTTP surface — the Crews dashboard page and the crews' own writes.

Registered from ``routes.register_routes`` (one import, one call) rather than by
the app manifest, so this app still has exactly ONE place that lists its routes.
It lives in its own module because ``routes.py`` is already 200KB and the crew
surface is a separate feature with its own store; the shared request plumbing
(``_key_from_request``, ``_str_field``, ``_st``, ``_require_enabled``,
``_pr_action_preamble``, ``_audit``) is imported from ``routes`` rather than
re-derived, because a second copy of a gate is how one of them eventually ships
without the check.

Routes, all under ``/api/apps/issue-radar``:

  GET    /crews?owner&repo          -> {owner,repo,provider,host, crews[], settings,
                                        counts{on_duty,working,needs_you,paused}}
  POST   /crews                     -> {crew}
  GET    /crews/names?owner&repo    -> {suggestions[]}
  GET    /crew?owner&repo&id        -> {crew, items[], events[], counts{open,escalated}}
  PUT    /crew                      -> {crew}
  DELETE /crew                      -> {crew}          (retire — the record survives)
  PUT    /crew/work                 -> {item, event}
  POST   /crew/pause                -> {crew}
  POST   /crew/guidance             -> {ok, injected, queued, reason}
  GET    /crews/settings?owner&repo -> {settings}
  PUT    /crews/settings            -> {settings}
  GET    /crews/escalations?o&r     -> {escalations:[{crew,item}]}
  POST   /issue/comment             -> {comment_id, url, number}

WRITE-PERMISSION DECISION (the one this module had to make). Two tiers:

  * ``POST /issue/comment`` is a FORGE write and goes through
    ``routes._pr_action_preamble``, which is the existing gate chain used by every
    mutating pull-request route: JSON body -> owner/repo -> connected repo ->
    ``_repo_can_write`` (fail-closed: ``None`` from a transient ``gh`` failure is
    DENIED). Nothing about a crew earns a weaker gate than a human clicking the
    same button.

  * Every LOCAL crew route (crews, crew, work, pause, guidance, settings,
    escalations) requires the repo to be CONNECTED but **not** writable. Three
    reasons, in the order they mattered:
      1. Precedent: ``_handle_put_investigation`` is the same shape — per-repo
         local state, nothing reaches the forge — and is gated on connected only.
      2. ``_repo_can_write`` FAILS CLOSED, and these writes are how a crew records
         work it has ALREADY done. Gating them on a remote permission read means a
         network blip leaves a crew holding a dirty worktree with no way to persist
         its phase or its reason — losing local truth to an unrelated outage. The
         forge writes it would then attempt are each gated on their own.
      3. A read-only repo is still worth crewing: Issue Radar already degrades to
         suggest-only there, and a crew that investigates and escalates without
         pushing is a legitimate configuration. Permissions can also be granted
         later without recreating the crew.
    The exposure this accepts is bounded and local: a user who can reach the
    dashboard can create crew records on a repo they cannot write to. Those records
    cannot mutate the repo — the first forge write refuses.

``crew_store.CrewStoreError`` maps to **409**, never 500: every raise is a
user-visible condition (a duplicate crew name, a second item trying to enter an
editing phase). "Unknown crew" is caught earlier and answered 404, because a
missing record is not a conflict.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from functools import partial, wraps
from typing import Any

from aiohttp import web

from . import crew_store, provider, routes, store

logger = logging.getLogger("kirocrew.app.issue-radar.crews")

_BASE = "/api/apps/issue-radar"

#: Phases in which the crew is NOT the actor: it is waiting on CI, on a human's
#: merge, on a human's reply, or on a human's decision.
#:
#: A FOURTH classification of ``crew_store.PHASES``, deliberately not one of the
#: store's three — and it coincides with none of them. TTL_ACTIVE is about which
#: claims age; COUNTS_TOWARD_OPEN is about slots; EDITING is about worktrees. This
#: one is about who the surface should show as busy, which is a presentation
#: question, which is why it lives in the route module and not in the store.
PARKED_PHASES = frozenset({"awaiting-ci", "awaiting-merge", "awaiting-reply", "escalated"})

#: Work-item fields ``PUT /crew/work`` forwards to ``upsert_work_item``.
#:
#: Listed explicitly so the envelope keys (owner/repo/crew_id/number/event/
#: event_kind) cannot land in the patch, and so this route's writable surface is
#: readable in one place. The store validates each field's TYPE and drops
#: unknowns, so this list is about legibility, not safety.
_WORK_PATCH_FIELDS = (
    "phase", "decision", "why", "next", "worktree", "branch", "base_sha",
    "pr_number", "claim_comment_id", "ci_state", "labels_applied", "escalation",
    "outcome", "tried_approach", "tried_rejected_because",
)

#: Bound on ``GET /crew?limit=`` — the ledger is append-only and a crew that ran
#: for weeks has thousands of lines; an unbounded read is a whole file into RAM
#: and down the websocket on a page open.
_MAX_EVENTS = 500
_DEFAULT_EVENTS = 200

#: Bound on ``GET /crews/names?limit=`` — the pool is 24 names and the create
#: dialog shows a handful of chips.
_MAX_SUGGESTIONS = 24

#: Guidance is injected as a normal user turn, TAGGED so the agent can tell a
#: human directive from its own per-cycle nudge (auto_research tags its guidance
#: the same way, and for the same reason: an untagged line reads as the agent's
#: own note-to-self and gets deprioritized).
_GUIDANCE_TEMPLATE = (
    "[GUIDANCE FROM THE HUMAN — issue #{number}]\n"
    "{text}\n\n"
    "This is a directive from the person who owns this repository. It outranks "
    "your current plan and your standing instructions. Apply it to #{number}, and "
    "record what changed on the work item (PUT /crew/work with an event) before "
    "you continue."
)


# ── shared plumbing ─────────────────────────────────────────────────────────


def _crew_conflict(handler):
    """Map ``CrewStoreError`` onto 409 for EVERY crew route.

    A decorator rather than a try/except per handler: the store raises this for
    invariant violations that are ordinary user conditions (duplicate name, second
    editing item), and letting one escape would surface a legitimate refusal as a
    500 — which the frontend renders as "something broke" instead of the store's
    own message, and which a crew agent would retry forever.
    """
    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.Response:
        try:
            return await handler(request)
        except crew_store.CrewStoreError as exc:
            return web.json_response({"error": str(exc), "code": "crew_conflict"}, status=409)

    return _wrapped


async def _query_preamble(
    request: web.Request,
) -> tuple[provider.RepoKey, web.Response | None]:
    """``?owner=``/``?repo=`` + the connected-repo gate, for the GET routes.

    No write gate — see the module docstring's write-permission decision.
    """
    key = routes._key_from_request(request)
    if not key.owner or not key.repo:
        return key, web.json_response(
            {"error": "missing ?owner= and ?repo=", "code": "missing_repo"}, status=400
        )
    # Synchronous config read, so off the loop — the same call every other route in
    # this app makes through asyncio.to_thread.
    if not await asyncio.to_thread(routes._connected, key):
        return key, web.json_response(
            {"error": f"{key.slug} is not connected — call /connect first",
             "code": "repo_not_connected"},
            status=404,
        )
    return key, None


async def _body_preamble(
    request: web.Request,
) -> tuple[dict, provider.RepoKey, web.Response | None]:
    """JSON body + owner/repo + the connected-repo gate, for the mutating routes.

    Deliberately NOT ``routes._pr_action_preamble``: that one also demands
    ``_repo_can_write``, which these local-state writes intentionally do not
    require (module docstring). Everything else — the malformed-JSON 400, the
    non-object 400, the not-connected 404 — is the same, in the same order, so a
    caller cannot tell the two preambles apart except by the gate that differs.
    """
    try:
        raw = await request.json()
    except Exception:
        return {}, provider.RepoKey(), web.json_response(
            {"error": "request body must be JSON", "code": "invalid_json"}, status=400
        )
    if not isinstance(raw, dict):
        return {}, provider.RepoKey(), web.json_response(
            {"error": "request body must be a JSON object", "code": "invalid_json"}, status=400
        )
    key = routes._key_from_body(raw)
    if not key.owner or not key.repo:
        return raw, key, web.json_response(
            {"error": "missing 'owner'/'repo'", "code": "missing_repo"}, status=400
        )
    if not await asyncio.to_thread(routes._connected, key):
        return raw, key, web.json_response(
            {"error": f"{key.slug} is not connected — call /connect first",
             "code": "repo_not_connected"},
            status=404,
        )
    return raw, key, None


async def _require_crew(
    key: provider.RepoKey, crew_id: str, *, must_be_live: bool
) -> tuple[dict, web.Response | None]:
    """Load a crew, or the response to return instead.

    An unknown crew is **404**, not the 409 that ``update_crew``'s own
    ``CrewStoreError`` would produce: a record that does not exist is not a
    conflicting write, and a 409 tells a crew agent to retry something that can
    never succeed. The residual race (the crew is retired between this read and
    the write) still surfaces as the store's 409, which is the correct answer for
    a write that lost.

    ``must_be_live`` refuses a RETIRED crew. Retiring stops the crew and releases
    its slot, so a work-item write or a guidance injection afterwards would either
    vanish (nothing will ever pick it up) or resurrect a crew whose name is still
    attached to public claim comments. A rename or a re-retire is still allowed —
    those only touch the archived record.
    """
    if not crew_id:
        return {}, web.json_response(
            {"error": "missing 'id'", "code": "missing_crew_id"}, status=400
        )
    crew = await routes._st(key, crew_store.read_crew, key.owner, key.repo, crew_id)
    if crew is None:
        return {}, web.json_response(
            {"error": f"unknown crew {crew_id!r}", "code": "crew_not_found"}, status=404
        )
    if must_be_live and crew.get("retired_at"):
        return crew, web.json_response(
            {"error": f"crew {crew.get('name') or crew_id!r} is retired", "code": "crew_retired"},
            status=409,
        )
    return crew, None


def _crew_flags(crew: dict, open_items: list[dict]) -> dict[str, bool]:
    """The three per-crew booleans the chip counts sum.

    Exactly the definitions the Crews page filters on:
      ``working``   — the NEWEST open item is in a non-parked phase, i.e. the crew
                      itself is the actor right now. Newest, not any: a crew with
                      one item parked on CI and one being implemented is working,
                      and ``list_work_items`` already returns newest-progress-first.
      ``needs_you`` — it has an escalated item, so a human is the blocker.
      ``paused``    — switched off but not retired.

    These are NOT mutually exclusive and the counts they feed are NOT a partition:
    a paused crew with an in-flight item is counted in both. That is correct for
    chip filters, where each chip is an independent predicate rather than a slice
    of a pie — the numbers are deliberately allowed to sum past the crew count.
    """
    return {
        "working": bool(open_items) and open_items[0].get("phase") not in PARKED_PHASES,
        "needs_you": any(it.get("phase") == "escalated" for it in open_items),
        "paused": crew.get("enabled") is False and not crew.get("retired_at"),
    }


def _crew_status(flags: dict[str, bool]) -> str:
    """One status for the crew's status DOT.

    A dot can only be one colour, so unlike the counts this must pick, and the
    order is by what the user has to do about it: a paused crew is doing nothing
    regardless of what it holds, an escalation is waiting on the human, work in
    flight needs nothing, and idle is the absence of all three.
    """
    if flags["paused"]:
        return "paused"
    if flags["needs_you"]:
        return "needs_you"
    if flags["working"]:
        return "working"
    return "idle"


def _crews_page(owner: str, repo: str, root: Any) -> dict[str, Any]:
    """Everything ``GET /crews`` answers, computed in ONE off-loop call.

    Not N separate ``_st`` round-trips: the counts need every crew's open work
    items, which is a directory walk plus a JSON read per item, so a hop per crew
    turns one page load into 2×N event-loop hand-offs for data that is already
    being read on the same thread.
    """
    crews = crew_store.list_crews(owner, repo, root)
    counts = {"on_duty": len(crews), "working": 0, "needs_you": 0, "paused": 0}
    for crew in crews:
        open_items = crew_store.list_work_items(
            owner, repo, str(crew.get("id") or ""), root, open_only=True
        )
        flags = _crew_flags(crew, open_items)
        for name, value in flags.items():
            if value:
                counts[name] += 1
        # Additive per-crew field, derived from the same flags as the counts. It is
        # here so the phase taxonomy stays in one language: without it the frontend
        # has to re-encode PARKED_PHASES in TypeScript and the two drift the first
        # time a phase is added.
        crew["status"] = _crew_status(flags)
    return {"crews": crews, "settings": crew_store.read_settings(owner, repo, root), "counts": counts}


def _crew_page(owner: str, repo: str, crew_id: str, limit: int, root: Any) -> dict[str, Any]:
    """Everything ``GET /crew`` answers, in ONE off-loop call (see ``_crews_page``)."""
    return {
        "crew": crew_store.read_crew(owner, repo, crew_id, root),
        "items": crew_store.list_work_items(owner, repo, crew_id, root),
        "events": crew_store.read_events(owner, repo, root, crew_id=crew_id, limit=limit),
        "counts": {
            "open": crew_store.open_slot_count(owner, repo, crew_id, root),
            "escalated": crew_store.escalated_count(owner, repo, crew_id, root),
        },
    }


def _escalations(owner: str, repo: str, root: Any) -> list[dict[str, Any]]:
    """Every escalated work item in the repo, paired with its crew.

    LONGEST-WAITING FIRST. ``last_progress_at`` is stamped when the escalation is
    recorded and the handback timeout is measured from it, so this is both the
    order of urgency and the order in which these will auto-return their claims —
    the desk shows you the one about to expire at the top rather than buried.
    """
    out: list[dict[str, Any]] = []
    for crew in crew_store.list_crews(owner, repo, root):
        for item in crew_store.list_work_items(
            owner, repo, str(crew.get("id") or ""), root, open_only=True
        ):
            if item.get("phase") == "escalated":
                out.append({"crew": crew, "item": item})
    out.sort(key=lambda row: row["item"].get("last_progress_at") or "")
    return out


def _bounded_limit(raw: str, default: int, ceiling: int) -> int:
    """A positive ``?limit=`` clamped to ``ceiling``; anything unparseable is the
    default. A bad limit is not worth a 400 on a read route — the ceiling is what
    protects the loop, and it applies either way."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, ceiling))


def _drop_issue_detail_cache(owner: str, repo: str, number: int, root: Any) -> None:
    """Invalidate one issue's cached detail + timeline after a comment lands.

    ``store`` has ``drop_pr_detail_cache`` but no issue equivalent yet, and adding
    one is another module's change, so this is that same one-line unlink through
    the store's own public path helper (OSError suppressed for the same reason:
    failing to invalidate a cache must not fail the write that succeeded).

    It matters more here than on the PR side. The crew's claim protocol READS the
    timeline back to confirm its own check-in comment; served a pre-comment cache
    it would conclude the claim never posted and post a second one.
    """
    with contextlib.suppress(OSError):
        store.issue_detail_cache_path(owner, repo, int(number), root).unlink(missing_ok=True)


# ── crews (list / create / names) ───────────────────────────────────────────


async def _handle_crews_list(request: web.Request) -> web.Response:
    """GET /crews?owner&repo — the Crews page's whole payload: the repo's
    non-retired crews (each with a derived ``status``), the repo-wide protocol
    settings, and the four chip counts."""
    key, early = await _query_preamble(request)
    if early is not None:
        return early
    page = await asyncio.to_thread(
        partial(_crews_page, key.owner, key.repo, routes._scope(key))
    )
    return web.json_response({**routes._identity(key), **page})


async def _handle_crew_create(request: web.Request) -> web.Response:
    """POST /crews {"owner","repo","name", ...crew fields} -> {"crew"}.

    The store enforces name uniqueness (including retired crews' names) and drops
    unknown fields, so this route validates only that a name was sent — a
    duplicate comes back as the store's own 409 message, which names the taken
    name rather than a generic conflict.
    """
    body, key, early = await _body_preamble(request)
    if early is not None:
        return early
    if not routes._str_field(body, "name"):
        return web.json_response(
            {"error": "'name' is required", "code": "name_required"}, status=400
        )
    crew = await routes._st(key, crew_store.create_crew, key.owner, key.repo, body)
    routes._audit("crew_create", f"{key.slug}:{crew['id']}", "ok")
    return web.json_response({"crew": crew})


async def _handle_crew_names(request: web.Request) -> web.Response:
    """GET /crews/names?owner&repo[&limit] -> {"suggestions"} — unused galaxy
    names for the create dialog's chips. Suggestions only: the name field is free
    text, so uniqueness is enforced on create, not here."""
    key, early = await _query_preamble(request)
    if early is not None:
        return early
    limit = _bounded_limit(request.query.get("limit") or "", 6, _MAX_SUGGESTIONS)
    suggestions = await routes._st(
        key, crew_store.suggest_names, key.owner, key.repo, limit=limit
    )
    return web.json_response({"suggestions": suggestions})


# ── one crew (read / update / retire) ───────────────────────────────────────


async def _handle_crew_read(request: web.Request) -> web.Response:
    """GET /crew?owner&repo&id[&limit] -> {"crew","items","events","counts"} —
    one crew's page: its record, all its work items (newest progress first),
    its slice of the event ledger, and its slot accounting."""
    key, early = await _query_preamble(request)
    if early is not None:
        return early
    crew_id = (request.query.get("id") or "").strip()
    # Read routes accept a retired crew: its record, work log and ledger are kept
    # deliberately so the page still opens after retirement.
    _crew, missing = await _require_crew(key, crew_id, must_be_live=False)
    if missing is not None:
        return missing
    limit = _bounded_limit(request.query.get("limit") or "", _DEFAULT_EVENTS, _MAX_EVENTS)
    page = await asyncio.to_thread(
        partial(_crew_page, key.owner, key.repo, crew_id, limit, routes._scope(key))
    )
    return web.json_response(page)


async def _handle_crew_update(request: web.Request) -> web.Response:
    """PUT /crew {"owner","repo","id", ...patch} -> {"crew"}.

    Merges a validated patch. A rename re-checks uniqueness in the store (409) and
    leaves ``avatar_seed`` alone, so the crew keeps its face. Allowed on a retired
    crew: correcting an archived record touches nothing live.
    """
    body, key, early = await _body_preamble(request)
    if early is not None:
        return early
    crew_id = routes._str_field(body, "id")
    _crew, missing = await _require_crew(key, crew_id, must_be_live=False)
    if missing is not None:
        return missing
    crew = await routes._st(key, crew_store.update_crew, key.owner, key.repo, crew_id, body)
    return web.json_response({"crew": crew})


async def _handle_crew_retire(request: web.Request) -> web.Response:
    """DELETE /crew {"owner","repo","id"} -> {"crew"} — RETIRE, not delete.

    The record, its name reservation and its work log all survive: the name still
    appears in check-in comments the crew left on the forge, so reusing it would
    make an old comment look like a live claim. Idempotent — retiring a retired
    crew re-stamps it rather than 409ing, so a double-click is not an error.
    """
    body, key, early = await _body_preamble(request)
    if early is not None:
        return early
    crew_id = routes._str_field(body, "id")
    _crew, missing = await _require_crew(key, crew_id, must_be_live=False)
    if missing is not None:
        return missing
    crew = await routes._st(key, crew_store.retire_crew, key.owner, key.repo, crew_id)
    routes._audit("crew_retire", f"{key.slug}:{crew_id}", "ok")
    return web.json_response({"crew": crew})


# ── work items (the crews' own write path) ──────────────────────────────────


async def _handle_crew_work(request: web.Request) -> web.Response:
    """PUT /crew/work {"owner","repo","crew_id","number", ...patch, "event",
    "event_kind"} -> {"item","event"}.

    THE route a crew writes its progress through, and the one the MCP write tool
    targets. It upserts the work item AND appends one ledger line in a single
    call, which is the whole point: a phase cannot change without a logged reason,
    because there is no route that changes one without the other.

    ORDER MATTERS and is: validate the log line -> write the item -> append the
    line. Validating the kind and text FIRST means the only way to reach an
    unlogged write is an I/O failure between the two writes (the store's own
    refusals, including the second-editing-item 409, all happen before anything is
    appended). Appending first would be worse: a store refusal would leave the
    ledger asserting a change that never happened, and a lie in an append-only log
    cannot be taken back.
    """
    body, key, early = await _body_preamble(request)
    if early is not None:
        return early

    crew_id = routes._str_field(body, "crew_id")
    # Reusing the PR field parser rather than copying its bound: the bound is the
    # point (the number becomes a FILENAME), and a second copy is how one of them
    # ships without it. Its out-of-range text says "pull-request number", which is
    # cosmetically wrong here and only reachable past 1e9.
    number, number_error = routes._pr_number_field(body)
    if number_error is not None:
        return number_error

    event_text, too_long = routes._pr_body_field(body, "event")
    if too_long is not None:
        return too_long
    if not event_text:
        return web.json_response(
            {"error": "'event' is required — a work-item write must say why",
             "code": "event_required"},
            status=400,
        )
    event_kind = routes._str_field(body, "event_kind")
    if event_kind not in crew_store.EVENT_KINDS:
        return web.json_response(
            {"error": f"'event_kind' must be one of {', '.join(crew_store.EVENT_KINDS)}",
             "code": "invalid_event_kind"},
            status=400,
        )

    _crew, missing = await _require_crew(key, crew_id, must_be_live=True)
    if missing is not None:
        return missing

    patch = {field: body[field] for field in _WORK_PATCH_FIELDS if field in body}
    item = await routes._st(
        key, crew_store.upsert_work_item, key.owner, key.repo, crew_id, number, patch
    )
    event = await routes._st(
        key, crew_store.append_event, key.owner, key.repo, crew_id, number,
        event_kind, event_text,
    )
    return web.json_response({"item": item, "event": event})


async def _handle_crew_pause(request: web.Request) -> web.Response:
    """POST /crew/pause {"owner","repo","id","paused", "reason"?} -> {"crew"}.

    ``paused`` is the request's own verb rather than a raw ``enabled`` patch, so
    the caller cannot half-express the state: pausing stores the reason, resuming
    CLEARS it. A stale reason on a running crew is worse than none — the page
    would show a live crew explaining why it is stopped.
    """
    body, key, early = await _body_preamble(request)
    if early is not None:
        return early
    paused = body.get("paused")
    if not isinstance(paused, bool):
        return web.json_response(
            {"error": "'paused' must be a boolean", "code": "invalid_paused"}, status=400
        )
    crew_id = routes._str_field(body, "id")
    _crew, missing = await _require_crew(key, crew_id, must_be_live=True)
    if missing is not None:
        return missing
    patch = {
        "enabled": not paused,
        "paused_reason": routes._str_field(body, "reason") if paused else "",
    }
    crew = await routes._st(key, crew_store.update_crew, key.owner, key.repo, crew_id, patch)
    routes._audit("crew_pause", f"{key.slug}:{crew_id}:{'on' if paused else 'off'}", "ok")
    return web.json_response({"crew": crew})


# ── guidance (human -> the crew's own chat session) ─────────────────────────


async def _handle_crew_guidance(request: web.Request) -> web.Response:
    """POST /crew/guidance {"owner","repo","id","number","text"} -> {"ok","injected"}.

    Appends the human's directive INTO the crew's existing chat session rather
    than handing it to a fresh agent, so the crew answers with everything it has
    already read — the worktree it built, the code it looked at, the approaches it
    already rejected. That context is the reason this is not simply a new session
    seeded with the issue.

    The slot key is the crew record's ``slot_key``. A worker slot is in-memory, so
    a gateway restart or the idle-slot cleanup (which archives a quiet session with
    ``closed=True``) leaves the transcript on disk and no slot: rehydrate with
    ``adopt_closed=True``, because this slot's lifecycle belongs to the app, not to
    a tab the user closed.

    Answers ``injected: false`` at HTTP 200 — never a 500 — when the session truly
    cannot be reached. The guidance failing to land is a state the UI must show and
    the human must retry, not a backend fault; a 500 renders as "something broke"
    and hides which of the two happened.

    NOTHING is appended to the event ledger here. A ledger line's ``text`` is
    PUBLIC (it is rendered inside the claim comment on the forge), and the human's
    words are private to this repo's dashboard. The crew logs its own event when it
    acts on the guidance, which is the line that belongs in public anyway.
    """
    body, key, early = await _body_preamble(request)
    if early is not None:
        return early

    crew_id = routes._str_field(body, "id")
    number, number_error = routes._pr_number_field(body)
    if number_error is not None:
        return number_error
    text, too_long = routes._pr_body_field(body, "text")
    if too_long is not None:
        return too_long
    if not text:
        return web.json_response(
            {"error": "'text' is required", "code": "text_required"}, status=400
        )
    crew, missing = await _require_crew(key, crew_id, must_be_live=True)
    if missing is not None:
        return missing

    slot_key = str(crew.get("slot_key") or "")
    if not slot_key:
        return _not_injected("this crew has no session yet")

    prompt = _GUIDANCE_TEMPLATE.format(number=number, text=text)
    try:
        injected, queued = await _inject_into_slot(request, slot_key, prompt)
    except Exception:
        # Broad on purpose: everything past this point is dashboard internals
        # (rehydration, slot state, turn dispatch) reached from an app route. A
        # failure there is "the session could not be reached", which this route
        # already has a representation for.
        logger.warning("crew guidance: injecting into %s failed", slot_key, exc_info=True)
        return _not_injected("the crew's session could not be reached")
    if not injected:
        return _not_injected("the crew's session is no longer available")

    routes._audit("crew_guidance", f"{key.slug}:{crew_id}#{number}", "ok")
    return web.json_response({"ok": True, "injected": True, "queued": queued, "reason": ""})


def _not_injected(reason: str) -> web.Response:
    """The 200 answer for guidance that could not reach a session.

    ``ok`` reports that the REQUEST was valid and processed; ``injected`` reports
    whether the text reached a live session. Keeping them separate is what lets the
    UI say "the crew is not running — start it and resend" instead of "error".
    """
    return web.json_response({"ok": True, "injected": False, "queued": False, "reason": reason})


async def _inject_into_slot(
    request: web.Request, slot_key: str, prompt: str
) -> tuple[bool, bool]:
    """Deliver ``prompt`` to ``slot_key``'s session. Returns ``(injected, queued)``.

    ``enqueue_or_run_prompt`` rather than a hand-rolled copy of the
    queue-vs-run decision: it makes that choice with no ``await`` between the
    ``running`` check and the mutation (so two concurrent injections cannot both
    start a turn), and it registers the task's exception logger. It is also the
    only path that keeps this app out of ``_queue``/``task``/``_background_tasks``.
    """
    state = request.app.get("state")
    if state is None:
        return False, False

    # Circular import: dashboard.server imports the builtin apps to register their
    # routes, so importing dashboard submodules at this module's scope would close
    # the loop. Function-local, exactly as spec_builder's slot relay does it.
    from kiro_crew.dashboard.chat_persistence import rehydrate_slot_from_history_async
    from kiro_crew.dashboard.chat_runner import _run_chat

    slot = state.get_slot(slot_key)
    if slot is None:
        slot = await rehydrate_slot_from_history_async(state, slot_key, adopt_closed=True)
    if slot is None:
        return False, False

    started = slot.enqueue_or_run_prompt(prompt, _run_chat, state)
    # The turn (or the queue entry) changed what the sidebar should show; the
    # dashboard learns about it from this push, not by polling.
    state.push_slots_update()
    return True, not started


# ── repo-wide protocol settings ─────────────────────────────────────────────


async def _handle_crews_settings_get(request: web.Request) -> web.Response:
    """GET /crews/settings?owner&repo -> {"settings"} — the repo-wide claim
    protocol (TTL, handback timeout, commit trailer), defaults filled in on read.
    Repo-wide and not per-crew: two crews negotiating with different TTLs is how a
    short-TTL crew steals a long-TTL crew's live work."""
    key, early = await _query_preamble(request)
    if early is not None:
        return early
    settings = await routes._st(key, crew_store.read_settings, key.owner, key.repo)
    return web.json_response({"settings": settings})


async def _handle_crews_settings_put(request: web.Request) -> web.Response:
    """PUT /crews/settings {"owner","repo","settings":{...}} -> {"settings"}.

    The ``{"settings": {...}}`` envelope matches the app's existing ``PUT /settings``
    so the two configuration surfaces do not need different client code.

    No ``revision`` precondition, unlike that route. There it is mandatory because
    the PUT replaces the WHOLE document, so a stale client would erase a field it
    never read. ``write_settings`` MERGES per field under the settings lock, so a
    partial patch cannot discard a key it did not send and there is nothing for an
    optimistic-concurrency check to protect.
    """
    body, key, early = await _body_preamble(request)
    if early is not None:
        return early
    patch = body.get("settings")
    if not isinstance(patch, dict):
        return web.json_response(
            {"error": "'settings' must be an object", "code": "invalid_settings"}, status=400
        )
    settings = await routes._st(key, crew_store.write_settings, key.owner, key.repo, patch)
    return web.json_response({"settings": settings})


# ── the desk's escalation queue ─────────────────────────────────────────────


async def _handle_crew_escalations(request: web.Request) -> web.Response:
    """GET /crews/escalations?owner&repo -> {"escalations":[{crew,item}]} — every
    item across the repo that is waiting on a human, longest-waiting first.

    Each row carries the whole crew record, not just an id: the desk renders the
    crew's name and avatar next to its escalation, and a second round-trip per row
    to fetch that is a request per escalation on the first paint.
    """
    key, early = await _query_preamble(request)
    if early is not None:
        return early
    escalations = await asyncio.to_thread(
        partial(_escalations, key.owner, key.repo, routes._scope(key))
    )
    return web.json_response({"escalations": escalations})


# ── the one forge write ─────────────────────────────────────────────────────


async def _handle_issue_comment(request: web.Request) -> web.Response:
    """POST /issue/comment {"owner","repo","number","body"} -> {"comment_id"}.

    Mirrors ``routes._handle_pull_comment``, including its permission gate: the
    shared ``_pr_action_preamble`` (JSON -> owner/repo -> connected ->
    ``_repo_can_write``, which fails closed) and the shared error taxonomy
    (403 for a provider refusal, 502 for anything else upstream). A crew posting a
    check-in comment gets no weaker gate than a human clicking Comment.

    ``add_issue_comment``, not ``add_pr_comment``: this route exists precisely
    because the crew's claim ledger lives on ISSUES, and on GitLab issues and merge
    requests are separate collections with independent numbering — the PR function
    would comment on an unrelated merge request that happens to share the number.

    Returns the comment's ``id`` because the claim protocol needs it: the crew
    stores it as ``claim_comment_id`` and EDITS that same comment on later
    check-ins rather than posting a new one.
    """
    body, key, early = await routes._pr_action_preamble(request, "issue_comment")
    if early is not None:
        return early

    number, number_error = routes._pr_number_field(body)
    if number_error is not None:
        return number_error
    text, too_long = routes._pr_body_field(body)
    if too_long is not None:
        return too_long
    if not text:
        return web.json_response(
            {"error": "'body' is required", "code": "body_required"}, status=400
        )

    target = f"{key.slug}#{number}"
    client = provider.client_for(key)
    try:
        result = await asyncio.to_thread(
            partial(
                client.add_issue_comment, key.owner, key.repo, number, text,
                **provider.call_kwargs(key),
            )
        )
    except routes.GhCliError as exc:
        return routes._pr_action_error("issue_comment", target, exc)

    await asyncio.to_thread(
        partial(_drop_issue_detail_cache, key.owner, key.repo, number, routes._scope(key))
    )
    routes._audit("issue_comment", target, "ok")
    return web.json_response({
        **routes._identity(key),
        "number": number,
        "comment_id": result.get("id"),
        "url": result.get("url"),
    })


# ── registration ────────────────────────────────────────────────────────────


def register_crew_routes(app: web.Application) -> None:
    """Register the crew routes. Called from ``routes.register_routes``.

    Every handler is wrapped in ``routes._require_enabled`` — not optional and not
    inherited from anywhere: routes are registered ONCE at gateway startup and
    Issue Radar is ``defaultEnabled: false``, so an unwrapped handler stays
    callable while the app is switched off. ``_crew_conflict`` is applied under it
    so a store invariant surfaces as 409 rather than 500.
    """
    def _add(method: str, path: str, handler) -> None:
        app.router.add_route(method, f"{_BASE}{path}", routes._require_enabled(_crew_conflict(handler)))

    _add("GET", "/crews", _handle_crews_list)
    _add("POST", "/crews", _handle_crew_create)
    _add("GET", "/crews/names", _handle_crew_names)
    _add("GET", "/crews/settings", _handle_crews_settings_get)
    _add("PUT", "/crews/settings", _handle_crews_settings_put)
    _add("GET", "/crews/escalations", _handle_crew_escalations)
    _add("GET", "/crew", _handle_crew_read)
    _add("PUT", "/crew", _handle_crew_update)
    _add("DELETE", "/crew", _handle_crew_retire)
    _add("PUT", "/crew/work", _handle_crew_work)
    _add("POST", "/crew/pause", _handle_crew_pause)
    _add("POST", "/crew/guidance", _handle_crew_guidance)
    _add("POST", "/issue/comment", _handle_issue_comment)
