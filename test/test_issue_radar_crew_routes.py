"""Tests for Issue Radar's crew HTTP surface (``backend/crew_routes.py``).

Every test calls the **registered** handler — looked up out of a real
``web.Application`` by method and path — rather than the bare function. That is
deliberate: it means each happy path also proves the route exists at the verb and
path the contract names, and that it is wrapped in both the ``_require_enabled``
gate and the ``CrewStoreError -> 409`` guard. A route that is written but never
registered, or registered without a gate, fails here instead of in production.

The coverage is weighted toward the conditions whose failure is otherwise silent:

  * **The gates.** All 13 routes are asserted denied when the app is disabled and
    404 when the repo is not connected — as a table, so a route added later
    without a gate fails the inventory test rather than shipping open.
  * **409, never 500.** A duplicate crew name and a second item entering an
    editing phase are ordinary user conditions; surfaced as 500 they read as "the
    backend broke" and a crew agent retries them forever.
  * **The ledger cannot lie.** ``PUT /crew/work`` writes the item and appends the
    event in one call, and a refused write must leave NO ledger line behind.
  * **``working`` is the NEWEST open item**, not any of them — the difference only
    shows up on a crew holding one parked and one active item.
  * **Guidance never 500s.** An unreachable session is a state the UI shows, so
    the route answers 200 with ``injected: false``.

Data isolation is one patch: ``routes._scope`` is the only thing that decides
where crew records land, so pointing it at a ``TemporaryDirectory`` keeps every
store write inside the test. Nothing here touches the network or a real data home.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.issue_radar.backend import (
    crew_routes,
    crew_store,
    provider,
    routes,
    store,
)

BASE = "/api/apps/issue-radar"
OWNER, REPO = "kirodotdev", "KiroCrew"  # brand-ok: the repository name

#: The contract, as a table. Also the inventory the registrar is checked against.
CREW_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/crews"),
    ("POST", "/crews"),
    ("GET", "/crews/names"),
    ("GET", "/crews/settings"),
    ("PUT", "/crews/settings"),
    ("GET", "/crews/escalations"),
    ("GET", "/crew"),
    ("PUT", "/crew"),
    ("DELETE", "/crew"),
    ("PUT", "/crew/work"),
    ("POST", "/crew/pause"),
    ("POST", "/crew/guidance"),
    ("POST", "/issue/comment"),
)

#: A minimally-valid request per route, used by the two table-driven gate tests.
#: Each one would SUCCEED if the gates passed, so a 403/404 can only come from the
#: gate under test rather than from validation firing first.
_MINIMAL: dict[tuple[str, str], dict] = {
    ("GET", "/crews"): {"query": {"owner": OWNER, "repo": REPO}},
    ("POST", "/crews"): {"body": {"owner": OWNER, "repo": REPO, "name": "Andromeda"}},
    ("GET", "/crews/names"): {"query": {"owner": OWNER, "repo": REPO}},
    ("GET", "/crews/settings"): {"query": {"owner": OWNER, "repo": REPO}},
    ("PUT", "/crews/settings"): {
        "body": {"owner": OWNER, "repo": REPO, "settings": {"claim_ttl_hours": 12}}
    },
    ("GET", "/crews/escalations"): {"query": {"owner": OWNER, "repo": REPO}},
    ("GET", "/crew"): {"query": {"owner": OWNER, "repo": REPO, "id": "c_dead"}},
    ("PUT", "/crew"): {"body": {"owner": OWNER, "repo": REPO, "id": "c_dead"}},
    ("DELETE", "/crew"): {"body": {"owner": OWNER, "repo": REPO, "id": "c_dead"}},
    ("PUT", "/crew/work"): {
        "body": {
            "owner": OWNER, "repo": REPO, "crew_id": "c_dead", "number": 7,
            "phase": "claimed", "event": "claimed it", "event_kind": "claim",
        }
    },
    ("POST", "/crew/pause"): {
        "body": {"owner": OWNER, "repo": REPO, "id": "c_dead", "paused": True}
    },
    ("POST", "/crew/guidance"): {
        "body": {"owner": OWNER, "repo": REPO, "id": "c_dead", "number": 7, "text": "do X"}
    },
    ("POST", "/issue/comment"): {
        "body": {"owner": OWNER, "repo": REPO, "number": 7, "body": "hello"}
    },
}


def _registered() -> dict[tuple[str, str], object]:
    """Every crew route the registrar installs, keyed by (method, sub-path)."""
    app = web.Application()
    crew_routes.register_crew_routes(app)
    return {
        (route.method, str(route.resource.canonical)[len(BASE):]): route.handler
        for route in app.router.routes()
    }


def _request(
    method: str, path: str, *, query: dict | None = None, body: object = "",
    app: web.Application | None = None,
) -> web.Request:
    """A real (mocked) aiohttp request for a handler under test.

    aiohttp's own ``make_mocked_request``, not a duck-typed stub: the handlers are
    annotated ``(web.Request) -> web.Response`` and a stand-in fails the mypy gate.
    ``body=None`` models a malformed payload — ``request.json()`` raising is exactly
    what the preamble's ``except -> 400`` branch is written for.
    """
    full = f"{BASE}{path}"
    if query:
        full = full + "?" + "&".join(f"{k}={v}" for k, v in query.items())
    kwargs = {"app": app} if app is not None else {}
    req = make_mocked_request(method, full, **kwargs)  # type: ignore[arg-type]
    if body is None:
        req.json = AsyncMock(side_effect=ValueError("not json"))  # type: ignore[method-assign]
    elif body != "":
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _payload(response: web.Response) -> dict:
    raw = response.body
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


class _Slot:
    """A crew's chat slot, stubbed at the one method the route uses.

    ``enqueue_or_run_prompt`` returns True when it STARTED a turn and False when it
    queued — the route maps that onto ``queued``, so the stub honours the same
    contract instead of always returning True.
    """

    def __init__(self, running: bool = False) -> None:
        self.running = running
        self.prompts: list[str] = []

    def enqueue_or_run_prompt(self, prompt: str, run_chat, state) -> bool:
        self.prompts.append(prompt)
        return not self.running


class _State:
    """The dashboard state, stubbed at the two members the route touches."""

    def __init__(self, slot: _Slot | None = None) -> None:
        self._slot = slot
        self.pushes = 0

    def get_slot(self, key: str) -> _Slot | None:
        return self._slot

    def push_slots_update(self) -> None:
        self.pushes += 1


class _CrewRouteCase(unittest.IsolatedAsyncioTestCase):
    """Base: a temp data root, an enabled app, and a connected repo."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        # _scope is the single place that decides which data root a request's store
        # calls land in, so this one patch isolates every write in the module.
        for patcher in (
            mock.patch.object(routes, "_scope", return_value=self.root),
            mock.patch.object(routes, "is_app_enabled", return_value=True),
            mock.patch.object(store, "is_repo_connected", return_value=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def call(
        self, method: str, path: str, *, query: dict | None = None, body: object = "",
        app: web.Application | None = None,
    ) -> web.Response:
        handler = _registered()[(method, path)]
        return await handler(_request(method, path, query=query, body=body, app=app))  # type: ignore[operator]

    # ── fixtures written straight through the store ──────────────────────────

    def crew(self, name: str = "Andromeda", **spec) -> dict:
        return crew_store.create_crew(OWNER, REPO, {"name": name, **spec}, self.root)

    def work(self, crew_id: str, number: int, phase: str) -> dict:
        return crew_store.upsert_work_item(
            OWNER, REPO, crew_id, number, {"phase": phase}, self.root
        )

    def ledger(self, crew_id: str = "") -> list[dict]:
        return crew_store.read_events(OWNER, REPO, self.root, crew_id=crew_id)


# ── registration and the two gates ──────────────────────────────────────────


class TestRegistrationAndGates(_CrewRouteCase):
    def test_the_registrar_installs_exactly_the_documented_route_table(self):
        # An inventory, not a spot check: a route dropped by a bad merge, or one
        # added at a path the frontend does not call, both show up here.
        self.assertEqual(sorted(_registered()), sorted(CREW_ROUTES))

    async def test_every_route_is_denied_while_the_app_is_disabled(self):
        # Routes register ONCE at gateway startup and the app is
        # defaultEnabled:false, so an unwrapped handler stays callable while the
        # app is switched off.
        with mock.patch.object(routes, "is_app_enabled", return_value=False):
            for method, path in CREW_ROUTES:
                with self.subTest(route=f"{method} {path}"):
                    res = await self.call(method, path, **_MINIMAL[(method, path)])
                    self.assertEqual(res.status, 403)
                    self.assertIn("disabled", _payload(res)["error"])

    async def test_every_route_rejects_a_repo_that_is_not_connected(self):
        with mock.patch.object(store, "is_repo_connected", return_value=False):
            for method, path in CREW_ROUTES:
                with self.subTest(route=f"{method} {path}"):
                    res = await self.call(method, path, **_MINIMAL[(method, path)])
                    self.assertEqual(res.status, 404)
                    self.assertEqual(_payload(res)["code"], "repo_not_connected")

    async def test_a_malformed_body_is_400_not_500(self):
        for method, path in CREW_ROUTES:
            if "body" not in _MINIMAL[(method, path)]:
                continue
            with self.subTest(route=f"{method} {path}"):
                res = await self.call(method, path, body=None)
                self.assertEqual(res.status, 400)
                self.assertEqual(_payload(res)["code"], "invalid_json")

    async def test_a_missing_repo_is_400_before_anything_else(self):
        self.assertEqual((await self.call("GET", "/crews")).status, 400)
        res = await self.call("POST", "/crews", body={"name": "Andromeda"})
        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "missing_repo")

    async def test_local_crew_routes_do_not_require_write_access(self):
        # The write-permission decision, asserted rather than described: a
        # read-only repo can still hold crews, because nothing on this path
        # reaches the forge and _repo_can_write fails CLOSED on a transient error.
        with mock.patch.object(routes, "_repo_can_write", return_value=None) as gate:
            res = await self.call(
                "POST", "/crews", body={"owner": OWNER, "repo": REPO, "name": "Andromeda"}
            )
        self.assertEqual(res.status, 200)
        gate.assert_not_called()


# ── GET /crews ──────────────────────────────────────────────────────────────


class TestCrewsList(_CrewRouteCase):
    async def test_returns_crews_settings_and_counts(self):
        self.crew("Andromeda")
        res = await self.call("GET", "/crews", query={"owner": OWNER, "repo": REPO})
        self.assertEqual(res.status, 200)
        page = _payload(res)
        self.assertEqual(page["owner"], OWNER)
        self.assertEqual(page["repo"], REPO)
        self.assertEqual([c["name"] for c in page["crews"]], ["Andromeda"])
        self.assertEqual(page["settings"]["claim_ttl_hours"], 48)
        # A new crew is enabled and holds nothing: on duty, and nothing else.
        self.assertEqual(
            page["counts"], {"on_duty": 1, "working": 0, "needs_you": 0, "paused": 0}
        )
        self.assertEqual(page["crews"][0]["status"], "idle")

    async def test_working_reads_the_newest_open_item_not_any_of_them(self):
        # The crew holds one item parked on CI and one being implemented. "Any
        # non-parked item" and "the newest item" agree here only because the
        # implement came second — which is the whole point of the rule.
        crew = self.crew("Andromeda")
        self.work(crew["id"], 1, "awaiting-ci")
        self.work(crew["id"], 2, "implementing")
        counts = _payload(
            await self.call("GET", "/crews", query={"owner": OWNER, "repo": REPO})
        )["counts"]
        self.assertEqual(counts["working"], 1)

    async def test_a_crew_parked_on_its_newest_item_is_not_working(self):
        crew = self.crew("Andromeda")
        self.work(crew["id"], 1, "implementing")
        self.work(crew["id"], 2, "awaiting-ci")
        page = _payload(await self.call("GET", "/crews", query={"owner": OWNER, "repo": REPO}))
        self.assertEqual(page["counts"]["working"], 0)
        self.assertEqual(page["crews"][0]["status"], "idle")

    async def test_a_retired_crew_is_neither_listed_nor_on_duty(self):
        crew = self.crew("Andromeda")
        crew_store.retire_crew(OWNER, REPO, crew["id"], self.root)
        page = _payload(await self.call("GET", "/crews", query={"owner": OWNER, "repo": REPO}))
        self.assertEqual(page["crews"], [])
        # Retired is NOT paused: the crew is gone, not stopped.
        self.assertEqual(
            page["counts"], {"on_duty": 0, "working": 0, "needs_you": 0, "paused": 0}
        )

    async def test_the_counts_are_independent_predicates_not_a_partition(self):
        working = self.crew("Andromeda")
        self.work(working["id"], 1, "implementing")
        blocked = self.crew("Whirlpool")
        self.work(blocked["id"], 2, "escalated")
        paused = self.crew("Sombrero", enabled=False)
        self.work(paused["id"], 3, "implementing")
        page = _payload(await self.call("GET", "/crews", query={"owner": OWNER, "repo": REPO}))
        # The paused crew still holds an in-flight item, so it is counted twice —
        # each chip is its own filter, so the numbers may sum past the crew count.
        self.assertEqual(
            page["counts"], {"on_duty": 3, "working": 2, "needs_you": 1, "paused": 1}
        )
        by_name = {c["name"]: c["status"] for c in page["crews"]}
        self.assertEqual(by_name["Andromeda"], "working")
        self.assertEqual(by_name["Whirlpool"], "needs_you")
        self.assertEqual(by_name["Sombrero"], "paused")


# ── POST /crews, GET /crews/names ───────────────────────────────────────────


class TestCreateAndNames(_CrewRouteCase):
    async def test_create_returns_the_crew(self):
        res = await self.call(
            "POST", "/crews", body={"owner": OWNER, "repo": REPO, "name": "Andromeda", "max_open": 5}
        )
        self.assertEqual(res.status, 200)
        crew = _payload(res)["crew"]
        self.assertTrue(crew["id"].startswith("c_"))
        self.assertEqual(crew["slot_key"], f"crew-{crew['id']}")
        self.assertEqual(crew["max_open"], 5)

    async def test_create_without_a_name_is_400(self):
        res = await self.call("POST", "/crews", body={"owner": OWNER, "repo": REPO})
        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "name_required")

    async def test_a_duplicate_name_is_409_with_the_stores_own_message(self):
        self.crew("Andromeda")
        res = await self.call(
            "POST", "/crews", body={"owner": OWNER, "repo": REPO, "name": "Andromeda"}
        )
        self.assertEqual(res.status, 409)
        self.assertIn("already taken", _payload(res)["error"])
        self.assertEqual(_payload(res)["code"], "crew_conflict")

    async def test_a_retired_crews_name_is_still_taken(self):
        crew = self.crew("Andromeda")
        crew_store.retire_crew(OWNER, REPO, crew["id"], self.root)
        res = await self.call(
            "POST", "/crews", body={"owner": OWNER, "repo": REPO, "name": "Andromeda"}
        )
        self.assertEqual(res.status, 409)

    async def test_names_suggests_unused_pool_names(self):
        self.crew("Andromeda")
        res = await self.call("GET", "/crews/names", query={"owner": OWNER, "repo": REPO})
        suggestions = _payload(res)["suggestions"]
        self.assertEqual(len(suggestions), 6)
        self.assertNotIn("Andromeda", suggestions)

    async def test_the_suggestion_limit_is_clamped(self):
        res = await self.call(
            "GET", "/crews/names", query={"owner": OWNER, "repo": REPO, "limit": "9999"}
        )
        self.assertLessEqual(len(_payload(res)["suggestions"]), crew_routes._MAX_SUGGESTIONS)


