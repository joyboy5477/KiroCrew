"""The agent's read/write path into its own Issue Radar crew ledger.

An Issue Radar crew is an unattended agent whose context does not survive
compaction, its per-turn ceiling, or a gateway restart. The ledger does — so
these two MCP tools are the crew's memory, and every property asserted here is
load-bearing for a cold resume:

  * the ledger must be REACHABLE from an agent session, which has no dashboard
    credential (httpOnly cookie, ``KIROCREW_INTERNAL_SECRET`` stripped from agent
    env, ``.local_secret`` on the sensitive-path denylist) — hence the
    internal-secret allowlist entries, and hence a raw HTTP call earning 403;
  * a partial write must never erase what an earlier write stored, or a resume
    reads back a ledger emptier than the work it describes;
  * a phase must never move without a logged reason, which is why upsert and
    append are ONE tool;
  * the public strings must not carry this machine's identity, because the event
    log feeds a comment on github.com as well as the crew page.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew import mcp_core
from kiro_crew.dashboard.token_auth import token_auth_middleware
from kiro_crew.validation import MCP_CORE_SCHEMAS, ValidationError, validate_tool_args

READ_TOOL = "issue_radar_crew_read"
RECORD_TOOL = "issue_radar_crew_record"
READ_PATH = "/api/apps/issue-radar/crew"
WORK_PATH = "/api/apps/issue-radar/crew/work"

#: What the read route answers with. Identity lives here and ONLY here — no tool
#: argument can select a crew — so the fixture doubles as the contract.
CREW_PAYLOAD = {
    "crew": {"id": "c_7f3a", "name": "Whirlpool", "owner": "acme", "repo": "widget"},
    "settings": {"claim_ttl_hours": 48, "escalation_handback_days": 3},
    "items": [{"crew_id": "c_7f3a", "owner": "acme", "repo": "widget", "number": 12}],
    "events": [],
    "counts": {"open": 1, "escalated": 0},
}


async def _ok_handler(request: web.Request) -> web.Response:
    return web.Response(text="ok")


def _clean(**over):
    args = {"number": 12}
    args.update(over)
    return validate_tool_args(args, MCP_CORE_SCHEMAS[RECORD_TOOL])


def _record(payload: dict | None = None, **over):
    """Invoke the record tool with the HTTP legs stubbed.

    Returns ``(captured, output)`` where ``captured`` holds the GET path and the
    PUT path/body. Module-level rather than a base-class method: a class that
    inherited it would also inherit and re-run that class's tests.
    """
    cleaned = _clean(**over)
    captured: dict = {}

    def fake_get(path):
        captured["get_path"] = path
        return CREW_PAYLOAD if payload is None else payload

    def fake_put(path, body=None):
        captured["put_path"] = path
        captured["body"] = body
        return {"item": {"phase": body.get("phase") or "claimed", "next": body.get("next") or ""}}

    with patch.object(mcp_core, "_get", side_effect=fake_get):
        with patch.object(mcp_core, "_put", side_effect=fake_put):
            out = mcp_core._call_tool_inner(RECORD_TOOL, cleaned)
    return captured, out


class TestGatewayPathsAreReachableWithTheInternalSecret(unittest.TestCase):
    def test_both_crew_routes_are_mixed_internal_paths(self):
        from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS

        assert READ_PATH in _MIXED_INTERNAL_API_PATHS
        assert WORK_PATH in _MIXED_INTERNAL_API_PATHS

    def test_entries_are_full_paths_not_the_apps_prefix(self):
        """A prefix entry would hand the app's forge-write routes to the secret.

        ``token_auth`` matches ``path == p or path.startswith(p + "/")``, so an
        ``/api/apps/issue-radar`` entry would also admit ``/labels/apply``,
        ``/issue/close`` and the comment routes — which write to GitHub — to
        anything holding the internal secret.
        """
        from kiro_crew.dashboard.server import (
            _MIXED_INTERNAL_API_PATHS,
            _STRICT_INTERNAL_API_PATHS,
        )

        for entry in _MIXED_INTERNAL_API_PATHS | _STRICT_INTERNAL_API_PATHS:
            assert entry != "/api/apps"
            assert entry != "/api/apps/issue-radar"
            assert not entry.startswith("/api/apps/issue-radar/labels")
            assert not entry.startswith("/api/apps/issue-radar/issue")
            assert not entry.startswith("/api/apps/issue-radar/comment")

    def test_the_paths_the_tools_call_are_the_paths_allowlisted(self):
        # A route constant that drifts from the allowlist entry is a silent 403
        # for an unattended agent — nobody is watching the turn it happens on.
        from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS

        assert mcp_core._CREW_READ_PATH == READ_PATH
        assert mcp_core._CREW_WORK_PATH == WORK_PATH
        assert mcp_core._CREW_READ_PATH in _MIXED_INTERNAL_API_PATHS
        assert mcp_core._CREW_WORK_PATH in _MIXED_INTERNAL_API_PATHS


class TestToolRegistration(unittest.TestCase):
    def test_both_schemas_are_registered(self):
        # An unregistered tool's args pass through raw and its ValidationError
        # escapes the stdio loop, killing kirocrew-core for the whole session.
        assert READ_TOOL in MCP_CORE_SCHEMAS
        assert RECORD_TOOL in MCP_CORE_SCHEMAS

    def test_both_tools_are_advertised(self):
        listed = {t["name"] for t in mcp_core._list_tools()}
        assert READ_TOOL in listed
        assert RECORD_TOOL in listed

    def test_read_takes_no_arguments_at_all(self):
        # Identity is resolved from the session; if the model could name a crew
        # it could read, then write against, another crew's ledger.
        spec = next(t for t in mcp_core._list_tools() if t["name"] == READ_TOOL)
        assert spec["inputSchema"]["properties"] == {}
        assert MCP_CORE_SCHEMAS[READ_TOOL].fields == []

    def test_record_requires_only_the_issue_number(self):
        required = {f.name for f in MCP_CORE_SCHEMAS[RECORD_TOOL].fields if f.required}
        assert required == {"number"}
        spec = next(t for t in mcp_core._list_tools() if t["name"] == RECORD_TOOL)
        assert spec["inputSchema"]["required"] == ["number"]

    def test_record_advertises_no_identity_arguments(self):
        props = next(
            t for t in mcp_core._list_tools() if t["name"] == RECORD_TOOL
        )["inputSchema"]["properties"]
        for forbidden in ("owner", "repo", "crew_id", "id"):
            assert forbidden not in props
        assert {f.name for f in MCP_CORE_SCHEMAS[RECORD_TOOL].fields}.isdisjoint(
            {"owner", "repo", "crew_id"}
        )

    def test_advertised_schema_matches_the_validated_schema(self):
        # A property the model is told about but the validator rejects is a
        # guaranteed error mid-turn; the reverse is an undiscoverable field.
        spec = next(t for t in mcp_core._list_tools() if t["name"] == RECORD_TOOL)
        assert set(spec["inputSchema"]["properties"]) == {
            f.name for f in MCP_CORE_SCHEMAS[RECORD_TOOL].fields
        }

    def test_advertised_enums_are_the_validated_enums(self):
        spec = next(t for t in mcp_core._list_tools() if t["name"] == RECORD_TOOL)
        props = spec["inputSchema"]["properties"]
        by_name = {f.name: f for f in MCP_CORE_SCHEMAS[RECORD_TOOL].fields}
        assert set(props["phase"]["enum"]) == set(by_name["phase"].allowed)
        assert set(props["event_kind"]["enum"]) == set(by_name["event_kind"].allowed)

    def test_descriptions_warn_that_the_progress_line_is_public(self):
        spec = next(t for t in mcp_core._list_tools() if t["name"] == RECORD_TOOL)
        assert "PUBLIC" in spec["description"]
        assert "403" in spec["description"]

    def test_the_phase_vocabulary_mirrors_the_store_exactly(self):
        # validation.py cannot import an app package, so it mirrors these. Drift
        # would silently reject a legitimate phase at the tool boundary.
        from kiro_crew.apps.builtins.issue_radar.backend import crew_store

        by_name = {f.name: f for f in MCP_CORE_SCHEMAS[RECORD_TOOL].fields}
        assert set(by_name["phase"].allowed) == set(crew_store.PHASES)
        assert set(by_name["event_kind"].allowed) == set(crew_store.EVENT_KINDS)


class TestArgValidation(unittest.TestCase):
    def test_rejects_an_unknown_arg(self):
        # Fail closed on a hallucinated field rather than dropping it silently:
        # a crew that thinks it recorded `blocked_on` has recorded nothing.
        with pytest.raises(ValidationError):
            _clean(blocked_on="review")

    def test_rejects_identity_args(self):
        # Not merely absent from the schema — actively refused, so an attempt to
        # aim a write at another repo or crew errors instead of being ignored.
        for arg, value in (("owner", "other"), ("repo", "other"), ("crew_id", "c_dead")):
            with pytest.raises(ValidationError):
                _clean(**{arg: value})

    def test_read_rejects_any_arg(self):
        with pytest.raises(ValidationError):
            validate_tool_args({"crew_id": "c_7f3a"}, MCP_CORE_SCHEMAS[READ_TOOL])

    def test_rejects_an_unknown_phase(self):
        with pytest.raises(ValidationError):
            _clean(phase="thinking", event="x", event_kind="ci")

    def test_rejects_an_unknown_event_kind(self):
        with pytest.raises(ValidationError):
            _clean(event="did a thing", event_kind="musing")

    def test_a_phase_write_must_carry_its_reason(self):
        # The point of merging upsert+append into one tool: two tools are what
        # let a phase move with nothing logged.
        with pytest.raises(ValidationError):
            _clean(phase="implementing")

    def test_event_and_kind_travel_together(self):
        with pytest.raises(ValidationError):
            _clean(event="CI round 3 — 41/47 green")
        with pytest.raises(ValidationError):
            _clean(event_kind="ci")

    def test_an_escalation_needs_its_question(self):
        with pytest.raises(ValidationError):
            _clean(escalation_options=["rebase", "wait"])
        with pytest.raises(ValidationError):
            _clean(escalation_recommendation="wait for main to go green")

    def test_a_complete_progress_step_validates(self):
        cleaned = _clean(phase="awaiting-ci", event="pushed round 3", event_kind="ci")
        assert cleaned["phase"] == "awaiting-ci"
        assert cleaned["event_kind"] == "ci"

    def test_rejects_a_number_that_would_blow_up_the_filename(self):
        # The number becomes crews/<crew_id>/<n>.json.
        with pytest.raises(ValidationError):
            _clean(number=10**12)

    def test_rejects_a_boolean_number(self):
        # bool is an int subclass; True would otherwise record against #1.
        with pytest.raises(ValidationError):
            _clean(number=True)

    def test_rejects_a_base_sha_that_is_not_a_git_object_name(self):
        with pytest.raises(ValidationError):
            _clean(base_sha="origin/main")

    def test_accepts_an_abbreviated_sha(self):
        assert _clean(base_sha="f2aa4c8")["base_sha"] == "f2aa4c8"


class TestIdentityComesFromTheSession(unittest.TestCase):
    def test_the_put_body_names_owner_repo_and_crew_explicitly(self):
        # Explicit identity is what stops a same-numbered issue in another repo
        # being the record that gets written.
        captured, _ = _record()
        assert captured["get_path"] == READ_PATH
        assert captured["put_path"] == WORK_PATH
        assert captured["body"]["owner"] == "acme"
        assert captured["body"]["repo"] == "widget"
        assert captured["body"]["crew_id"] == "c_7f3a"
        assert captured["body"]["number"] == 12

    def test_identity_is_read_from_a_work_item_when_the_crew_record_omits_it(self):
        payload = {
            "crew": {"id": "c_7f3a", "name": "Whirlpool"},
            "items": [{"crew_id": "c_7f3a", "owner": "acme", "repo": "widget", "number": 12}],
        }
        captured, _ = _record(payload=payload)
        assert (captured["body"]["owner"], captured["body"]["repo"]) == ("acme", "widget")

    def test_a_non_crew_session_is_refused_before_any_write(self):
        captured, out = _record(payload={"crew": {}, "items": []})
        assert out.startswith("Error:")
        assert "not bound to an Issue Radar crew" in out
        assert "put_path" not in captured

    def test_a_read_error_is_surfaced_instead_of_writing_blind(self):
        cleaned = _clean()
        with patch.object(mcp_core, "_get", return_value={"error": "not connected"}):
            with patch.object(mcp_core, "_put") as put:
                out = mcp_core._call_tool_inner(RECORD_TOOL, cleaned)
        assert out.startswith("Error:")
        put.assert_not_called()

    def test_a_write_error_is_surfaced_instead_of_claiming_success(self):
        cleaned = _clean(next="rebase onto main")
        with patch.object(mcp_core, "_get", return_value=CREW_PAYLOAD):
            with patch.object(mcp_core, "_put", return_value={"error": "unknown crew"}):
                out = mcp_core._call_tool_inner(RECORD_TOOL, cleaned)
        assert out.startswith("Error:")
        assert "unknown crew" in out


class TestEmptyFieldsAreDropped(unittest.TestCase):
    """A partial patch must preserve what an earlier write stored."""

    def test_a_minimal_call_sends_identity_and_nothing_else(self):
        captured, _ = _record()
        assert set(captured["body"]) == {"owner", "repo", "crew_id", "number"}

    def test_an_explicitly_empty_field_is_not_sent(self):
        # "" would still be a merge key on the way through; dropping keeps the
        # request honest about what this call is actually asserting.
        captured, _ = _record(next="", decision="", worktree="", branch="")
        assert set(captured["body"]) == {"owner", "repo", "crew_id", "number"}

    def test_only_the_supplied_fields_are_sent(self):
        captured, _ = _record(next="add the Windows branch to _safe_chmod")
        assert set(captured["body"]) == {"owner", "repo", "crew_id", "number", "next"}
        assert captured["body"]["next"] == "add the Windows branch to _safe_chmod"

    def test_a_rejected_approach_carries_its_reason(self):
        captured, _ = _record(
            tried_approach="patch os.fchmod directly",
            tried_rejected_because="Windows has no fchmod",
        )
        body = captured["body"]
        assert body["tried_approach"] == "patch os.fchmod directly"
        assert body["tried_rejected_because"] == "Windows has no fchmod"

    def test_a_reason_with_no_approach_is_not_sent_alone(self):
        # The store only appends a `tried` entry when it sees an approach; a
        # lone reason would be silently discarded there.
        captured, _ = _record(tried_rejected_because="Windows has no fchmod")
        assert "tried_rejected_because" not in captured["body"]

    def test_the_local_resume_fields_survive_verbatim(self):
        captured, _ = _record(
            worktree="/home/user/src/project",
            branch="fix/os-fchmod-windows",
            base_sha="f2aa4c8bb",
        )
        body = captured["body"]
        # NOT scrubbed: an absolute path is the point of this field, it stays
        # local, and a scrubbed one is a resume that cannot find its worktree.
        assert body["worktree"] == "/home/user/src/project"
        assert body["branch"] == "fix/os-fchmod-windows"
        assert body["base_sha"] == "f2aa4c8bb"


class TestCiStateAssembly(unittest.TestCase):
    """The flat ci_* args become the store's ``ci_state`` dict."""

    def test_assembles_every_ci_field(self):
        captured, _ = _record(
            phase="awaiting-ci",
            event="CI round 3 — 41/47 green, 6 inherited",
            event_kind="ci",
            ci_state="failure",
            ci_passed=41,
            ci_total=47,
            ci_round=3,
            ci_inherited_reds=6,
        )
        assert captured["body"]["ci_state"] == {
            "state": "failure",
            "passed": 41,
            "total": 47,
            "round": 3,
            "inherited_reds": 6,
        }

    def test_a_partial_ci_reading_sends_only_what_was_read(self):
        captured, _ = _record(ci_state="pending")
        assert captured["body"]["ci_state"] == {"state": "pending"}

    def test_no_ci_key_at_all_when_nothing_was_read(self):
        captured, _ = _record(next="waiting")
        assert "ci_state" not in captured["body"]

    def test_a_zero_reading_is_kept_not_dropped(self):
        # 0/47 green and 0 inherited reds are both real, load-bearing readings:
        # dropping them on falsiness would leave the previous round's numbers in
        # place and a crew would read them as still-green.
        captured, _ = _record(ci_passed=0, ci_total=47, ci_inherited_reds=0)
        assert captured["body"]["ci_state"] == {
            "passed": 0,
            "total": 47,
            "inherited_reds": 0,
        }

    def test_the_flat_ci_args_never_leak_through_as_themselves(self):
        captured, _ = _record(ci_state="success", ci_passed=47, ci_total=47)
        for leaked in ("ci_passed", "ci_total", "ci_round", "ci_inherited_reds"):
            assert leaked not in captured["body"]


