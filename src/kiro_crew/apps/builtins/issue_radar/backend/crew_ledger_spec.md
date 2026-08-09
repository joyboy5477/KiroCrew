# Crew ledger + agent write path

Storage root follows the app's existing convention — `app_data_dir("issue-radar")`,
per-repo namespace, `atomic_write` under an exclusive `platform_compat.file_lock`
for every read-modify-write.

```
<data>/repos/<owner>/<repo>/crews/<crew_id>.json          # crew record
<data>/repos/<owner>/<repo>/crews/<crew_id>/<number>.json  # one work item
<data>/repos/<owner>/<repo>/crews/events.jsonl             # append-only event log
<data>/repos/<owner>/<repo>/crews/settings.json            # per-repo protocol constants
```

Every file carries `schema: 1`. Issue Radar's existing versioning strategy —
"schema mismatch ⇒ treat as a cache miss and refetch from GitHub" — does **not**
transfer here: a crew record has no upstream to refetch from, so a forward
migration is required from the first release.

## Crew record

| Field | Type | Notes |
|---|---|---|
| `schema` | int | 1 |
| `id` | str | `c_<8 hex>`. Stable forever. Everything machine-readable keys on this, never on `name` |
| `name` | str | galaxy name, unique per repo including retired crews |
| `avatar_seed` | str | separate from `name` so a rename keeps the face |
| `avatar_variant` | int \| null | 0–7 pins one ghost outfit; null = derive from `avatar_seed` |
| `agent` | str | `kirocrew-crew` by default |
| `model` | str | `""` = governed default |
| `extra_prompt` | str | appended after the brief, never replacing it |
| `labels` | [str] | its scope. Empty = every label |
| `auto_resolve_conflicts` | bool | default true, structural files only |
| `auto_merge` | bool | default true |
| `unattended` | bool | default true → per-slot trust, re-established each cycle |
| `max_open` | int | default 3 |
| `max_escalated` | int | default 3 |
| `worktree_root` | str | one worktree per issue lives under here |
| `slot_key` | str | `crew-<id>`. ASCII, no colon — already normalization-safe |
| `enabled` / `paused_reason` | bool / str | a self-pause records why |
| `created_at` / `retired_at` | ISO8601 Z | retiring keeps the record so the name stays taken |

## Work item

One file per (crew, issue). Merged per field on write — a patch carrying only
`phase` preserves everything else, same semantics as the existing
`write_investigation`.

| Field | Type | Notes |
|---|---|---|
| `schema` | int | 1 |
| `crew_id` / `owner` / `repo` / `number` | | identity |
| `phase` | enum | see below |
| `outcome` | enum \| null | set only in a terminal phase |
| `decision` / `why` | str | what this crew decided to do and on what grounds |
| `next` | str | **the resumable intent.** "add the Windows branch to `_safe_chmod`, the test already fails" — not "implementing" |
| `tried` | [{`approach`, `rejected_because`}] | append-only, so a resumed turn does not re-walk a dead end |
| `worktree` / `branch` / `base_sha` | str | local only, never echoed into a comment |
| `pr_number` | int \| null | |
| `ci_state` | {`state`, `passed`, `total`, `round`, `inherited_reds`} | `inherited_reds` is what keeps a crew from rebasing at main's breakage |
| `claim_comment_id` | int \| null | which comment to PATCH. Rediscoverable from the marker if lost |
| `labels_applied` | [str] | so a hand-back knows exactly what to remove |
| `escalation` | null \| {`question`, `options`, `recommendation`, `asked_at`, `answered_at`, `guidance`} | renders Your Desk |
| `claimed_at` / `last_progress_at` / `finished_at` | ISO8601 Z | `last_progress_at` moves only on real progress |

### Phase enum

```
selected        local only, pre-claim — never public
claimed
investigating
implementing            ← the only editing phase
awaiting-ci
addressing-review
awaiting-merge
resolved                terminal
```

Side states: `awaiting-reply`, `escalated`, `skipped`, `yielded`, `handed-back`,
`preempted`.

Three independent classifications hang off this enum, and they do not coincide:

- **TTL-active** — `claimed`, `investigating`, `implementing`. Only these age
  toward the claim TTL. Everything else is parked legitimately and is exempt: an
  open PR or an escalation comment is stronger evidence of a live claim than any
  heartbeat.
- **Counts toward `max_open`** — every non-terminal phase **except** `escalated`.
- **Editing** — `implementing`, plus `addressing-review` while the worktree has
  uncommitted changes. At most one per crew, enforced by the store: a second item
  entering an editing phase is refused, not warned about.

## Event log

Append-only JSONL, content-addressed id so duplicate lines merge on read rather
than conflict (the ledger pattern from ops-mission-control, which shipped without
a lock and was caught in review — take its `_LedgerLock` too).