# ── GET/PUT/DELETE /crew ────────────────────────────────────────────────────


class TestCrewReadUpdateRetire(_CrewRouteCase):
    async def test_read_returns_the_crew_its_items_events_and_counts(self):
        crew = self.crew("Andromeda")
        self.work(crew["id"], 7, "implementing")
        self.work(crew["id"], 8, "escalated")
        crew_store.append_event(OWNER, REPO, crew["id"], 7, "claim", "took #7", self.root)
        res = await self.call(
            "GET", "/crew", query={"owner": OWNER, "repo": REPO, "id": crew["id"]}
        )
        self.assertEqual(res.status, 200)
        page = _payload(res)
        self.assertEqual(page["crew"]["name"], "Andromeda")
        self.assertEqual(sorted(it["number"] for it in page["items"]), [7, 8])
        self.assertEqual([e["text"] for e in page["events"]], ["took #7"])
        # An escalated item waits on a human and must NOT consume a work slot.
        self.assertEqual(page["counts"], {"open": 1, "escalated": 1})

    async def test_read_of_an_unknown_crew_is_404_not_409(self):
        res = await self.call(
            "GET", "/crew", query={"owner": OWNER, "repo": REPO, "id": "c_nope"}
        )
        self.assertEqual(res.status, 404)
        self.assertEqual(_payload(res)["code"], "crew_not_found")

    async def test_read_without_an_id_is_400(self):
        res = await self.call("GET", "/crew", query={"owner": OWNER, "repo": REPO})
        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "missing_crew_id")

    async def test_a_retired_crews_page_still_opens(self):
        crew = self.crew("Andromeda")
        crew_store.retire_crew(OWNER, REPO, crew["id"], self.root)
        res = await self.call(
            "GET", "/crew", query={"owner": OWNER, "repo": REPO, "id": crew["id"]}
        )
        self.assertEqual(res.status, 200)
        self.assertTrue(_payload(res)["crew"]["retired_at"])

    async def test_update_merges_a_patch_and_keeps_the_face(self):
        crew = self.crew("Andromeda")
        res = await self.call(
            "PUT", "/crew",
            body={"owner": OWNER, "repo": REPO, "id": crew["id"], "name": "Whirlpool",
                  "auto_merge": False, "not_a_field": "x"},
        )
        self.assertEqual(res.status, 200)
        updated = _payload(res)["crew"]
        self.assertEqual(updated["name"], "Whirlpool")
        self.assertEqual(updated["auto_merge"], False)
        self.assertEqual(updated["avatar_seed"], "Andromeda")
        self.assertNotIn("not_a_field", updated)

    async def test_a_rename_onto_a_taken_name_is_409(self):
        first = self.crew("Andromeda")
        self.crew("Whirlpool")
        res = await self.call(
            "PUT", "/crew", body={"owner": OWNER, "repo": REPO, "id": first["id"], "name": "Whirlpool"}
        )
        self.assertEqual(res.status, 409)

    async def test_delete_retires_and_keeps_the_record(self):
        crew = self.crew("Andromeda")
        res = await self.call(
            "DELETE", "/crew", body={"owner": OWNER, "repo": REPO, "id": crew["id"]}
        )
        self.assertEqual(res.status, 200)
        retired = _payload(res)["crew"]
        self.assertTrue(retired["retired_at"])
        self.assertIs(retired["enabled"], False)
        # The record survives — the name stays reserved and the work log readable.
        self.assertIsNotNone(crew_store.read_crew(OWNER, REPO, crew["id"], self.root))


