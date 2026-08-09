---
title: Issue Radar Crews — autonomous issue workers with a public claim ledger
status: draft
revision: v1
author: kirocrew agent session, directed by diwm
created: 2026-08-08
last-audited: 2026-08-08
audited-at: f2aa4c8bb
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---
# RFC: Issue Radar Crews — autonomous issue workers with a public claim ledger

Status: draft. Nothing in this document exists on main. Every code reference below
was read at `5adec8c58` and re-verified unchanged at `f2aa4c8bb`, the commit
implementation starts from.

**Disambiguation.** "Crews" already names two other things in this repository: the
agent-template roster at `/capabilities` → Crews (`website/src/pages/KiroCrewAgentsPage.tsx`),
and "Crew Mode" in `rfc-orchestrator-chat-sessions.md`. This RFC's crews are
neither. A crew here is a long-lived worker session that works one repository's
open issues. The name is kept because it is the one the feature will be called;
its i18n keys live under `apps.issueRadar.views.crews.*` so nothing collides.

---

## 1. Summary

A **crew** is a named, avatared worker that continuously watches one repository's
open issues. It picks up work inside a label scope, investigates, replies to
requesters, implements fixes, opens pull requests, drives them to green, resolves
merge conflicts, and waits for merge — escalating to a human whenever the next
step is a judgement that is not its own.

A crew is implemented as a dashboard chat session plus a scheduler. Coordination
between crews — including crews belonging to other people — runs through a single
public comment on each issue, edited in place, plus a `crew:` label as a cheap
index.

Three surfaces are added to the Issue Radar app: **Your Desk** (the escalation
inbox and the protocol settings), a **per-crew page**, and a **create/edit
dialog**. They mount as a fourth `MainView`, so the app's existing three-column
shell carries them unchanged.

Two companion documents carry the detail this one deliberately does not repeat:

- `src/kiro_crew/apps/builtins/issue_radar/backend/crew_brief.md` — the crew's
  behavioural contract, delivered to the model verbatim.
- `src/kiro_crew/apps/builtins/issue_radar/backend/crew_ledger_spec.md` — record
  schemas, the phase enum, the event log, and the two MCP tools.

## 2. Why, and the honest ceiling

The value case is not "fix issues faster". It is that a maintainer's issue tracker
accumulates work that is individually small and collectively unbounded, and that a
worker which reads an issue, recognises it as already fixed, and says so is doing
real work.

The ceiling is measured, not assumed. An audit of 40 of the 50 most recent
Kiro Crew issues found:

- **3 of 40 (7.5%)** were cleanly implementable without a human decision.
- **15 of 40 (37.5%)** were duplicates of work already fixed or already in an open
  PR.
- Of the 3 implementable candidates, 2 named the wrong root cause in the issue
  text.

Two consequences shape the whole design. First, **deduplication is the highest-value
step**, not implementation — so it is a mandatory part of investigation, performed
before any claim is made. Second, **"skipped — duplicate of #2240, already fixed on
main" is a successful turn**, and the UI must present it that way, or a crew told to
keep working will manufacture work.

Throughput is measured in hours per issue, not minutes. A full implement-to-merged
cycle in this repository routinely takes 4–8 CI rounds across several hours. The
per-crew page therefore shows *phase and next step*, not a velocity number.

## 3. What a crew is at runtime

| Element | Mechanism |
|---|---|
| Identity | a crew record under `repos/<owner>/<repo>/crews/<id>.json` |
| Execution | one dashboard `_ChatSlot`, key `crew-<id>` |
| Behaviour | `crew_brief.md`, injected into the conversation (§9) |
| Memory across turns | the ledger, not the transcript (§10) |
| Clock | the Issue Radar watcher (§4) |
| Backstop clock | AutoNudge, only when the watcher has nothing and the crew stalled |
| Authorisation | per-slot trust, re-established every cycle |
| Workspace | one git worktree per issue |

