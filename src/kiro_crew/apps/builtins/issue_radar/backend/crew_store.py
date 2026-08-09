"""Crew records, work items and the append-only event ledger.

One repository's crews live under its repo data dir::

    repos/<owner>/<repo>/crews/settings.json      protocol constants, repo-wide
    repos/<owner>/<repo>/crews/<crew_id>.json     one crew
    repos/<owner>/<repo>/crews/<crew_id>/<n>.json one work item (crew × issue)
    repos/<owner>/<repo>/crews/events.jsonl       append-only progress log

Every file carries ``schema``. Issue Radar's usual versioning strategy — a schema
mismatch is a cache miss, refetch from the forge — does NOT transfer here: a crew
record has no upstream to refetch from, so readers coerce forward on read and a
real migration is required if the shape ever changes incompatibly.

Locking. ``store.py``'s per-record lock is the model, with one deliberate
difference: work-item writes take the **crew-level** lock, not a per-item one.
The "at most one item in an editing phase" invariant is a statement about the
whole crew, so the check and the write must be atomic together; a per-item lock
would let two concurrent writes each observe no other editor and both proceed.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write

from . import store

CREW_SCHEMA = 1

# ── phases ──────────────────────────────────────────────────────────────────
#
# Three classifications hang off this enum and they deliberately do NOT coincide,
# which is why none of them can be collapsed into a boolean on the record:
#
#   TTL_ACTIVE          — only these age toward the claim TTL. A parked PR or an
#                         open escalation is stronger evidence of a live claim
#                         than any heartbeat could be, and a crew waiting on a
#                         human review for three days has no progress to record.
#   COUNTS_TOWARD_OPEN  — everything unfinished EXCEPT `escalated`, which is
#                         waiting on a human and must not consume a work slot.
#   EDITING             — a worktree with uncommitted changes. At most one per
#                         crew, enforced in `upsert_work_item`.
PHASES = (
    "selected",          # local only, pre-claim — never public
    "claimed",
    "investigating",
    "implementing",
    "awaiting-ci",
    "addressing-review",
    "awaiting-merge",
    "awaiting-reply",
    "escalated",
    "resolved",
    "skipped",
    "yielded",
    "handed-back",
    "preempted",
)
TERMINAL_PHASES = frozenset({"resolved", "skipped", "yielded", "handed-back", "preempted"})
TTL_ACTIVE_PHASES = frozenset({"claimed", "investigating", "implementing"})
EDITING_PHASES = frozenset({"implementing", "addressing-review"})

EVENT_KINDS = (
    "claim", "investigate", "reply", "implement", "ci",
    "review", "conflict", "merge", "escalate", "handback", "skip", "yield",
)

#: Galaxy names. No two share their first two letters, so a crew name is
#: unambiguous at a glance in a log line — `Cartwheel`/`Pinwheel` and
#: `Circinus`/`Cigar` were dropped for exactly that reason, and `Pegasus` /
#: `Phoenix` / `Sextans` because they collide with well-known software or read
#: badly in a work context.
NAME_POOL = (
    "Andromeda", "Bode", "Butterfly", "Carina", "Cigar", "Cocoon",
    "Draco", "Fireworks", "Fornax", "Grus", "Hoag", "Leo",
    "Mayall", "Medusa", "Pinwheel", "Porpoise", "Sculptor", "Sombrero",
    "Spindle", "Tadpole", "Triangulum", "Tucana", "Ursa", "Whirlpool",
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "schema": CREW_SCHEMA,
    "claim_ttl_hours": 48,
    "escalation_handback_days": 3,
    "commit_trailer": "Crew: {name} (Kiro Crew Issue Radar)",
}

_DEFAULT_CREW: dict[str, Any] = {
    "labels": [],
    "auto_resolve_conflicts": True,
    "auto_merge": True,
    "unattended": True,
    "max_open": 3,
    "max_escalated": 3,
    "agent": "kirocrew",
    "model": "",
    "extra_prompt": "",
    "worktree_root": "",
    "enabled": True,
    "paused_reason": "",
}


class CrewStoreError(Exception):
    """A store invariant was violated — a duplicate name, an unknown crew, or a
    second work item trying to enter an editing phase."""


# ── paths ───────────────────────────────────────────────────────────────────


def crews_dir(owner: str, repo: str, root: Path | None = None) -> Path:
    d = store.repo_data_dir(owner, repo, root) / "crews"
    d.mkdir(parents=True, exist_ok=True)
    return d


def crew_path(owner: str, repo: str, crew_id: str, root: Path | None = None) -> Path:
    return crews_dir(owner, repo, root) / f"{crew_id}.json"


def work_item_path(
    owner: str, repo: str, crew_id: str, number: int, root: Path | None = None
) -> Path:
    d = crews_dir(owner, repo, root) / crew_id
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{int(number)}.json"


def events_path(owner: str, repo: str, root: Path | None = None) -> Path:
    return crews_dir(owner, repo, root) / "events.jsonl"


def settings_path(owner: str, repo: str, root: Path | None = None) -> Path:
    return crews_dir(owner, repo, root) / "settings.json"


def _crew_lock_path(owner: str, repo: str, crew_id: str, root: Path | None = None) -> Path:
    return crews_dir(owner, repo, root) / f"{crew_id}.lock"


# ── settings ────────────────────────────────────────────────────────────────


def read_settings(owner: str, repo: str, root: Path | None = None) -> dict[str, Any]:
    """Repo-wide protocol constants, with defaults filled in on read.

    These cannot be per-crew: two crews negotiating with different TTLs is how a
    short-TTL crew steals a long-TTL crew's live work.
    """
    path = settings_path(owner, repo, root)
    out = dict(DEFAULT_SETTINGS)
    if path.is_file():
        try:
            stored = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return out
        if isinstance(stored, dict):
            for key in ("claim_ttl_hours", "escalation_handback_days"):
                val = stored.get(key)
                if isinstance(val, (int, float)) and val > 0:
                    out[key] = int(val)
            trailer = stored.get("commit_trailer")
            if isinstance(trailer, str) and trailer.strip():
                out["commit_trailer"] = trailer.strip()
    return out


def write_settings(
    owner: str, repo: str, patch: dict[str, Any], root: Path | None = None
) -> dict[str, Any]:
    """Merge *patch* into the repo's protocol settings. Returns the stored doc."""
    lock_path = crews_dir(owner, repo, root) / "settings.lock"
    with open(lock_path, "w") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            record = read_settings(owner, repo, root)
            for key in ("claim_ttl_hours", "escalation_handback_days"):
                if key in patch:
                    val = patch[key]
                    if isinstance(val, (int, float)) and val > 0:
                        record[key] = int(val)
            if "commit_trailer" in patch and isinstance(patch["commit_trailer"], str):
                if patch["commit_trailer"].strip():
                    record["commit_trailer"] = patch["commit_trailer"].strip()
            record["schema"] = CREW_SCHEMA
            atomic_write(settings_path(owner, repo, root), json.dumps(record, indent=2))
    return record