# ── PUT /crew/work ──────────────────────────────────────────────────────────


class TestWorkItems(_CrewRouteCase):
    async def test_it_upserts_the_item_and_logs_one_event_in_one_call(self):
        crew = self.crew("Andromeda")
        res = await self.call(
            "PUT", "/crew/work",
            body={
                "owner": OWNER, "repo": REPO, "crew_id": crew["id"], "number": 7,
                "phase": "implementing", "next": "write the failing test",
                "event": "reproduced the crash", "event_kind": "implement",
            },
        )
        self.assertEqual(res.status, 200)
        page = _payload(res)
        self.assertEqual(page["item"]["phase"], "implementing")
        self.assertEqual(page["item"]["next"], "write the failing test")
        self.assertEqual(page["event"]["kind"], "implement")
        self.assertEqual(page["event"]["text"], "reproduced the crash")
        self.assertEqual(page["event"]["number"], 7)
        self.assertEqual(len(self.ledger(crew["id"])), 1)

    async def test_the_envelope_keys_do_not_leak_into_the_work_item(self):
        crew = self.crew("Andromeda")
        item = _payload(
            await self.call(
                "PUT", "/crew/work",
                body={
                    "owner": OWNER, "repo": REPO, "crew_id": crew["id"], "number": 7,
                    "phase": "claimed", "event": "took it", "event_kind": "claim",
                },
            )
        )["item"]
        self.assertNotIn("event", item)
        self.assertNotIn("event_kind", item)

    async def test_a_write_without_an_event_is_refused(self):
        # The reason this route exists: a phase must not change without a logged
        # reason, so the log line is not optional.
        crew = self.crew("Andromeda")
        res = await self.call(
            "PUT", "/crew/work",
            body={"owner": OWNER, "repo": REPO, "crew_id": crew["id"], "number": 7,
                  "phase": "implementing", "event_kind": "implement"},
        )
        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "event_required")
        self.assertIsNone(crew_store.read_work_item(OWNER, REPO, crew["id"], 7, self.root))

    async def test_an_unknown_event_kind_is_400_and_writes_nothing(self):
        crew = self.crew("Andromeda")
        res = await self.call(
            "PUT", "/crew/work",
            body={"owner": OWNER, "repo": REPO, "crew_id": crew["id"], "number": 7,
                  "phase": "implementing", "event": "x", "event_kind": "vibes"},
        )
        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "invalid_event_kind")
        # Validated BEFORE the upsert, so the item was never written.
        self.assertIsNone(crew_store.read_work_item(OWNER, REPO, crew["id"], 7, self.root))

    async def test_a_bad_number_is_400(self):
        crew = self.crew("Andromeda")
        for number in (0, -1, True, "7"):
            with self.subTest(number=number):
                res = await self.call(
                    "PUT", "/crew/work",
                    body={"owner": OWNER, "repo": REPO, "crew_id": crew["id"],
                          "number": number, "event": "x", "event_kind": "claim"},
                )
                self.assertEqual(res.status, 400)

    async def test_a_second_editing_item_is_409(self):
        crew = self.crew("Andromeda")
        await self.call(
            "PUT", "/crew/work",
            body={"owner": OWNER, "repo": REPO, "crew_id": crew["id"], "number": 7,
                  "phase": "implementing", "event": "started #7", "event_kind": "implement"},
        )
        res = await self.call(
            "PUT", "/crew/work",
            body={"owner": OWNER, "repo": REPO, "crew_id": crew["id"], "number": 8,
                  "phase": "implementing", "event": "started #8", "event_kind": "implement"},
        )
        self.assertEqual(res.status, 409)
        self.assertIn("already editing #7", _payload(res)["error"])

    async def test_a_refused_write_leaves_no_ledger_line(self):
        # Ordering proof. The event is appended only AFTER the item write returns,
        # so the 409 above cannot leave the append-only log asserting a phase
        # change that never happened.
        crew = self.crew("Andromeda")
        await self.call(
            "PUT", "/crew/work",
            body={"owner": OWNER, "repo": REPO, "crew_id": crew["id"], "number": 7,
                  "phase": "implementing", "event": "started #7", "event_kind": "implement"},
        )
        await self.call(
            "PUT", "/crew/work",
            body={"owner": OWNER, "repo": REPO, "crew_id": crew["id"], "number": 8,
                  "phase": "implementing", "event": "started #8", "event_kind": "implement"},
        )
        texts = [e["text"] for e in self.ledger(crew["id"])]
        self.assertEqual(texts, ["started #7"])

    async def test_an_unknown_crew_is_404(self):
        res = await self.call(
            "PUT", "/crew/work",
            body={"owner": OWNER, "repo": REPO, "crew_id": "c_nope", "number": 7,
                  "event": "x", "event_kind": "claim"},
        )
        self.assertEqual(res.status, 404)

    async def test_a_retired_crew_cannot_take_new_work(self):
        crew = self.crew("Andromeda")
        crew_store.retire_crew(OWNER, REPO, crew["id"], self.root)
        res = await self.call(
            "PUT", "/crew/work",
            body={"owner": OWNER, "repo": REPO, "crew_id": crew["id"], "number": 7,
                  "phase": "claimed", "event": "took it", "event_kind": "claim"},
        )
        self.assertEqual(res.status, 409)
        self.assertEqual(_payload(res)["code"], "crew_retired")


