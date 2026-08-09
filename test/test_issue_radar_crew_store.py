"""Tests for the crew store — records, work items and the event ledger.

The coverage here is deliberately weighted toward the invariants whose failure is
SILENT, because those are the ones that corrupt the claim protocol rather than
raising:

  * **Name reuse.** A retired crew's name still appears in the check-in comments
    it left on the forge, so reusing it makes an old comment look like a live
    claim. Uniqueness is checked in the store because the name field is free text.
  * **``last_progress_at``.** The claim TTL is measured from this field, so a
    read-back or a no-op write must not renew a claim. If it did, a dead crew
    would hold an issue forever and nothing would report it.
  * **One editing item.** Two worktrees with uncommitted changes is how a fix for
    one issue gets committed onto another issue's branch. The store refuses the
    second one; a warning would not.
  * **Slot accounting.** An escalated item must NOT consume a work slot, or a
    crew blocked on a human stops picking up other work.
  * **Ledger dedupe.** Duplicate lines merge on read, so an append retried after a
    crash does not double-report.

Every test runs against a ``tmp_path`` root — the store threads ``root`` through
every function for exactly this reason, so nothing here touches a real data home.
"""

import json

import pytest

from kiro_crew.apps.builtins.issue_radar.backend import crew_store as cs

OWNER, REPO = "kirodotdev", "KiroCrew"  # brand-ok: the repository name


def _crew(root, name="Andromeda", **spec):
    return cs.create_crew(OWNER, REPO, {"name": name, **spec}, root)


# ── crews ───────────────────────────────────────────────────────────────────


def test_create_assigns_id_slot_key_and_seed(tmp_path):
    crew = _crew(tmp_path)
    assert crew["id"].startswith("c_")
    assert crew["slot_key"] == f"crew-{crew['id']}"
    # The seed defaults to the name but is a SEPARATE field, so a later rename
    # keeps the face.
    assert crew["avatar_seed"] == "Andromeda"
    assert crew["schema"] == cs.CREW_SCHEMA
    assert crew["max_open"] == 3 and crew["max_escalated"] == 3
    assert crew["auto_merge"] is True and crew["unattended"] is True


def test_duplicate_name_is_refused(tmp_path):
    _crew(tmp_path)
    with pytest.raises(cs.CrewStoreError, match="already taken"):
        _crew(tmp_path)


def test_retired_crew_keeps_its_name_reserved(tmp_path):
    crew = _crew(tmp_path)
    cs.retire_crew(OWNER, REPO, crew["id"], tmp_path)
    assert cs.list_crews(OWNER, REPO, tmp_path) == []
    assert len(cs.list_crews(OWNER, REPO, tmp_path, include_retired=True)) == 1
    with pytest.raises(cs.CrewStoreError, match="already taken"):
        _crew(tmp_path)


def test_rename_keeps_the_avatar_seed(tmp_path):
    crew = _crew(tmp_path)
    renamed = cs.update_crew(OWNER, REPO, crew["id"], {"name": "Whirlpool"}, tmp_path)
    assert renamed["name"] == "Whirlpool"
    assert renamed["avatar_seed"] == "Andromeda"


def test_unknown_patch_fields_are_dropped(tmp_path):
    crew = _crew(tmp_path)
    updated = cs.update_crew(
        OWNER, REPO, crew["id"], {"max_open": 5, "not_a_field": "x", "unattended": False}, tmp_path
    )
    assert updated["max_open"] == 5
    assert updated["unattended"] is False
    assert "not_a_field" not in updated


def test_out_of_range_limits_are_ignored(tmp_path):
    crew = _crew(tmp_path)
    updated = cs.update_crew(OWNER, REPO, crew["id"], {"max_open": 0}, tmp_path)
    assert updated["max_open"] == 3