# ── crews ───────────────────────────────────────────────────────────────────


def list_crews(
    owner: str, repo: str, root: Path | None = None, *, include_retired: bool = False
) -> list[dict[str, Any]]:
    """Every crew in this repo, oldest first. Retired crews are excluded by
    default but their records are kept — the name stays reserved and their work
    log stays readable."""
    out: list[dict[str, Any]] = []
    for path in sorted(crews_dir(owner, repo, root).glob("*.json")):
        if path.name == "settings.json":
            continue
        try:
            rec = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("retired_at") and not include_retired:
            continue
        out.append(_coerce_crew(rec))
    out.sort(key=lambda r: r.get("created_at") or "")
    return out


def read_crew(
    owner: str, repo: str, crew_id: str, root: Path | None = None
) -> dict[str, Any] | None:
    path = crew_path(owner, repo, crew_id, root)
    if not path.is_file():
        return None
    try:
        rec = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return _coerce_crew(rec) if isinstance(rec, dict) else None


def _coerce_crew(rec: dict[str, Any]) -> dict[str, Any]:
    """Fill defaults on read, the way ``list_connected_repos`` back-fills
    provider/host — so no caller has to know which fields a record predates."""
    out = dict(_DEFAULT_CREW)
    out.update(rec)
    out["schema"] = CREW_SCHEMA
    if not isinstance(out.get("labels"), list):
        out["labels"] = []
    # The avatar seed is stored separately from the name on purpose: renaming a
    # crew must not change its face.
    if not out.get("avatar_seed"):
        out["avatar_seed"] = out.get("name") or ""
    return out


def taken_names(owner: str, repo: str, root: Path | None = None) -> set[str]:
    """Names that may not be reused — including retired crews'.

    A retired crew's name still appears in its work log and in the check-in
    comments it left on GitHub. Reusing it would make an old comment look like a
    live claim.
    """
    return {
        str(c.get("name") or "")
        for c in list_crews(owner, repo, root, include_retired=True)
    }