# ── POST /crew/pause ────────────────────────────────────────────────────────


class TestPause(_CrewRouteCase):
    async def test_pausing_stores_the_reason(self):
        crew = self.crew("Andromeda")
        res = await self.call(
            "POST", "/crew/pause",
            body={"owner": OWNER, "repo": REPO, "id": crew["id"], "paused": True,
                  "reason": "waiting on the release"},
        )
        self.assertEqual(res.status, 200)
        paused = _payload(res)["crew"]
        self.assertIs(paused["enabled"], False)
        self.assertEqual(paused["paused_reason"], "waiting on the release")

    async def test_resuming_clears_the_reason(self):
        crew = self.crew("Andromeda")
        await self.call(
            "POST", "/crew/pause",
            body={"owner": OWNER, "repo": REPO, "id": crew["id"], "paused": True, "reason": "hold"},
        )
        resumed = _payload(
            await self.call(
                "POST", "/crew/pause",
                body={"owner": OWNER, "repo": REPO, "id": crew["id"], "paused": False},
            )
        )["crew"]
        self.assertIs(resumed["enabled"], True)
        # A stale reason on a running crew would explain why it is stopped.
        self.assertEqual(resumed["paused_reason"], "")

    async def test_paused_must_be_a_boolean(self):
        crew = self.crew("Andromeda")
        res = await self.call(
            "POST", "/crew/pause",
            body={"owner": OWNER, "repo": REPO, "id": crew["id"], "paused": "yes"},
        )
        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "invalid_paused")