def test_suggest_names_skips_taken_and_degrades_when_pool_is_spent(tmp_path):
    _crew(tmp_path, name="Andromeda")
    assert "Andromeda" not in cs.suggest_names(OWNER, REPO, tmp_path)
    for name in cs.NAME_POOL:
        if name != "Andromeda":
            _crew(tmp_path, name=name)
    # Pool exhausted — the degraded form is astronomically correct (Leo II etc.)
    suggestions = cs.suggest_names(OWNER, REPO, tmp_path, limit=2)
    assert len(suggestions) == 2
    assert all(s.endswith(" II") for s in suggestions)


# ── settings ────────────────────────────────────────────────────────────────


def test_settings_default_and_merge(tmp_path):
    assert cs.read_settings(OWNER, REPO, tmp_path)["claim_ttl_hours"] == 48
    cs.write_settings(OWNER, REPO, {"claim_ttl_hours": 72}, tmp_path)
    got = cs.read_settings(OWNER, REPO, tmp_path)
    assert got["claim_ttl_hours"] == 72
    # Untouched keys keep their default rather than disappearing.
    assert got["escalation_handback_days"] == 3


def test_settings_rejects_nonsense(tmp_path):
    cs.write_settings(OWNER, REPO, {"claim_ttl_hours": -5, "commit_trailer": "  "}, tmp_path)
    got = cs.read_settings(OWNER, REPO, tmp_path)
    assert got["claim_ttl_hours"] == 48
    assert got["commit_trailer"] == cs.DEFAULT_SETTINGS["commit_trailer"]


# ── work items ──────────────────────────────────────────────────────────────


def test_upsert_stamps_claimed_at_and_merges_per_field(tmp_path):
    crew = _crew(tmp_path)
    cid = crew["id"]
    first = cs.upsert_work_item(
        OWNER, REPO, cid, 2251, {"phase": "claimed", "next": "read the call sites"}, tmp_path
    )
    assert first["claimed_at"]
    assert first["next"] == "read the call sites"

    # A patch carrying only `decision` must preserve `next`.
    second = cs.upsert_work_item(OWNER, REPO, cid, 2251, {"decision": "fix it"}, tmp_path)
    assert second["decision"] == "fix it"
    assert second["next"] == "read the call sites"
    assert second["claimed_at"] == first["claimed_at"]


def _backdate(tmp_path, cid, number, stamp="2020-01-01T00:00:00Z"):
    """Force a stale ``last_progress_at`` on disk.

    Without this the stamp assertions below are TOOTHLESS: ``_now_iso`` has
    one-second resolution, so two writes inside the same second produce an equal
    stamp whether the code guards it or not, and the "did not renew" test would
    pass even with the guard deleted.
    """
    path = cs.work_item_path(OWNER, REPO, cid, number, tmp_path)
    rec = json.loads(path.read_text())
    rec["last_progress_at"] = stamp
    path.write_text(json.dumps(rec))
    return stamp


def test_no_op_write_does_not_renew_the_claim(tmp_path):
    """The TTL is measured from ``last_progress_at``. A write that carries no
    progress must leave it alone, or a dead crew holds its claim forever."""
    crew = _crew(tmp_path)
    cid = crew["id"]
    cs.upsert_work_item(OWNER, REPO, cid, 2251, {"phase": "claimed"}, tmp_path)
    stale = _backdate(tmp_path, cid, 2251)

    assert cs.upsert_work_item(OWNER, REPO, cid, 2251, {}, tmp_path)["last_progress_at"] == stale
    # Re-asserting the SAME phase is not progress either.
    again = cs.upsert_work_item(OWNER, REPO, cid, 2251, {"phase": "claimed"}, tmp_path)
    assert again["last_progress_at"] == stale
    # Nor is a field that carries no new information.
    same_next = cs.upsert_work_item(OWNER, REPO, cid, 2251, {"next": ""}, tmp_path)
    assert same_next["last_progress_at"] == stale