def suggest_names(owner: str, repo: str, root: Path | None = None, *, limit: int = 6) -> list[str]:
    """Unused pool names first; then ``<Galaxy> II``, ``III``… once it is spent.

    The degraded form is astronomically correct — Leo II, Draco II and Grus II
    are all real dwarf galaxies.
    """
    used = taken_names(owner, repo, root)
    free = [n for n in NAME_POOL if n not in used]
    if len(free) >= limit:
        return free[:limit]
    out = list(free)
    suffix = 2
    romans = {2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI"}
    while len(out) < limit and suffix <= 6:
        for base in NAME_POOL:
            cand = f"{base} {romans[suffix]}"
            if cand not in used and cand not in out:
                out.append(cand)
                if len(out) >= limit:
                    break
        suffix += 1
    return out[:limit]


def create_crew(
    owner: str, repo: str, spec: dict[str, Any], root: Path | None = None
) -> dict[str, Any]:
    """Create a crew. Raises :class:`CrewStoreError` on a duplicate name.

    Uniqueness is enforced HERE rather than only in the create dialog's
    suggestion chips, because the name field is free text.
    """
    name = str(spec.get("name") or "").strip()
    if not name:
        raise CrewStoreError("a crew needs a name")

    lock_path = crews_dir(owner, repo, root) / "_create.lock"
    with open(lock_path, "w") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            if name in taken_names(owner, repo, root):
                raise CrewStoreError(f"crew name {name!r} is already taken in this repo")
            crew_id = f"c_{secrets.token_hex(4)}"
            now = store._now_iso()
            record = dict(_DEFAULT_CREW)
            record.update(
                {
                    "schema": CREW_SCHEMA,
                    "id": crew_id,
                    "name": name,
                    "avatar_seed": str(spec.get("avatar_seed") or name),
                    "avatar_variant": spec.get("avatar_variant"),
                    "slot_key": f"crew-{crew_id}",
                    "created_at": now,
                    "retired_at": None,
                }
            )
            record.update(_validated_crew_patch(spec))
            atomic_write(
                crew_path(owner, repo, crew_id, root), json.dumps(record, indent=2)
            )
    return _coerce_crew(record)


def _validated_crew_patch(patch: dict[str, Any]) -> dict[str, Any]:
    """Only known, type-checked fields survive — same discipline as
    ``write_investigation``: an unknown key in a patch is dropped, not stored."""
    out: dict[str, Any] = {}
    for key in ("agent", "model", "extra_prompt", "worktree_root", "paused_reason"):
        if key in patch and isinstance(patch[key], str):
            out[key] = patch[key]
    for key in ("auto_resolve_conflicts", "auto_merge", "unattended", "enabled"):
        if key in patch and isinstance(patch[key], bool):
            out[key] = patch[key]
    for key in ("max_open", "max_escalated"):
        if key in patch:
            val = patch[key]
            if isinstance(val, (int, float)) and 1 <= int(val) <= 20:
                out[key] = int(val)
    if "labels" in patch and isinstance(patch["labels"], list):
        out["labels"] = [str(x) for x in patch["labels"] if isinstance(x, str) and x.strip()]
    if "avatar_variant" in patch:
        val = patch["avatar_variant"]
        out["avatar_variant"] = int(val) if isinstance(val, (int, float)) else None
    if "avatar_seed" in patch and isinstance(patch["avatar_seed"], str):
        if patch["avatar_seed"].strip():
            out["avatar_seed"] = patch["avatar_seed"].strip()
    return out


def update_crew(
    owner: str, repo: str, crew_id: str, patch: dict[str, Any], root: Path | None = None
) -> dict[str, Any]:
    """Merge *patch* into a crew. A rename re-checks uniqueness but leaves
    ``avatar_seed`` alone, so the crew keeps its face."""
    lock_path = _crew_lock_path(owner, repo, crew_id, root)
    with open(lock_path, "w") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            record = read_crew(owner, repo, crew_id, root)
            if record is None:
                raise CrewStoreError(f"unknown crew {crew_id!r}")
            new_name = str(patch.get("name") or "").strip()
            if new_name and new_name != record.get("name"):
                if new_name in taken_names(owner, repo, root):
                    raise CrewStoreError(f"crew name {new_name!r} is already taken")
                record["name"] = new_name
            record.update(_validated_crew_patch(patch))
            record["schema"] = CREW_SCHEMA
            atomic_write(crew_path(owner, repo, crew_id, root), json.dumps(record, indent=2))
    return _coerce_crew(record)


def retire_crew(
    owner: str, repo: str, crew_id: str, root: Path | None = None
) -> dict[str, Any]:
    """Retire a crew: it stops working but its record, its name reservation and
    its work log all survive."""
    record = update_crew(owner, repo, crew_id, {"enabled": False}, root)
    lock_path = _crew_lock_path(owner, repo, crew_id, root)
    with open(lock_path, "w") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            record = read_crew(owner, repo, crew_id, root) or record
            record["retired_at"] = store._now_iso()
            atomic_write(crew_path(owner, repo, crew_id, root), json.dumps(record, indent=2))
    return _coerce_crew(record)


# ── work items ──────────────────────────────────────────────────────────────


def read_work_item(
    owner: str, repo: str, crew_id: str, number: int, root: Path | None = None
) -> dict[str, Any] | None:
    path = work_item_path(owner, repo, crew_id, number, root)
    if not path.is_file():
        return None
    try:
        rec = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return rec if isinstance(rec, dict) else None


def list_work_items(
    owner: str, repo: str, crew_id: str, root: Path | None = None, *, open_only: bool = False
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    d = crews_dir(owner, repo, root) / crew_id
    if not d.is_dir():
        return out
    for path in sorted(d.glob("*.json")):
        try:
            rec = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rec, dict):
            continue
        if open_only and rec.get("phase") in TERMINAL_PHASES:
            continue
        out.append(rec)
    out.sort(key=lambda r: r.get("last_progress_at") or "", reverse=True)
    return out


def open_slot_count(
    owner: str, repo: str, crew_id: str, root: Path | None = None
) -> int:
    """Work items occupying a slot: unfinished, and NOT escalated — an item
    waiting on a human must not stop the crew picking up other work."""
    return sum(
        1
        for it in list_work_items(owner, repo, crew_id, root, open_only=True)
        if it.get("phase") != "escalated"
    )


def escalated_count(owner: str, repo: str, crew_id: str, root: Path | None = None) -> int:
    return sum(
        1
        for it in list_work_items(owner, repo, crew_id, root, open_only=True)
        if it.get("phase") == "escalated"
    )


def upsert_work_item(
    owner: str,
    repo: str,
    crew_id: str,
    number: int,
    patch: dict[str, Any],
    root: Path | None = None,
) -> dict[str, Any]:
    """Merge *patch* into one work item, per field, and return the stored record.

    ``claimed_at`` is stamped once. ``last_progress_at`` moves ONLY when the patch
    carries real progress — a phase change, a new ``next``, a PR number, a CI
    reading, or an appended ``tried`` entry. A bare read-back must not renew a
    claim, because the TTL is measured from this field.

    Refuses a second item entering an editing phase (see the module docstring for
    why this takes the crew-level lock).
    """
    number = int(number)
    now = store._now_iso()
    lock_path = _crew_lock_path(owner, repo, crew_id, root)
    with open(lock_path, "w") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            existing = read_work_item(owner, repo, crew_id, number, root) or {}
            phase = existing.get("phase") if existing.get("phase") in PHASES else "selected"

            if "phase" in patch:
                new_phase = str(patch["phase"] or "").strip()
                if new_phase not in PHASES:
                    raise CrewStoreError(f"unknown phase {new_phase!r}")
                if new_phase in EDITING_PHASES and phase not in EDITING_PHASES:
                    other = _editing_item(owner, repo, crew_id, root, exclude=number)
                    if other is not None:
                        raise CrewStoreError(
                            f"crew {crew_id} is already editing #{other} — finish or "
                            "commit that before entering an editing phase on another issue"
                        )
                phase = new_phase

            record: dict[str, Any] = {
                "schema": CREW_SCHEMA,
                "crew_id": crew_id,
                "owner": owner,
                "repo": repo,
                "number": number,
                "phase": phase,
                "outcome": existing.get("outcome"),
                "decision": existing.get("decision", ""),
                "why": existing.get("why", ""),
                "next": existing.get("next", ""),
                "tried": list(existing.get("tried") or []),
                "worktree": existing.get("worktree", ""),
                "branch": existing.get("branch", ""),
                "base_sha": existing.get("base_sha", ""),
                "pr_number": existing.get("pr_number"),
                "ci_state": existing.get("ci_state") or {},
                "claim_comment_id": existing.get("claim_comment_id"),
                "labels_applied": list(existing.get("labels_applied") or []),
                "escalation": existing.get("escalation"),
                "claimed_at": existing.get("claimed_at") or (
                    now if phase not in ("selected",) else None
                ),
                "last_progress_at": existing.get("last_progress_at") or now,
                "finished_at": existing.get("finished_at"),
            }

            progressed = "phase" in patch and patch["phase"] != existing.get("phase")

            for key in ("decision", "why", "next", "worktree", "branch", "base_sha"):
                if key in patch and isinstance(patch[key], str):
                    record[key] = patch[key]
                    if key == "next" and patch[key] != existing.get("next"):
                        progressed = True
            if "pr_number" in patch:
                val = patch["pr_number"]
                record["pr_number"] = int(val) if isinstance(val, (int, float)) else None
                progressed = True
            if "claim_comment_id" in patch:
                val = patch["claim_comment_id"]
                record["claim_comment_id"] = int(val) if isinstance(val, (int, float)) else None
            if "ci_state" in patch and isinstance(patch["ci_state"], dict):
                record["ci_state"] = {**record["ci_state"], **patch["ci_state"]}
                progressed = True
            if "labels_applied" in patch and isinstance(patch["labels_applied"], list):
                record["labels_applied"] = [
                    str(x) for x in patch["labels_applied"] if isinstance(x, str)
                ]
            if "escalation" in patch:
                record["escalation"] = (
                    patch["escalation"] if isinstance(patch["escalation"], dict) else None
                )
                progressed = True
            if "outcome" in patch and isinstance(patch["outcome"], str):
                record["outcome"] = patch["outcome"].strip() or None
            tried = patch.get("tried_approach")
            if isinstance(tried, str) and tried.strip():
                record["tried"].append(
                    {
                        "approach": tried.strip(),
                        "rejected_because": str(patch.get("tried_rejected_because") or ""),
                        "at": now,
                    }
                )
                progressed = True

            if progressed:
                record["last_progress_at"] = now
            if phase in TERMINAL_PHASES and not record["finished_at"]:
                record["finished_at"] = now

            atomic_write(
                work_item_path(owner, repo, crew_id, number, root),
                json.dumps(record, indent=2),
            )
    return record


def _editing_item(
    owner: str, repo: str, crew_id: str, root: Path | None = None, *, exclude: int | None = None
) -> int | None:
    """The issue number this crew is currently editing, if any."""
    for it in list_work_items(owner, repo, crew_id, root, open_only=True):
        if it.get("phase") in EDITING_PHASES and it.get("number") != exclude:
            num = it.get("number")
            if isinstance(num, int):
                return num
    return None


# ── event ledger ────────────────────────────────────────────────────────────


def _event_id(ts: str, crew_id: str, number: int, kind: str, text: str) -> str:
    raw = f"{ts}|{crew_id}|{number}|{kind}|{text}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def append_event(
    owner: str,
    repo: str,
    crew_id: str,
    number: int,
    kind: str,
    text: str,
    root: Path | None = None,
) -> dict[str, Any]:
    """Append one progress line.

    The id is content-addressed so a duplicated line merges on read rather than
    conflicting — the same discipline as ops-mission-control's ledger, whose own
    docstring records that it shipped without a lock and was caught in review.

    ``text`` BECOMES PUBLIC: it is rendered both on the crew page and inside the
    ``<details>`` block of the claim comment on the forge. Callers must keep
    absolute paths, host names and anything else environment-specific out of it;
    worktree paths belong in the work item's own fields.
    """
    if kind not in EVENT_KINDS:
        raise CrewStoreError(f"unknown event kind {kind!r}")
    ts = store._now_iso()
    entry = {
        "id": _event_id(ts, crew_id, int(number), kind, text),
        "ts": ts,
        "crew_id": crew_id,
        "number": int(number),
        "kind": kind,
        "text": text,
    }
    path = events_path(owner, repo, root)
    lock_path = crews_dir(owner, repo, root) / "events.lock"
    with open(lock_path, "w") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            with open(path, "a", encoding="utf-8") as out:
                out.write(json.dumps(entry) + "\n")
    return entry


def read_events(
    owner: str,
    repo: str,
    root: Path | None = None,
    *,
    crew_id: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Newest first, duplicate ids collapsed. A malformed line is skipped rather
    than failing the whole read — the ledger is append-only and a torn tail must
    not hide the history in front of it."""
    path = events_path(owner, repo, root)
    if not path.is_file():
        return []
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        rid = str(rec.get("id") or "")
        if rid and rid in seen:
            continue
        if crew_id and rec.get("crew_id") != crew_id:
            continue
        seen.add(rid)
        out.append(rec)
        if len(out) >= limit:
            break
    return out