# ── /crews/settings ─────────────────────────────────────────────────────────


class TestSettings(_CrewRouteCase):
    async def test_get_returns_defaults_for_a_never_configured_repo(self):
        res = await self.call("GET", "/crews/settings", query={"owner": OWNER, "repo": REPO})
        self.assertEqual(res.status, 200)
        settings = _payload(res)["settings"]
        self.assertEqual(settings["claim_ttl_hours"], 48)
        self.assertEqual(settings["escalation_handback_days"], 3)

    async def test_put_merges_and_leaves_untouched_fields_alone(self):
        first = _payload(
            await self.call(
                "PUT", "/crews/settings",
                body={"owner": OWNER, "repo": REPO, "settings": {"claim_ttl_hours": 12}},
            )
        )["settings"]
        self.assertEqual(first["claim_ttl_hours"], 12)

        second = _payload(
            await self.call(
                "PUT", "/crews/settings",
                body={"owner": OWNER, "repo": REPO, "settings": {"commit_trailer": "Crew: {name}"}},
            )
        )["settings"]
        # A partial patch MERGES — which is why this route needs no revision
        # precondition: it cannot discard a field it did not send.
        self.assertEqual(second["claim_ttl_hours"], 12)
        self.assertEqual(second["commit_trailer"], "Crew: {name}")
        self.assertEqual(second["escalation_handback_days"], 3)
        self.assertEqual(
            _payload(
                await self.call("GET", "/crews/settings", query={"owner": OWNER, "repo": REPO})
            )["settings"],
            second,
        )

    async def test_put_requires_a_settings_object(self):
        for value in (None, "12", 12, ["a"]):
            with self.subTest(settings=value):
                res = await self.call(
                    "PUT", "/crews/settings",
                    body={"owner": OWNER, "repo": REPO, "settings": value},
                )
                self.assertEqual(res.status, 400)
                self.assertEqual(_payload(res)["code"], "invalid_settings")