def test_real_progress_moves_the_stamp(tmp_path):
    crew = _crew(tmp_path)
    cid = crew["id"]
    cs.upsert_work_item(OWNER, REPO, cid, 2251, {"phase": "claimed"}, tmp_path)

    for patch in (
        {"phase": "implementing"},
        {"next": "add the Windows branch"},
        {"pr_number": 2271},
        {"ci_state": {"state": "running", "round": 3}},
        {"tried_approach": "pywin32"},
        {"escalation": {"question": "which behaviour?"}},
    ):
        stale = _backdate(tmp_path, cid, 2251)
        got = cs.upsert_work_item(OWNER, REPO, cid, 2251, patch, tmp_path)
        assert got["last_progress_at"] != stale, f"{patch} should count as progress"


def test_second_editing_item_is_refused(tmp_path):
    crew = _crew(tmp_path)
    cid = crew["id"]
    cs.upsert_work_item(OWNER, REPO, cid, 2251, {"phase": "implementing"}, tmp_path)
    with pytest.raises(cs.CrewStoreError, match="already editing"):
        cs.upsert_work_item(OWNER, REPO, cid, 2264, {"phase": "implementing"}, tmp_path)


def test_editing_slot_frees_when_the_first_item_parks(tmp_path):
    crew = _crew(tmp_path)
    cid = crew["id"]
    cs.upsert_work_item(OWNER, REPO, cid, 2251, {"phase": "implementing"}, tmp_path)
    cs.upsert_work_item(OWNER, REPO, cid, 2251, {"phase": "awaiting-ci"}, tmp_path)
    other = cs.upsert_work_item(OWNER, REPO, cid, 2264, {"phase": "implementing"}, tmp_path)
    assert other["phase"] == "implementing"


def test_staying_in_an_editing_phase_is_not_a_second_editor(tmp_path):
    crew = _crew(tmp_path)
    cid = crew["id"]
    cs.upsert_work_item(OWNER, REPO, cid, 2251, {"phase": "implementing"}, tmp_path)
    again = cs.upsert_work_item(
        OWNER, REPO, cid, 2251, {"phase": "implementing", "next": "keep going"}, tmp_path
    )
    assert again["next"] == "keep going"


def test_unknown_phase_is_refused(tmp_path):
    crew = _crew(tmp_path)
    with pytest.raises(cs.CrewStoreError, match="unknown phase"):
        cs.upsert_work_item(OWNER, REPO, crew["id"], 1, {"phase": "vibing"}, tmp_path)


def test_escalated_item_does_not_consume_a_slot(tmp_path):
    crew = _crew(tmp_path)
    cid = crew["id"]
    cs.upsert_work_item(OWNER, REPO, cid, 1, {"phase": "awaiting-ci"}, tmp_path)
    cs.upsert_work_item(OWNER, REPO, cid, 2, {"phase": "escalated"}, tmp_path)
    assert cs.open_slot_count(OWNER, REPO, cid, tmp_path) == 1
    assert cs.escalated_count(OWNER, REPO, cid, tmp_path) == 1


def test_terminal_phase_frees_the_slot_and_stamps_finished(tmp_path):
    crew = _crew(tmp_path)
    cid = crew["id"]
    cs.upsert_work_item(OWNER, REPO, cid, 1, {"phase": "implementing"}, tmp_path)
    done = cs.upsert_work_item(OWNER, REPO, cid, 1, {"phase": "resolved"}, tmp_path)
    assert done["finished_at"]
    assert cs.open_slot_count(OWNER, REPO, cid, tmp_path) == 0


def test_tried_entries_append_rather_than_replace(tmp_path):
    crew = _crew(tmp_path)
    cid = crew["id"]
    cs.upsert_work_item(
        OWNER, REPO, cid, 1,
        {"tried_approach": "hasattr guard", "tried_rejected_because": "loses the ACL"},
        tmp_path,
    )
    second = cs.upsert_work_item(OWNER, REPO, cid, 1, {"tried_approach": "pywin32"}, tmp_path)
    assert [t["approach"] for t in second["tried"]] == ["hasattr guard", "pywin32"]
    assert second["tried"][0]["rejected_because"] == "loses the ACL"