The closest existing precedent is `auto_research`
(`src/kiro_crew/apps/builtins/auto_research/handlers.py:974-1040`): an app-owned
dashboard slot, an autonudge loop, per-slot trust, a STOP sentinel, and a TTL
watchdog. That launch sequence is the one to copy.

A crew is scoped to **one repository**. Issue Radar is repo-first — you select a
repo, then create crews inside it — so there is no repo field on the crew.
Crew names are unique per repository.

## 4. Scheduling: the watcher is the clock

A crew should always have something to do. While one issue waits on CI, it should
advance another. That is a scheduling property, not an agent property, and it is
worth stating plainly what cannot be done:

**A turn cannot be made not to end.** The turn ends when the model stops emitting
tool calls, and `agent.chat_turn_timeout_secs` caps it at 7200s (default and
maximum; config range 300–7200, `config/loader.py:2666`, clamped against the ACP
prompt timeout at `acp/client.py:676`). A crew working three issues across six
hours is therefore many turns, and something must start each one.

That something is the **Issue Radar watcher**, extended into the crew scheduler.
It is the only always-on loop in the app: `watch.py:_watch_loop` runs on
`app.on_startup` at `POLL_INTERVAL_SEC = 60`. Everything else in the app —
including the probe-coalescing and staleness machinery in `routes.py`
(`_PROBE_COALESCE_SEC`, `LIST_POLL_MAX_STALENESS_SEC`) — is driven by HTTP
requests from the frontend, so **with no tab open, none of it runs and none of its
caches are fresh**. A crew cannot trust them.

The watcher gains a zero-LLM sweep over every crew's open work items, comparing
the six unblock signals (§6). When a signal changes, it wakes the owning crew.
Two gates must be treated differently from today's behaviour:

- `is_app_enabled(APP_NAME)` is kept — app disabled means crews stop.
- the per-repo `notify_on_new_issue` gate is **not** inherited. That is a
  notification preference, not a fetch switch.

AutoNudge remains armed as the backstop for the case the watcher cannot see: the
crew simply stopped. Its state is persisted (`~/.kiro/crew/autonudge.json`), it
re-arms its timers on gateway start (`autonudge.py:456`), and its fire path
rehydrates a non-resident slot before giving up (`gateway.py:3202`).

## 5. The turn contract

Every turn, in this order:

1. **Read the ledger.** Paused or retired → stop.
2. **Reconcile.** Check unblock signals on every open work item. Where a worktree
   has uncommitted changes, run `git status` and reconcile against the ledger — the
   previous turn may have been cut off by the 2-hour ceiling or a restart, and the
   files on disk are ahead of what was recorded.
3. **Advance** the single most advanceable item, in priority order: an item that
   was being edited → a merge conflict → CI turned red → new review comments →
   approved/mergeable → the requester replied → CI turned green.
4. **Pick up new work** only if nothing above is advanceable and the crew is under
   its open-item limit.
5. **Write the ledger.** Always, including turns where nothing moved. Also at any
   checkpoint inside a turn that precedes something long.

The ledger write is a hard step, not an instruction to be followed when
convenient: it is the only thing that survives compaction, the turn ceiling, and a
gateway restart.

## 6. Unblock signals

| Signal | Source |
|---|---|
| requester replied | issue timeline, comments after the crew's own |
| CI state changed | PR check-runs + commit statuses |
| approved / changes requested | PR reviews |
| merge conflict appeared | PR `mergeable` / `mergeable_state` |
| PR merged | PR state |
| post-merge comment | PR timeline after the merge commit |

All six, or an item stalls silently.

## 7. Claim protocol

The tool is for a maintainer working their own repository, so a public comment is
an acceptable coordination substrate. It is also the only one available: every
GitHub write goes out as the operator's own `gh` identity — there is no per-crew
GitHub identity, no PAT, no App (`github_client.py:1-14`) — so crews cannot be
told apart by author, and attribution must live in the comment body.