```json
{"id":"<sha256(ts|crew|number|kind|text)[:16]>","ts":"2026-08-08T20:44:12Z",
 "crew_id":"c_7f3a","number":2251,"kind":"ci","text":"CI round 3 — 41/47 green, 6 inherited from main"}
```

One log feeds **two** surfaces: the work-log table on the crew page, and the
`<details>` progress list inside the public claim comment.

That dual use imposes the stricter constraint on both: **`text` becomes public**,
so it must never contain an absolute path, a host name, or anything from the
user's environment. Worktree paths live in the work-item fields (local only) and
must not appear in an event. Redact on the way in with
`platform.redact_via_context`, exactly as `issue_radar_record_investigation`
already does — that tool redacts because the prose is re-rendered on a card; here
it is re-rendered on github.com.

## Per-repo settings

Protocol constants shared by every crew in the repo. They cannot be per-crew:
two crews negotiating with different values is how a short-TTL crew steals a
long-TTL crew's live work.

| Field | Default |
|---|---|
| `claim_ttl_hours` | 48 |
| `escalation_handback_days` | 3 |
| `commit_trailer` | `Crew: {name} (Kiro Crew Issue Radar)` |

Editable on Your Desk.

## Nudge composition

The brief is not carried by an agent spec — it is injected into the conversation
by the backend, so it works with whatever agent the user picked. Two parts go out
each turn:

**The volatile snapshot** (~120 tokens): crew name, repo, crew id, label scope,
limits and current counts, every open work item with its phase and `next`, and the
`crew:` labels it may write. Everything here changes turn to turn, so it is cheap
and correct to resend.

**A compressed Never block** (~80 tokens): the hard prohibitions, restated
verbatim from the brief's Never list. This exists because the injected brief is a
**user** message, not a system prompt, and therefore carries less authority than
the same words would in an agent spec. Keeping the prohibitions adjacent to the
instruction costs about one credit a day and is the cheapest way to buy that
authority back.

### Brief injection — presence check, not a heuristic

The brief itself is injected only when it is **absent from the conversation**, not
on a schedule and not by inferring that a compaction happened. The backend scans
`slot.messages` for the sentinel

```
<!-- kirocrew-crew-brief v1 -->
```

and requires the message carrying it to be at least as long as the brief — a
compaction summary that merely quotes the sentinel is shorter and does not count
as a hit. On a miss, inject.

One rule covers session start, post-compaction, gateway restart, and any future
truncation mechanism, with no detection logic to get wrong. Measured on this
machine's own usage shards, the marginal cost of the brief is 0.154 credits per 1k
of context on `claude-opus-5`; at ~3.3k the brief costs about 0.5 credits each
time it is injected, and a presence check fires it a handful of times a day rather
than on all ~80 turns.

## Agent write path — two tools, two allowlisted routes

The gate must stay a **full-path** allowlist entry, never the
`/api/apps/issue-radar` prefix. That distinction is deliberate in
`dashboard/server.py`: prefix-matching there would also admit the app's GitHub
write routes (label, close, comment) to anything holding the internal secret.

### `issue_radar_crew_read`

No required args beyond the crew's own identity, which the handler resolves from
the session. Returns the crew record, its per-repo settings, and every
non-terminal work item with the fields above.

The nudge already carries a snapshot, so this exists for the two cases the
snapshot cannot cover: a turn that runs long enough for the snapshot to go stale,
and a resume after compaction or restart where the crew has to re-establish what
it was doing.

### `issue_radar_crew_record`

One write tool that upserts work-item state **and** appends one event, rather than
two tools. Merging them means a phase can never change without a logged reason,
and a progress step costs one call instead of two.

Flat args, following `issue_radar_record_investigation`'s shape (it flattens its
five findings fields the same way). Empty fields are dropped, so a partial patch
preserves what an earlier write stored.

```
number                    required int
phase                     optional enum
outcome                   optional enum
next                      optional str
decision, why             optional str
tried_approach,
tried_rejected_because    optional pair — appends one `tried` entry
worktree, branch, base_sha  optional str
pr_number                 optional int
ci_state, ci_passed,
ci_total, ci_round,
ci_inherited_reds         optional
claim_comment_id          optional int
labels_applied            optional [str]
escalation_question,
escalation_options,
escalation_recommendation optional
event                     optional str — the public progress line
event_kind                optional enum (claim|investigate|reply|implement|ci|review|conflict|merge|escalate|handback|skip|yield)
```

Validation lives in `validation.py` alongside the existing schemas. The handler
sends `owner`/`repo` explicitly so a same-numbered issue in another repo cannot
overwrite this record, and refuses a second item entering an editing phase.