# ── GET /crews/escalations ──────────────────────────────────────────────────


class TestEscalations(_CrewRouteCase):
    async def test_each_row_pairs_the_item_with_its_whole_crew(self):
        crew = self.crew("Andromeda")
        self.work(crew["id"], 7, "escalated")
        self.work(crew["id"], 8, "implementing")
        res = await self.call("GET", "/crews/escalations", query={"owner": OWNER, "repo": REPO})
        self.assertEqual(res.status, 200)
        rows = _payload(res)["escalations"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["item"]["number"], 7)
        # The whole record, not an id: the desk renders the name and avatar next to
        # the escalation, and a lookup per row is a request per escalation.
        self.assertEqual(rows[0]["crew"]["name"], "Andromeda")
        self.assertEqual(rows[0]["crew"]["avatar_seed"], "Andromeda")

    async def test_the_longest_waiting_escalation_comes_first(self):
        first = self.crew("Andromeda")
        self.work(first["id"], 7, "escalated")
        second = self.crew("Whirlpool")
        self.work(second["id"], 9, "escalated")
        rows = _payload(
            await self.call("GET", "/crews/escalations", query={"owner": OWNER, "repo": REPO})
        )["escalations"]
        # Oldest last_progress_at first: the handback timeout is measured from that
        # field, so this is also the order in which these will expire.
        self.assertEqual([r["item"]["number"] for r in rows], [7, 9])

    async def test_a_repo_with_nothing_escalated_answers_an_empty_list(self):
        crew = self.crew("Andromeda")
        self.work(crew["id"], 7, "implementing")
        rows = _payload(
            await self.call("GET", "/crews/escalations", query={"owner": OWNER, "repo": REPO})
        )["escalations"]
        self.assertEqual(rows, [])