class TestPublicStringsAreSanitizedOnTheWayIn(unittest.TestCase):
    """``event`` and the escalation prose reach a comment on github.com.

    The investigation tool redacts because its prose is re-rendered on a card;
    here the same prose is published, so credentials AND this machine's identity
    must both be gone before the write, not before the render.
    """

    def test_a_credential_quoted_into_a_progress_line_is_not_published(self):
        captured, _ = _record(
            event="log had AKIAIOSFODNN7EXAMPLE in it",
            event_kind="investigate",
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in captured["body"]["event"]

    def test_a_credential_in_the_escalation_prose_is_not_published(self):
        captured, _ = _record(
            escalation_question="is AKIAIOSFODNN7EXAMPLE meant to be in the fixture?",
            escalation_options=["aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"],
            escalation_recommendation="rotate AKIAIOSFODNN7EXAMPLE first",
        )
        esc = captured["body"]["escalation"]
        assert "AKIAIOSFODNN7EXAMPLE" not in esc["question"]
        assert "AKIAIOSFODNN7EXAMPLE" not in esc["recommendation"]
        assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in esc["options"][0]

    def test_the_home_directory_is_scrubbed_from_a_progress_line(self):
        # Redaction alone covers credentials and exfil URLs, NOT paths — and a
        # path is the thing this line must not publish.
        from pathlib import Path

        home = str(Path.home())
        captured, _ = _record(
            event=f"ran the gate in {home}/src/project",
            event_kind="implement",
        )
        assert home not in captured["body"]["event"]
        assert "<home>" in captured["body"]["event"]

    def test_the_kirocrew_home_collapses_before_the_user_home(self):
        # Longest-first ordering: scrubbing the home first would leave
        # "<home>/.kiro/crew/..." — still this machine's directory layout.
        from kiro_crew.config.loader import config_dir

        captured, _ = _record(
            event=f"read the ledger under {config_dir()}/workspace",
            event_kind="investigate",
        )
        assert str(config_dir()) not in captured["body"]["event"]

    def test_the_hostname_is_scrubbed_from_the_escalation_question(self):
        with patch.object(mcp_core.socket, "gethostname", return_value="dev-dsk-example-1a"):
            captured, _ = _record(
                escalation_question="the build only fails on dev-dsk-example-1a",
            )
        assert "dev-dsk-example-1a" not in captured["body"]["escalation"]["question"]
        assert "<host>" in captured["body"]["escalation"]["question"]

    def test_a_short_hostname_is_left_alone(self):
        # Substring-replacing a short hostname corrupts ordinary prose, which is
        # a worse outcome than a name the brief already forbids writing.
        with patch.object(mcp_core.socket, "gethostname", return_value="dev"):
            captured, _ = _record(event="dev server restarted", event_kind="implement")
        assert captured["body"]["event"] == "dev server restarted"

    def test_ordinary_prose_is_left_alone(self):
        captured, _ = _record(
            phase="awaiting-ci",
            event="CI round 3 — 41/47 green, 6 inherited from main",
            event_kind="ci",
            next="rerun the flaky Windows shard",
        )
        body = captured["body"]
        assert body["event"] == "CI round 3 — 41/47 green, 6 inherited from main"
        assert body["next"] == "rerun the flaky Windows shard"

    def test_the_local_worktree_path_is_not_scrubbed(self):
        # The pass that removes the home from a public line must not touch the
        # field whose whole purpose is to carry it.
        from pathlib import Path

        worktree = f"{Path.home()}/src/project"
        captured, _ = _record(worktree=worktree, event="checked out", event_kind="implement")
        assert captured["body"]["worktree"] == worktree

    def test_a_label_carrying_a_credential_is_not_persisted(self):
        captured, _ = _record(labels_applied=["crew: in progress", "AKIAIOSFODNN7EXAMPLE"])
        assert "AKIAIOSFODNN7EXAMPLE" not in captured["body"]["labels_applied"][1]


class TestRecordOutput(unittest.TestCase):
    def test_echoes_the_stored_event_not_the_argument(self):
        # If a sanitizer pass changed the line, the crew must see what actually
        # became public — otherwise it believes it published something else.
        cleaned = _clean(event="pushed round 3", event_kind="ci")
        with patch.object(mcp_core, "_get", return_value=CREW_PAYLOAD):
            with patch.object(
                mcp_core,
                "_put",
                return_value={"item": {"phase": "awaiting-ci"}, "event": {"text": "stored line"}},
            ):
                out = mcp_core._call_tool_inner(RECORD_TOOL, cleaned)
        assert "stored line" in out
        assert "awaiting-ci" in out

    def test_reports_the_next_step_back_so_a_resume_can_be_trusted(self):
        _, out = _record(next="add the Windows branch to _safe_chmod")
        assert "add the Windows branch to _safe_chmod" in out


class TestReadOutput(unittest.TestCase):
    def _read(self, payload):
        with patch.object(mcp_core, "_get", return_value=payload):
            return mcp_core._call_tool_inner(READ_TOOL, {})

    def test_returns_the_crew_settings_and_open_items(self):
        out = json.loads(self._read(CREW_PAYLOAD))
        assert out["crew"]["name"] == "Whirlpool"
        assert out["settings"]["claim_ttl_hours"] == 48
        assert out["open_items"][0]["number"] == 12
        assert out["counts"]["open"] == 1

    def test_bounds_the_event_log_newest_first(self):
        payload = dict(CREW_PAYLOAD)
        payload["events"] = [{"text": f"line {i}"} for i in range(60)]
        out = json.loads(self._read(payload))
        assert len(out["recent_events"]) == mcp_core._CREW_MAX_EVENTS
        assert out["recent_events"][0]["text"] == "line 59"
        assert "60" in out["recent_events_note"]

    def test_a_non_crew_session_is_told_so(self):
        out = self._read({"crew": {}, "items": []})
        assert out.startswith("Error:")

    def test_a_gateway_error_is_surfaced(self):
        out = self._read({"error": "not connected"})
        assert out.startswith("Error:")
        assert "not connected" in out

    def test_the_worktree_path_survives_the_read(self):
        # The resume needs it; output redaction removes credentials, not paths.
        payload = dict(CREW_PAYLOAD)
        payload["items"] = [
            {
                "crew_id": "c_7f3a",
                "owner": "acme",
                "repo": "widget",
                "number": 12,
                "worktree": "/home/user/src/project",
            }
        ]
        out = json.loads(self._read(payload))
        assert out["open_items"][0]["worktree"] == "/home/user/src/project"


class TestMiddlewareDecision:
    """Drive the real auth middleware over the crew paths.

    Set membership alone would still pass if the prefix matcher changed, so
    assert the decision: a credential-less call is refused, the tools' call is
    granted, and the forge-write routes stay closed.
    """

    @staticmethod
    def _request(path: str, method: str, headers: dict | None = None, remote: str = "127.0.0.1"):
        req = MagicMock(spec=web.Request)
        req.path = path
        req.query = {}
        req.cookies = {}
        req.remote = remote
        req.headers = headers or {}
        req.method = method
        return req

    def _mw(self, secret: str = "s3cret"):
        from kiro_crew.dashboard.server import (
            _MIXED_INTERNAL_API_PATHS,
            _STRICT_INTERNAL_API_PATHS,
        )

        return token_auth_middleware(
            internal_paths=_STRICT_INTERNAL_API_PATHS,
            mixed_internal_paths=_MIXED_INTERNAL_API_PATHS,
            internal_secret=secret,
        )

    @pytest.mark.asyncio
    async def test_a_credentialless_read_is_refused(self):
        resp = await self._mw()(self._request(READ_PATH, "GET"), _ok_handler)
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_a_credentialless_write_is_refused(self):
        resp = await self._mw()(self._request(WORK_PATH, "PUT"), _ok_handler)
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_both_tool_calls_are_granted(self):
        for path, method in ((READ_PATH, "GET"), (WORK_PATH, "PUT")):
            resp = await self._mw()(
                self._request(path, method, headers={"X-Internal-Secret": "s3cret"}),
                _ok_handler,
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_non_loopback_with_the_secret_is_refused(self):
        # The secret is a machine-local handshake and a forwarder can make
        # remote traffic look loopback, so a genuinely remote peer never gets in.
        resp = await self._mw()(
            self._request(
                WORK_PATH, "PUT", headers={"X-Internal-Secret": "s3cret"}, remote="10.0.0.1"
            ),
            _ok_handler,
        )
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_the_forge_write_routes_are_not_reachable_with_the_secret(self):
        for path, method in (
            ("/api/apps/issue-radar/labels/apply", "POST"),
            ("/api/apps/issue-radar/issue/close", "POST"),
            ("/api/apps/issue-radar/comment", "POST"),
        ):
            resp = await self._mw()(
                self._request(path, method, headers={"X-Internal-Secret": "s3cret"}),
                _ok_handler,
            )
            assert resp.status == 403