**Claim on commit, not on look.** Investigation leaves no trace and is free;
a claim is a public comment. A crew reads, deduplicates and decides first, and only
then claims. This is what keeps the 37.5% duplicate rate from producing a comment
on every duplicate.

**One comment per crew per issue, edited in place.** Never a second comment.
GitHub does not notify subscribers on an edit, so progress edits are silent where
new comments would not be. Edit only on real progress.

```
👻 **Whirlpool** is on this · Kiro Crew Issue Radar
implementing · PR #2271 · CI round 3 · updated 20:44 UTC

<details><summary>progress</summary>

- `18:02` claimed — read the issue and the 4 call sites
- `18:14` confirmed not a duplicate — #2240 is a different code path
- `20:44` CI round 3 — 41/47 green, 6 reds inherited from main

</details>

<!-- kirocrew-crew id=c_7f3a phase=implementing pr=2271 updated=2026-08-08T20:44:12Z -->
```

Two visible lines, history folded. The HTML comment is the machine payload, and
`id` is the crew id rather than the name — a crew may be renamed and must still
recognise its own claim.

**The timestamp is written by the crew, in the body.** Not read from GitHub's
`updated_at`, because that field moves on *any* edit — including a human fixing a
typo in the crew's comment, which would silently renew a dead claim.

**Tie-break.** Two crews in the same label scope can both finish investigating and
claim within seconds. After posting, a crew re-reads the comments; if another
crew's marker carries a lower comment id, it yields — editing its own comment to
say so rather than deleting it, because the yield is useful history — and picks
again.

**TTL is phase-aware.** A claim ages only in the phases where the crew is supposed
to be acting: `claimed`, `investigating`, `implementing`. `awaiting-ci`,
`awaiting-review`, `awaiting-merge`, `awaiting-reply` and `escalated` are exempt —
an open PR or an escalation comment is stronger evidence of a live claim than any
heartbeat, and a crew waiting three days for a review makes no progress to record.
Default TTL 48 hours.

**Labels as a cheap index, not as authority.** `_ISSUE_JQ` already projects
`labels`, `body` and the comment **count** for every open issue
(`github_client.py:347`), so one list call answers both "is this in my scope" and
"is anyone on it". `comments == 0` means definitively unclaimed with no timeline
read at all. The asymmetry that makes divergence safe:

- label present → skip, no verification. A false skip only costs a different issue.
- label absent → still read the comments before claiming. A lost label cannot
  cause duplicate work.

Three labels, created on first use via `create_label`: `crew: in progress`,
`crew: needs decision`, `crew: awaiting reply`. No completion label — the issue is
closed by the PR's `Fixes #N` and the PR link is already in the timeline.

**Crews do not apply taxonomy labels.** `.github/workflows/issue-triage.yml`
labels every issue on `issues: opened`, from an allowlist-by-construction, and it
only ever adds. So the crew's writable label set is exactly the `crew: ` prefix —
which is also the entire label allowlist the backend has to enforce.

**Hand-back.** After the escalation window (default 3 days) the claim is released:
remove the `crew:` label **and** edit the comment to say it was handed back. Both,
together. A stale `crew: in progress` with no live crew makes every other crew skip
that issue forever, and it is the one place in this design where divergence fails
in the dangerous direction.

## 8. Work isolation

**One worktree per issue**, created at claim, path recorded in the work item.
Branch `crew/<name>/issue-<n>`.

**At most one item in an editing phase per crew**, enforced by the store rather
than asked for in the brief. Editing means a worktree with uncommitted changes.
Two of them is how a fix for one issue gets committed onto another issue's branch,
across a `cd` the crew has forgotten about, and it is close to undetectable
afterwards.

Cost is real: a worktree of this repository measures **1.3 GB, of which 763 MB is
`website/node_modules`**. Two mitigations, both required:

- Install frontend dependencies **only when the diff touches `website/`**. Most
  issues are backend-only. Never share or symlink `node_modules` between
  worktrees — a rebase that moves `package-lock.json` leaves the crew testing
  against a toolchain it does not have, and the resulting red looks exactly like
  inherited breakage.
- Delete on `resolved` — not on green (a conflict may still arrive) and not on
  merge (post-merge comments may still arrive). A TTL sweep catches abandoned ones.

Concurrent git operations against the same repository (`fetch`, `worktree add`)
can contend on locks under `.git`. Retry with backoff, and distinguish retryable
contention (`cannot lock ref`, `index.lock exists`) from failures that will never
succeed on retry (`non-fast-forward`, a real conflict).

## 9. Behaviour delivery and its cost

The brief is **injected into the conversation by the backend**, so it works with
whatever agent the user picked. No agent spec is shipped for crews, and nothing
has to be pasted anywhere.

**Injection is a presence check, not a schedule.** The backend scans
`slot.messages` for the sentinel `<!-- kirocrew-crew-brief v1 -->`, requiring the
carrying message to be at least as long as the brief so that a compaction summary
quoting the sentinel is not a false hit. On a miss, inject. One rule covers session
start, post-compaction, gateway restart, and any future truncation mechanism, with
no detection heuristic to get wrong.

The cost model matters because it is not intuitive. Measured against this machine's
own usage shards (3,241 records over 10 days):

| Fact | Value |
|---|---|
| marginal credits per 1k of context, `claude-opus-5` | **0.154** (n=1,416, context < 250k) |
| `context_window` reported for `claude-opus-5` | **1,000,000** (n=2,271) |
| brief size | ~3.3k tokens |
| a busy real session today | 79 turns, 1,005 credits, 12.7/turn |

Anything appended to `slot.messages` **accumulates**: a nudge is appended as a user
message (`state.py`, `enqueue_or_run_prompt`), and so is a tool result
(`chat_runner.py:3668`). A brief re-sent on all ~80 turns of a day is therefore
present ~80 times, costing about 0.254·N² ≈ **1,650 credits/day/crew** — more than
the entire real session above. Reading it from a file each turn is worse still: the
tool result accumulates identically *and* costs an extra full-context round-trip.
The 1M window means compaction does not rescue this early.

The presence check reduces that to a handful of injections a day, ~0.5 credits each.

A **compressed Never block** (~80 tokens) rides in every nudge alongside the
volatile snapshot. The injected brief is a *user* message, not a system prompt, and
carries less authority for it; keeping the hard prohibitions adjacent to the
instruction is the cheap way to buy that back.

Per-crew `model` overrides whatever the chosen agent pins, since
`get_or_create_slot` takes `model=`.

## 10. Storage

Full schemas in `crew_ledger_spec.md`. The shape, and the three things that are
easy to get wrong:

**The ledger is working memory, not a report.** It must carry enough to resume
cold — worktree, branch, base SHA, what was tried and rejected and why, and a
`next` field holding a resumable *intent* ("add the Windows branch to
`_safe_chmod`, the regression test already fails") rather than a status
("implementing").

**The phase enum drives three classifications that do not coincide.** TTL-active is
three phases; counting against `max_open` is every non-terminal phase *except*
`escalated`; editing is `implementing` plus `addressing-review` with uncommitted
changes. One boolean cannot express this.

**Event text becomes public.** The event log feeds both the crew page's work log
and the `<details>` list inside the public claim comment, so the stricter side
governs: no absolute paths, no host names, nothing about the machine. Worktree
paths live in the work item's own fields, which stay local. Redact on the way in,
as `issue_radar_record_investigation` already does.

The agent write path is **two narrowly-allowlisted routes** — one read, one that
upserts work-item state and appends one event together, so a phase cannot change
without a logged reason. The allowlist entry must be the **full path**, never the
`/api/apps/issue-radar` prefix: prefix-matching there would also admit the app's
GitHub write routes to anything holding the internal secret
(`dashboard/server.py:414-427`).

## 11. UI

Crews is a fourth `MainView`, not a dashboard tab. Dashboard tabs render full-width
with no list column (`Workspace.tsx`), while `issues` and `pulls` render
`list column + ResizeHandle + main` — which is the shape this feature needs.

```
COLUMN 1 (nav only)   COLUMN 2 (list)        COLUMN 3
CREWS            2    ▸ Your Desk       2    Your Desk: stats, escalation queue,
  Crews               ──────────────         protocol settings
FILTERS               ▸ Andromeda    ●
PULL REQUESTS         ▸ Whirlpool    ⚠       or the selected crew's page
SETTINGS              ▸ Pinwheel     ●
```

This mirrors the Settings section's existing general-page-plus-per-item structure.
State indicators live on the column-2 rows, as they do in `IssueList`; column 1
stays navigation. Filter chips (All / Working / Needs you / Paused) sit in the
column-2 header. Give the crew list its own width key rather than reusing
`LIST_WIDTH_KEY`, or crew and issue column widths move together.

**Your Desk** is the human's inbox: counts, the escalation queue with an inline
guidance box that injects into the crew's own session, and the per-repo protocol
settings. **Escalations get a count badge on the rail's CREWS header and a
`state.notify("issue-radar", …)` notification** — the path the watcher already uses
for new issues — because the crew view is per-repo and a blocked crew must be
visible without visiting its repository.

Registration is two files: `lib/types.ts` (`MainView`, `ExpandedSection`) and the
`Workspace.tsx` branch.

Evidence: `.github/screenshots/issue-radar-crews/` — Your Desk, the crew page and
the create dialog in both themes, plus `crews-guidance-flow.gif` walking the
escalation flow end to end. Captured from the real built SPA by
`website/scripts/capture-crews.mjs` and `record-crews.mjs`, which share their
fixtures via `scripts/lib/issue-radar-crews-fixtures.mjs`.

## 12. Identity

**Avatars are Kiro ghosts.** The 24×28 bitmap already exists
(`hooks/sceneText.ts:145` `KIRO_GHOST_PIXELS`), as does an 8-entry outfit table
that differentiates ghosts by hat, glasses and cape rather than body colour
(`scenes/GhostScene.tsx:36`), and a canvas renderer for it
(`useSceneInteraction.tsx:20` `MiniGhost`). The outfit is selected by a djb2 hash
of the avatar seed — the same technique `WateringHoleScene.tsx:92` already uses to
keep an agent's species stable.

DiceBear `pixel-art` (`components/CrewAvatar.tsx`) is deliberately *not* reused:
it is already the visual language of the agent-template roster, and a crew must not
look like an agent definition. Ghost art is also brand-correct and needs no new
asset — which matters, because `AUTOSDE.yaml`'s `use-lucide-icons` rule blocks new
inline `<svg viewBox>` in any `.tsx` file including tests. Canvas is exempt and
already precedented.

`avatar_seed` is stored **separately from `name`**, so renaming a crew keeps its
face. An explicit `avatar_variant` pins one outfit.

**Names are galaxies**, 24 of them, no two sharing their first two letters so a log
line is unambiguous at a glance:

```
Andromeda  Bode      Butterfly  Carina     Cigar      Cocoon
Draco      Fireworks Fornax     Grus       Hoag       Leo
Mayall     Medusa    Pinwheel   Porpoise   Sculptor   Sombrero
Spindle    Tadpole   Triangulum Tucana     Ursa       Whirlpool
```

Allocation excludes every name ever used, **including retired crews** — a retired
crew's work log and its check-in comments still carry its name, and reuse would
make an old comment look like a live claim. Exhaustion degrades to `<Galaxy> II`,
which is also astronomically correct. Uniqueness is enforced server-side on create
(409), not merely by the suggestion chips: the name field is free text.

## 13. What bounds a crew

All crews run allow-all-tools and unattended. What that means, and what is left:

**Still enforced in code.** The PreToolUse hook path fires independently of
`allowedTools` (`chat_runner.py:375` — `allowedTools` skips *approval*, not the
hook), so Kiro Crew's own policy still hard-refuses destructive commands,
force-pushes to protected branches, and credential-file reads. Branch protection
keeps crews off `main`. Every PR needs human approval before merge, so nothing
lands unreviewed. `_repo_can_write` fails closed on permission checks
(`routes.py:1357`). The `crew: ` label prefix is enforceable server-side at the
label route. The one-editing-item rule is enforced by the store.

**Asked for, not enforced.** Not modifying the gate configs that judge a crew's own
PR (`.github/`, `AUTOSDE.yaml`, `.woke.yml`, `.jscpd.json`). A `pull_request` run
on a same-repo branch executes the workflow from the PR head, so a PR that raises
`--max-warnings` makes its own lint failure disappear and the mechanical gate goes
green. What catches it is the four AI review gates reading the diff, plus the human
approval every PR needs. The rule lives in the brief's Never list — and, because
that list rides in every nudge, it is now the most-repeated rule in the design
rather than the least.

**Inherited from the chosen agent.** Whatever MCP servers it carries. An unattended
crew on an agent with credential-adjacent servers has that reach.

## 14. Phase 0 — runtime gaps that must close first

These are not hardening; without them the feature does not work.

**Approval parks for two hours.** The dashboard turn path does not use
`request_approval` and hardcodes its own wait: `chat_runner.py:4643`,
`await asyncio.wait_for(fut, timeout=7200.0)`, then denies. There is no
`is_background` on this path — the 180s background variant
(`_BACKGROUND_APPROVAL_TIMEOUT_SECS`, `state.py:2329`) is only reachable from the
Slack gateway. A crew that trips one untrusted tool holds its slot for two hours
and then fails, silently.

Fix: set `slot._trust = True` per crew, and re-establish it every cycle from a
crew watchdog. `_trust` is **not persisted** — `auto_research` re-sets it each
watchdog cycle for exactly this reason (`handlers.py:867-869`, "restart-durable;
bounded above"). A process-wide yolo toggle is not a substitute: it dies with the
process while autonudge survives it, so the first turn after a restart walks into
the 7200s wait. A crew that finds itself unauthorised must escalate rather than
wait.

**Nothing caps concurrency.** Chat slots are uncapped and concurrent turns are
uncapped; the nearest analogue, terminal sessions, caps at 12
(`handlers/terminal.py:53`). The real ceiling today is `Semaphore(4)` on agent
cold starts (`session.py:748`) and host memory. Add a crew-level semaphore —
`code_review_sage`'s `Semaphore(max)` plus a ceiling is the pattern.

**Idle cleanup kills loops permanently.** `/api/chat/slots/cleanup`
(`chat_handlers.py:1941`, 3-day default) marks an idle slot `closed`. The autonudge
fire path rehydrates without `adopt_closed=True`, so it cannot reach a closed slot
and **removes the loop** — terminally. Pin crew slots, and pass `adopt_closed=True`
on that rehydrate.

## 15. Missing GitHub client surface

| Needed | Status |
|---|---|
| `update_issue_comment` → `PATCH /repos/{o}/{r}/issues/comments/{id}` | **missing.** The only PATCHes are on `issues/{n}` and `pulls/{n}`, both state-only |
| `id` and `updated_at` on normalized comment rows | **missing.** `_normalize_timeline_event` keeps only `kind`/`actor`/`created_at`/`body`, so the comment cannot be addressed for an edit |
| `create_pull_request` | **missing** from this client. A parallel implementation exists in `auto_improvement/profiles/github_repo/pr_recipe.py` |
| `/issue/comment` HTTP route | **missing.** `add_issue_comment` exists at `github_client.py:2416`; only `/pull/comment` is routed |
| everything else (list, timeline, labels, checks, reviews, merge, auto-merge, rerun) | present — 12 write functions |

Empirically unverified, worth one real test before relying on it: whether editing a
comment bumps the *issue's* `updated_at` (which would make every heartbeat visible
to the change probe), and whether GitHub disables an armed auto-merge when a branch
becomes conflicted. The design re-arms auto-merge unconditionally after resolving a
conflict, which is idempotent and costs nothing if it was still armed.

## 16. Implementation phases

**Phase 0 — guardrails.** §14, three independent changes. No crew yet.

**Phase 1 — client surface.** §15's four missing pieces. Independently testable.

**Phase 2 — one crew, vertical slice.** Crew record + work item + event log with a
schema stamp from the first release (Issue Radar's "schema mismatch → refetch from
GitHub" strategy does not transfer: a crew record has no upstream). One worker,
one repo, claim → investigate → dedup → reply, stopping short of implementation.
This is where the claim protocol and the tie-break get exercised for real.

**Phase 3 — implementation and PR.** Worktree lifecycle, commit identity and
trailer, `Fixes #N`, CI-to-green, review rounds, merge conflicts, auto-merge.

**Phase 4 — N crews and the UI.** The crew semaphore, label-scoped routing, Your
Desk, the crew page, the create/edit dialog, notifications.

## 17. Open items

- **`max_escalated` is a per-crew setting with a default of 3, and the 3-day
  hand-back bounds the queue** — but a productive crew can still hold several
  escalated items, each with a 1.3 GB worktree. Whether that needs a further cap is
  a question for the first real run, not a design decision.
- **The brief's phase playbooks are all always-present.** Splitting the ~3.3k into
  a small always-on core plus files read on entering a phase would cut the flat
  cost to ~10 credits/day and put the i18n rules in front of the model at the
  moment they matter. Not adopted; the flat cost is affordable and the indirection
  has its own failure mode (a crew that skips the "read this first" step acts on
  incomplete rules).
- **Requiring the crew to declare any gate-config change in the PR body** would
  convert §13's residual risk from silent to declared. Cheap; not yet in the brief.

## Appendix — measured facts

Everything quantitative in this document, and where it came from.

| Fact | Value | Source |
|---|---|---|
| Auto-fixable share of recent issues | 3/40 (7.5%) | 10-agent audit of 40 of the 50 latest issues |
| Duplicate share | 15/40 (37.5%) | same |
| Marginal credits per 1k context, opus-5 | 0.154 | regression over `~/.kiro/crew/usage/tokens/*.jsonl`, n=1,416 |
| `context_window`, opus-5 | 1,000,000 | same shards, n=2,271 |
| Busiest real session, 2026-08-08 | 79 turns / 1,005 credits | same shards |
| Brief size | 13,124 chars ≈ 3.3k tokens | `crew_brief.md` |
| Worktree size | 1.3 GB (763 MB `node_modules`) | `du -sh` on a real worktree |
| Watcher interval | 60s | `watch.py:43` |
| Turn ceiling | 7200s | `turn_dispatch.py:50`, `constants.py:45` |
| Dashboard approval wait | 7200s, hardcoded | `chat_runner.py:4643` |
| Background approval wait (unreachable here) | 180s | `state.py:2329` |
| Agent cold-start concurrency | 4 | `session.py:748` |
| Terminal session cap (contrast) | 12 | `handlers/terminal.py:53` |
| Idle cleanup threshold | 3 days | `chat_handlers.py:1941` |
| `auto_research` trust TTL | 24h | `auto_research/handlers.py:126` |
| Repository labels | 34 total; `crew: ` is the crew-writable set | `gh label list` |