# ── POST /crew/guidance ─────────────────────────────────────────────────────


class TestGuidance(_CrewRouteCase):
    def _app(self, state: object | None) -> web.Application:
        app = web.Application()
        app["state"] = state
        return app

    async def _guide(self, crew: dict, state: object | None, text: str = "use the other API"):
        return await self.call(
            "POST", "/crew/guidance",
            body={"owner": OWNER, "repo": REPO, "id": crew["id"], "number": 7, "text": text},
            app=self._app(state),
        )

    async def test_it_injects_into_the_crews_resident_slot(self):
        crew = self.crew("Andromeda")
        slot = _Slot()
        state = _State(slot)
        res = await self._guide(crew, state)
        self.assertEqual(res.status, 200)
        self.assertEqual(
            _payload(res), {"ok": True, "injected": True, "queued": False, "reason": ""}
        )
        # Tagged, and carrying the issue number, so the agent can tell a human
        # directive from its own per-cycle nudge.
        self.assertEqual(len(slot.prompts), 1)
        self.assertIn("GUIDANCE FROM THE HUMAN", slot.prompts[0])
        self.assertIn("#7", slot.prompts[0])
        self.assertIn("use the other API", slot.prompts[0])
        self.assertEqual(state.pushes, 1)

    async def test_a_busy_slot_queues_rather_than_dropping_the_guidance(self):
        crew = self.crew("Andromeda")
        slot = _Slot(running=True)
        res = await self._guide(crew, _State(slot))
        self.assertEqual(_payload(res), {"ok": True, "injected": True, "queued": True, "reason": ""})
        self.assertEqual(len(slot.prompts), 1)

    async def test_an_archived_session_is_rehydrated_with_adopt_closed(self):
        # An idle worker slot is archived with closed=True, and its lifecycle
        # belongs to the app rather than to a tab the user closed — so the
        # rehydration must opt into adopting it.
        crew = self.crew("Andromeda")
        slot = _Slot()
        with mock.patch(
            "kiro_crew.dashboard.chat_persistence.rehydrate_slot_from_history_async",
            new=AsyncMock(return_value=slot),
        ) as rehydrate:
            res = await self._guide(crew, _State(None))
        self.assertTrue(_payload(res)["injected"])
        self.assertEqual(rehydrate.await_args.args[1], crew["slot_key"])
        self.assertIs(rehydrate.await_args.kwargs["adopt_closed"], True)
        self.assertEqual(len(slot.prompts), 1)

    async def test_an_unreachable_session_is_200_with_injected_false(self):
        crew = self.crew("Andromeda")
        with mock.patch(
            "kiro_crew.dashboard.chat_persistence.rehydrate_slot_from_history_async",
            new=AsyncMock(return_value=None),
        ):
            res = await self._guide(crew, _State(None))
        self.assertEqual(res.status, 200)
        payload = _payload(res)
        self.assertIs(payload["injected"], False)
        self.assertIs(payload["ok"], True)
        self.assertTrue(payload["reason"])

    async def test_no_dashboard_state_is_injected_false_not_500(self):
        crew = self.crew("Andromeda")
        res = await self._guide(crew, None)
        self.assertEqual(res.status, 200)
        self.assertIs(_payload(res)["injected"], False)

    async def test_a_failure_inside_the_dashboard_is_injected_false_not_500(self):
        crew = self.crew("Andromeda")
        state = _State(None)
        with mock.patch(
            "kiro_crew.dashboard.chat_persistence.rehydrate_slot_from_history_async",
            new=AsyncMock(side_effect=RuntimeError("slot table exploded")),
        ):
            res = await self._guide(crew, state)
        self.assertEqual(res.status, 200)
        self.assertIs(_payload(res)["injected"], False)

    async def test_guidance_is_never_written_to_the_public_ledger(self):
        # A ledger line's text is rendered inside the claim comment ON THE FORGE.
        # The human's words are private to this dashboard.
        crew = self.crew("Andromeda")
        await self._guide(crew, _State(_Slot()), text="the customer's name is Ada")
        self.assertEqual(self.ledger(crew["id"]), [])

    async def test_empty_text_is_400(self):
        crew = self.crew("Andromeda")
        res = await self.call(
            "POST", "/crew/guidance",
            body={"owner": OWNER, "repo": REPO, "id": crew["id"], "number": 7, "text": "   "},
            app=self._app(_State(_Slot())),
        )
        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "text_required")

    async def test_a_retired_crew_cannot_be_guided(self):
        crew = self.crew("Andromeda")
        crew_store.retire_crew(OWNER, REPO, crew["id"], self.root)
        res = await self._guide(crew, _State(_Slot()))
        self.assertEqual(res.status, 409)