def test_work_items_are_scoped_per_crew(tmp_path):
    a = _crew(tmp_path, name="Andromeda")
    b = _crew(tmp_path, name="Whirlpool")
    cs.upsert_work_item(OWNER, REPO, a["id"], 2251, {"phase": "implementing"}, tmp_path)
    # Same issue number, different crew — must not collide, and must not trip the
    # one-editor rule, which is per crew.
    cs.upsert_work_item(OWNER, REPO, b["id"], 2251, {"phase": "implementing"}, tmp_path)
    assert cs.read_work_item(OWNER, REPO, a["id"], 2251, tmp_path)["crew_id"] == a["id"]
    assert cs.read_work_item(OWNER, REPO, b["id"], 2251, tmp_path)["crew_id"] == b["id"]


# ── event ledger ────────────────────────────────────────────────────────────


def test_events_read_newest_first_and_filter_by_crew(tmp_path):
    a = _crew(tmp_path, name="Andromeda")
    b = _crew(tmp_path, name="Whirlpool")
    cs.append_event(OWNER, REPO, a["id"], 1, "claim", "claimed", tmp_path)
    cs.append_event(OWNER, REPO, b["id"], 2, "ci", "CI round 3", tmp_path)
    all_events = cs.read_events(OWNER, REPO, tmp_path)
    assert [e["kind"] for e in all_events] == ["ci", "claim"]
    mine = cs.read_events(OWNER, REPO, tmp_path, crew_id=a["id"])
    assert [e["kind"] for e in mine] == ["claim"]


def test_duplicate_event_lines_collapse_on_read(tmp_path):
    crew = _crew(tmp_path)
    entry = cs.append_event(OWNER, REPO, crew["id"], 1, "claim", "claimed", tmp_path)
    # Simulate an append retried after a crash: the same content-addressed id.
    with open(cs.events_path(OWNER, REPO, tmp_path), "a", encoding="utf-8") as fd:
        fd.write(json.dumps(entry) + "\n")
    assert len(cs.read_events(OWNER, REPO, tmp_path)) == 1


def test_malformed_line_does_not_hide_the_history_before_it(tmp_path):
    crew = _crew(tmp_path)
    cs.append_event(OWNER, REPO, crew["id"], 1, "claim", "claimed", tmp_path)
    with open(cs.events_path(OWNER, REPO, tmp_path), "a", encoding="utf-8") as fd:
        fd.write("{ this is a torn tail\n")
    events = cs.read_events(OWNER, REPO, tmp_path)
    assert [e["kind"] for e in events] == ["claim"]


def test_unknown_event_kind_is_refused(tmp_path):
    crew = _crew(tmp_path)
    with pytest.raises(cs.CrewStoreError, match="unknown event kind"):
        cs.append_event(OWNER, REPO, crew["id"], 1, "vibes", "…", tmp_path)


# ── phase classification ────────────────────────────────────────────────────


def test_the_three_phase_classifications_do_not_coincide(tmp_path):
    """This is the point of keeping three separate sets rather than a flag."""
    # awaiting-ci: occupies a slot, is NOT ttl-active, is NOT editing.
    assert "awaiting-ci" not in cs.TTL_ACTIVE_PHASES
    assert "awaiting-ci" not in cs.EDITING_PHASES
    assert "awaiting-ci" not in cs.TERMINAL_PHASES
    # escalated: unfinished, but must not occupy a slot.
    assert "escalated" not in cs.TERMINAL_PHASES
    assert "escalated" not in cs.TTL_ACTIVE_PHASES
    # implementing: all three of ttl-active, editing, slot-occupying.
    assert "implementing" in cs.TTL_ACTIVE_PHASES
    assert "implementing" in cs.EDITING_PHASES