# ── POST /issue/comment (the one forge write) ───────────────────────────────


class _StubClient:
    def __init__(self, result: dict | None = None, error: Exception | None = None) -> None:
        self.result = result or {"id": 4242, "url": "https://example.invalid/c/4242"}
        self.error = error
        self.issue_calls: list[tuple] = []
        self.pr_calls: list[tuple] = []

    def add_issue_comment(self, owner, repo, number, body, **kwargs):
        self.issue_calls.append((owner, repo, number, body, kwargs))
        if self.error is not None:
            raise self.error
        return self.result

    def add_pr_comment(self, owner, repo, number, body, **kwargs):
        self.pr_calls.append((owner, repo, number, body, kwargs))
        return self.result


class TestIssueComment(_CrewRouteCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = _StubClient()
        for patcher in (
            mock.patch.object(routes, "_repo_can_write", return_value=True),
            mock.patch.object(provider, "client_for", return_value=self.client),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def _comment(self, **overrides):
        body = {"owner": OWNER, "repo": REPO, "number": 7, "body": "claiming this"}
        body.update(overrides)
        return await self.call("POST", "/issue/comment", body=body)

    async def test_it_posts_and_returns_the_comment_id(self):
        res = await self._comment()
        self.assertEqual(res.status, 200)
        payload = _payload(res)
        # The id is what the claim protocol stores as claim_comment_id and EDITS on
        # later check-ins instead of posting again.
        self.assertEqual(payload["comment_id"], 4242)
        self.assertEqual(payload["number"], 7)
        self.assertEqual(payload["owner"], OWNER)

    async def test_it_calls_the_issue_function_not_the_pull_request_one(self):
        # On GitLab issues and merge requests are separate collections with
        # independent numbering, so add_pr_comment here would comment on an
        # unrelated merge request that happens to share the number.
        await self._comment()
        self.assertEqual(len(self.client.issue_calls), 1)
        self.assertEqual(self.client.pr_calls, [])
        owner, repo, number, body, _kwargs = self.client.issue_calls[0]
        self.assertEqual((owner, repo, number, body), (OWNER, REPO, 7, "claiming this"))

    async def test_a_read_only_repo_is_403(self):
        with mock.patch.object(routes, "_repo_can_write", return_value=False):
            res = await self._comment()
        self.assertEqual(res.status, 403)
        self.assertEqual(_payload(res)["code"], "repo_read_only")
        self.assertEqual(self.client.issue_calls, [])

    async def test_an_undeterminable_permission_fails_closed(self):
        with mock.patch.object(routes, "_repo_can_write", return_value=None):
            res = await self._comment()
        self.assertEqual(res.status, 403)

    async def test_a_provider_refusal_is_403_and_other_failures_are_502(self):
        cases = ((routes.GhPermissionError("nope"), 403), (routes.GhCliError("timeout"), 502))
        for error, status in cases:
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(
                    provider, "client_for", return_value=_StubClient(error=error)
                ):
                    res = await self._comment()
                self.assertEqual(res.status, status)

    async def test_an_empty_body_is_400(self):
        res = await self._comment(body="   ")
        self.assertEqual(res.status, 400)
        self.assertEqual(_payload(res)["code"], "body_required")

    async def test_the_cached_timeline_is_dropped_so_the_claim_reads_back(self):
        # Without this the crew's next timeline read is served the pre-comment
        # cache, concludes its claim never posted, and posts a second one.
        store.write_issue_detail_cache(
            OWNER, REPO, 7, {"number": 7}, [], root=self.root
        )
        path = store.issue_detail_cache_path(OWNER, REPO, 7, self.root)
        self.assertTrue(path.is_file())
        await self._comment()
        self.assertFalse(path.is_file())


if __name__ == "__main__":
    unittest.main()
