/**
 * CrewDesk — "Your Desk": the human's escalation inbox for Issue Radar Crews.
 *
 * Two regions, top to bottom, matching `temp-screenshots/crews-mock/01-your-desk-*.png`:
 *
 *   1. A four-up stat row. Three of the four are the obvious ones (waiting on
 *      you / working / resolved). The fourth — SKIPPED AS DUPLICATE — is here on
 *      purpose and is deliberately NOT framed as a miss: an audit of this repo's
 *      40 most recent issues found 15 of them (37.5%) were duplicates of work
 *      already merged or already in review, against only 3 (7.5%) that a crew
 *      could implement end-to-end. A crew that reads one of those to the end and
 *      closes it out did the cheapest useful work on the board. A dashboard that
 *      counted only merges would report that as nothing happening.
 *
 *   2. The escalation queue, OLDEST FIRST. The first card is expanded with the
 *      crew's own question, its own recommendation, and a guidance composer; the
 *      rest collapse to one line. A crew that escalates without proposing an
 *      answer makes the human do all the work, so the recommendation is given as
 *      much room as the question.
 *
 * Between the two sits the ONE way to hire a crew. It is here rather than on the
 * roster column because the roster is a navigation list, and it reads as an
 * action on the desk: the desk is the page a human lands on with no crew yet.
 *
 * The repo's protocol settings are NOT on this page. They are repo-wide rather
 * than desk-scoped, so they live on that repo's own settings page
 * (`views/settings/CrewProtocolSettings.tsx`) with every other per-repo
 * preference. The desk still reads them — the hand-back window is part of what
 * the "needs your decision" tile says — but it does not own them.
 *
 * ## Two states that are NOT errors, and are not rendered as errors
 *
 * `sendCrewGuidance` resolves `{ ok, injected }`. `injected: false` means the
 * guidance was RECORDED on the work item but the crew's session was not live to
 * receive it — the crew will read it when it next wakes. That is a normal
 * outcome of a session that has been closed or restarted, so it renders as a
 * plainly-worded notice naming what did and did not happen, not as a failure.
 * Only a rejected request is an error.
 *
 * Likewise a `skipped` work item is an outcome, not a failure (see region 1).
 */
import { useMemo, useState } from 'react'
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { ChevronDown, ChevronUp, Inbox, MessagesSquare, Plus, SquareArrowOutUpRight, Undo2 } from 'lucide-react'
import { Btn, Card, EmptyState, IconButton, StatCard } from '../../../components/ui'
import {
  issueRadarApi,
  type Crew,
  type CrewCounts,
  type CrewEscalationRow,
  type WorkItem,
} from '../api'
import { issueUrlFor, repoScopeKey } from '../lib/links'
import { fmtDuration, toDate } from '../../../i18n/format'
import CrewGhost from '../components/CrewGhost'
import { useIssueRadar } from '../context'
import { i18nT } from '../../../i18n/t'

/** Translation root for every string on this view. */

/** The queue is the surface a human watches while a crew works, so it polls
 * faster than the crew list; the roster and the per-crew ledgers move on the
 * slower clock. Both are well inside the app's own `LIST_POLL_MS` budget. */
const QUEUE_POLL_MS = 30_000
const ROSTER_POLL_MS = 60_000

/** The window the two "· 24h" tiles count over. */
const DAY_MS = 86_400_000

/** Textarea styling. The shared `Input` primitive is an `<input>` and there is no
 * textarea primitive, so its class string is reused verbatim (minus `flex-1`,
 * which fights a full-width block) rather than re-invented — same background,
 * border, radius, text size and focus ring as every other field on the page. */
const TEXTAREA_CLASS =
  'w-full bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm '
  + 'font-body outline-none resize-y transition-colors focus-ring'

/** Section label inside an escalation card ("What I'm blocked on"). */
const SECTION_LABEL_CLASS = 'text-[13px] font-semibold uppercase tracking-[.06em] text-muted'

/** A stable identity for one queue row. A crew can hold more than one escalation
 * (bounded by its `max_escalated`), so the crew id alone is not unique. */
function rowKey(crewId: string, number: number): string {
  return `${crewId}:${number}`
}

/** Epoch-ms this item started waiting on a human.
 *
 * `escalation.at` when the backend stamped one, else `last_progress_at` — which
 * the store only moves on REAL progress, so it is the moment the crew stopped
 * being able to proceed. Returns 0 for an unreadable stamp so a bad record sorts
 * to the top of an oldest-first queue rather than silently to the bottom. */
function waitingSince(item: WorkItem): number {
  const at = toDate(item.escalation?.at ?? item.last_progress_at)
  return at ? at.getTime() : 0
}

/**
 * An elapsed span in the active locale, at ONE unit of granularity.
 *
 * The unit is chosen here (a product decision — the desk never wants
 * "2h 14m 3s") and the rendering is delegated to the locale-aware seam, so en
 * reads `38m` while de reads `38 Min.` and zh `38分钟`. Never hand-built from a
 * number and a letter.
 */
function elapsedLabel(sinceMs: number, nowMs: number): string {
  if (!sinceMs) return ''
  const secs = Math.max(0, Math.floor((nowMs - sinceMs) / 1000))
  if (secs < 60) return fmtDuration([[secs, 'second']])
  const mins = Math.floor(secs / 60)
  if (mins < 60) return fmtDuration([[mins, 'minute']])
  const hours = Math.floor(mins / 60)
  if (hours < 24) return fmtDuration([[hours, 'hour']])
  return fmtDuration([[Math.floor(hours / 24), 'day']])
}


/* ── Region 1: the stat row ─────────────────────────────────────────────────── */

interface DeskTallies {
  /** Resolved in the last 24h. */
  resolved: number
  /** Of those, how many carry a pull request. */
  resolvedWithPr: number
  /** Skipped in the last 24h — a duplicate or already-fixed issue, read to the
   *  end and closed out. */
  skipped: number
}

function StatRow({
  counts, tallies, talliesReady, oldestWaitMs, handbackDays, now,
}: {
  counts: CrewCounts | undefined
  tallies: DeskTallies
  talliesReady: boolean
  oldestWaitMs: number
  handbackDays: number | undefined
  now: number
}) {
  const { t } = useTranslation()
  const needsYou = counts?.needs_you
  // Two facts a human needs before deciding whether to open the queue at all:
  // how long the oldest crew has been stuck, and how long it has left before the
  // hand-back releases the claim on its own.
  const needsYouSub = !counts
    ? ''
    : needsYou === 0 || !oldestWaitMs
      ? t('apps.issueRadar.views.crews.desk.stat_needs_decision_sub_clear')
      : t('apps.issueRadar.views.crews.desk.stat_needs_decision_sub', {
        age: elapsedLabel(oldestWaitMs, now),
        window: handbackDays === undefined ? '' : fmtDuration([[handbackDays, 'day']]),
      })

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3" data-testid="crew-desk-stats">
      {/* Each tile and its subtitle share ONE grid cell. Putting the four
          subtitles in a row of their own lines them up at xl but stacks all four
          below all four tiles at `grid-cols-1`, which is where a narrow window
          actually renders.

          The four CARDS are the same height in every locale, which takes both
          halves of this: `flex-1` lets a card fill its cell (grid stretches the
          cells to the tallest), and the subtitle below it is given a fixed
          two-line slot. Without the slot a subtitle that wraps steals height from
          its own card only, and the row of four goes ragged — which is exactly
          what a label wrapping (or an InfoTip pushing one) used to do. */}
      <div className="flex flex-col gap-1">
        <StatCard
          className="flex-1"
          label={t('apps.issueRadar.views.crews.desk.stat_needs_decision')}
          value={needsYou}
          colorClass="text-danger"
          data-testid="crew-desk-stat-needs-you"
        />
        <div className="text-[13px] text-muted leading-snug min-h-[2.6em] line-clamp-2" data-testid="crew-desk-stat-needs-you-sub">{needsYouSub}</div>
      </div>
      <div className="flex flex-col gap-1">
        <StatCard
          className="flex-1"
          label={t('apps.issueRadar.views.crews.desk.stat_working')}
          value={counts?.working}
          accent
          data-testid="crew-desk-stat-working"
        />
        <div className="text-[13px] text-muted leading-snug min-h-[2.6em] line-clamp-2" data-testid="crew-desk-stat-working-sub">
          {counts ? t('apps.issueRadar.views.crews.desk.stat_working_sub', { count: counts.on_duty }) : ''}
        </div>
      </div>
      <div className="flex flex-col gap-1">
        <StatCard
          className="flex-1"
          label={t('apps.issueRadar.views.crews.desk.stat_resolved')}
          value={talliesReady ? tallies.resolved : undefined}
          data-testid="crew-desk-stat-resolved"
        />
        <div className="text-[13px] text-muted leading-snug min-h-[2.6em] line-clamp-2" data-testid="crew-desk-stat-resolved-sub">
          {talliesReady ? t('apps.issueRadar.views.crews.desk.stat_resolved_sub', { count: tallies.resolvedWithPr }) : ''}
        </div>
      </div>
      <div className="flex flex-col gap-1">
        <StatCard
          className="flex-1"
          label={t('apps.issueRadar.views.crews.desk.stat_skipped')}
          value={talliesReady ? tallies.skipped : undefined}
          // The InfoTip carries the "this is a win" argument. It is too long for a
          // subtitle and too important to leave to the reader's assumption: without
          // it, a number under "Skipped" reads as work thrown away.
          title={t('apps.issueRadar.views.crews.desk.stat_skipped_why')}
          data-testid="crew-desk-stat-skipped"
        />
        <div className="text-[13px] text-muted leading-snug min-h-[2.6em] line-clamp-2" data-testid="crew-desk-stat-skipped-sub">
          {talliesReady ? t('apps.issueRadar.views.crews.desk.stat_skipped_sub') : ''}
        </div>
      </div>
    </div>
  )
}

/* ── Region 2: the escalation queue ─────────────────────────────────────────── */

/** What happened to the last guidance send for one row. `stored` is the
 * `injected: false` case: recorded, not delivered — see the file header. */
type SendState =
  | { kind: 'idle' }
  | { kind: 'sending' }
  | { kind: 'delivered' }
  | { kind: 'stored' }
  | { kind: 'failed'; message: string }

function EscalationCard({
  crew, item, expanded, onToggle,
}: {
  crew: Crew
  item: WorkItem
  expanded: boolean
  onToggle: () => void
}) {
  const { t } = useTranslation()
  const { active } = useIssueRadar()
  const queryClient = useQueryClient()
  const scope = repoScopeKey(active)
  const [draft, setDraft] = useState('')
  const [sendState, setSendState] = useState<SendState>({ kind: 'idle' })
  const [handBackError, setHandBackError] = useState('')

  const escalation = item.escalation

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['issue-radar', 'crew-escalations', scope] })
    void queryClient.invalidateQueries({ queryKey: ['issue-radar', 'crews', scope] })
    void queryClient.invalidateQueries({ queryKey: ['issue-radar', 'crew', scope, crew.id] })
  }

  const send = useMutation({
    mutationFn: (text: string) => issueRadarApi.sendCrewGuidance(active, crew.id, item.number, text),
    onMutate: () => setSendState({ kind: 'sending' }),
    onSuccess: (res) => {
      // `ok` says the write landed; `injected` says the crew's session took it.
      // They are reported separately because only one of them is about delivery,
      // and collapsing them would either overstate delivery or invent a failure.
      setSendState({ kind: res.injected ? 'delivered' : 'stored' })
      setDraft('')
      invalidate()
    },
    onError: (e: unknown) => setSendState({ kind: 'failed', message: e instanceof Error ? e.message : String(e) }),
  })

  const handBack = useMutation({
    mutationFn: () => issueRadarApi.recordCrewWork(active, crew.id, item.number, {
      phase: 'handed-back',
      // The ledger line is PUBLIC — it is rendered on the crew page and inside
      // the claim comment on the forge — so the reason is written out rather
      // than left implicit in a phase change.
      event: i18nT('apps.issueRadar.views.crews.desk.hand_back_event'),
      event_kind: 'handback',
    }),
    onMutate: () => setHandBackError(''),
    onSuccess: invalidate,
    onError: (e: unknown) => setHandBackError(e instanceof Error ? e.message : String(e)),
  })

  const busy = send.isPending || handBack.isPending

  /* The header line, shared by both states: face, name, the issue, and the two
     links out.

     No duration badge and no `crew:` label chips. The wait is already on the
     desk's own "needs your decision" tile (oldest, plus the hand-back clock), and
     the labels are an INDEX the crews use to find each other's claims cheaply —
     they carry nothing a human reading their own inbox has to act on. */
  const header = (
    <div className="flex items-start gap-3">
      <CrewGhost seed={crew.avatar_seed} variant={crew.avatar_variant} size={50} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-[15px] text-text-strong" data-testid="crew-desk-row-name">{crew.name}</span>
        </div>
        <div className="mt-1 text-[13px] text-muted flex items-center gap-1.5 flex-wrap">
          <span className="font-mono text-text">#{item.number}</span>
          <span className="text-text">{item.why || item.next || item.decision}</span>
          <span aria-hidden="true">·</span>
          <a
            href={issueUrlFor(active, item.number)}
            target="_blank"
            rel="noreferrer"
            className="text-accent hover:underline inline-flex items-center gap-1"
            data-testid="crew-desk-row-issue-link"
          >
            <SquareArrowOutUpRight className="lucide-inline" />
            {t('apps.issueRadar.views.crews.desk.link_issue')}
          </a>
          {/* The crew works in a real chat session; `?sid=` is the dashboard's
              own deep link to one, so this is an in-app route and not a reload. */}
          {crew.slot_key && (
            <>
              <span aria-hidden="true">·</span>
              <Link
                to={`/chat?sid=${encodeURIComponent(crew.slot_key)}`}
                className="text-accent hover:underline inline-flex items-center gap-1"
                data-testid="crew-desk-row-session-link"
              >
                <MessagesSquare className="lucide-inline" />
                {t('apps.issueRadar.views.crews.desk.link_session')}
              </Link>
            </>
          )}
        </div>
      </div>
      <div className="flex items-center gap-1.5 shrink-0">
        {expanded && (
          <Btn
            onClick={() => handBack.mutate()}
            disabled={busy}
            data-testid="crew-desk-hand-back"
          >
            <Undo2 className="lucide-inline" />
            {handBack.isPending ? t('apps.issueRadar.views.crews.desk.handing_back') : t('apps.issueRadar.views.crews.desk.hand_back')}
          </Btn>
        )}
        <IconButton
          aria-label={expanded ? t('apps.issueRadar.views.crews.desk.collapse', { name: crew.name }) : t('apps.issueRadar.views.crews.desk.expand', { name: crew.name })}
          aria-expanded={expanded}
          onClick={onToggle}
          data-testid="crew-desk-row-toggle"
        >
          {expanded ? <ChevronUp className="lucide-inline" /> : <ChevronDown className="lucide-inline" />}
        </IconButton>
      </div>
    </div>
  )

  if (!expanded) {
    return (
      <Card className="mb-3" data-testid="crew-desk-row">{header}</Card>
    )
  }

  /* The alternatives the crew itself named. The crew's own recommendation is NOT
     here — it has its own button directly under the recommendation it refers to,
     because that is the fast path and a recommendation exists precisely so the
     human usually only has to agree. Each one PREFILLS the composer rather than
     sending, so the human can qualify it before it goes. */
  const quickActions = (escalation?.options ?? []).map((option, i) => (
    { key: `option-${i}`, label: option, text: option, primary: false }
  ))

  /* Shared by the accept button and the alternatives: end the prefill with a
     blank line so a qualification typed straight after starts on its own
     paragraph. Without it the recording showed the two running together —
     "…user-scoped directory alreadyGo with (a), but…" — because the caret lands
     at the end of the prefill. Safe to leave trailing whitespace: every send path
     (click, Cmd+Enter, and the disabled check) trims.

     Concatenated rather than interpolated on purpose: a template literal here is
     an i18n `template` finding (the gate cannot tell a whitespace join from
     assembled copy), and the separator carries no words to translate. */
  const PARAGRAPH_BREAK = '\n\n'
  const prefill = (text: string) => setDraft(text + PARAGRAPH_BREAK)

  return (
    <Card className="mb-3 border-danger/60" data-testid="crew-desk-row">
      {header}
      {/* No heading: the tinted panel IS the label. A red-tinted block reads as
          "this is the problem" without spending a line saying so, and the card is
          already titled by the crew and issue it belongs to. */}
      <div
        className="mt-4 bg-danger-subtle border border-danger/30 rounded-md px-3 py-2.5 text-sm text-text leading-relaxed"
        data-testid="crew-desk-question"
      >
        {escalation?.question || t('apps.issueRadar.views.crews.desk.no_question')}
      </div>
      {escalation?.recommendation && (
        <div className="mt-4 flex flex-col gap-1.5">
          <div className={SECTION_LABEL_CLASS}>{t('apps.issueRadar.views.crews.desk.recommendation_heading')}</div>
          {/* The crew's own words, kept verbatim (line breaks preserved) but in the
              app font: a recommendation is prose, so `font-mono` here would pin it
              to `--mono` and ignore the user's Font Family choice. Accent tint, so
              the two panels read as problem (red) and proposed answer (theme). */}
          <div
            className="bg-accent-subtle border border-accent/30 rounded-md px-3 py-2 text-[13px] text-text leading-relaxed whitespace-pre-wrap"
            data-testid="crew-desk-recommendation"
          >
            {escalation.recommendation}
          </div>
          {/* Directly under the recommendation, not down in the composer: the
              button acts ON the text above it, and the adjacency is what makes it
              obvious that agreeing is one click. */}
          <div className="flex">
            <Btn
              primary
              onClick={() => prefill(escalation.recommendation)}
              disabled={busy}
              data-testid="crew-desk-quick-approve"
            >
              {t('apps.issueRadar.views.crews.desk.approve_recommendation')}
            </Btn>
          </div>
        </div>
      )}
      <div className="mt-4 flex flex-col gap-2">
        <div className={SECTION_LABEL_CLASS}>{t('apps.issueRadar.views.crews.desk.guidance_heading')}</div>
        {quickActions.length > 0 && (
          <div className="flex items-center gap-2 flex-wrap" data-testid="crew-desk-quick-actions">
            {quickActions.map((a) => (
              <Btn
                key={a.key}
                primary={a.primary}
                onClick={() => prefill(a.text)}
                disabled={busy}
                data-testid={`crew-desk-quick-${a.key}`}
              >
                {a.label}
              </Btn>
            ))}
          </div>
        )}
        <div className="flex items-end gap-2">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              // Cmd/Ctrl+Enter sends, matching the chat composer. A bare Enter
              // stays a newline: guidance is prose and usually multi-line.
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && draft.trim()) {
                e.preventDefault()
                send.mutate(draft.trim())
              }
            }}
            placeholder={t('apps.issueRadar.views.crews.desk.guidance_placeholder', { name: crew.name })}
            aria-label={t('apps.issueRadar.views.crews.desk.guidance_heading')}
            rows={3}
            className={TEXTAREA_CLASS}
            data-testid="crew-desk-guidance-input"
          />
          <Btn
            primary
            onClick={() => send.mutate(draft.trim())}
            disabled={busy || !draft.trim()}
            className="h-9 px-4 shrink-0"
            data-testid="crew-desk-guidance-send"
          >
            {send.isPending ? t('apps.issueRadar.views.crews.desk.sending') : t('apps.issueRadar.views.crews.desk.send')}
          </Btn>
        </div>
        <div className="text-[13px] text-muted">{t('apps.issueRadar.views.crews.desk.guidance_note')}</div>
        {/* Updates in place after a send, so it must announce itself. */}
        <div
          aria-live="polite"
          className="text-[13px] min-h-[1.25rem]"
          data-testid="crew-desk-guidance-status"
          data-state={sendState.kind}
        >
          {sendState.kind === 'delivered' && (
            <span className="text-ok">{t('apps.issueRadar.views.crews.desk.delivered', { name: crew.name })}</span>
          )}
          {sendState.kind === 'stored' && (
            // NOT an error: the write landed, only the live hand-off did not.
            // Worded so the human knows exactly what is true — recorded, not
            // delivered, and the crew will read it when it next wakes.
            <span className="text-warn">{t('apps.issueRadar.views.crews.desk.stored_not_injected', { name: crew.name })}</span>
          )}
          {sendState.kind === 'failed' && (
            <span className="text-danger">{t('apps.issueRadar.views.crews.desk.send_failed', { error: sendState.message })}</span>
          )}
          {handBackError && (
            <span className="text-danger">{t('apps.issueRadar.views.crews.desk.hand_back_failed', { error: handBackError })}</span>
          )}
        </div>
      </div>
    </Card>
  )
}

/* ── The view ───────────────────────────────────────────────────────────────── */

export interface CrewDeskProps {
  /** Raise the create-crew dialog, which lives in `Workspace` (both this button
   *  and the crew page's Edit open the same form, so one owner means one dialog).
   *
   *  REQUIRED, unlike `CrewPageView`'s optional `onEdit`: this button is the only
   *  way to hire a crew, so a caller that forgets it must be a type error rather
   *  than a page that silently cannot create anything. */
  onCreate: () => void
}

export default function CrewDesk({ onCreate }: CrewDeskProps) {
  const { t } = useTranslation()
  const { active } = useIssueRadar()
  const scope = repoScopeKey(active)
  // One clock for the whole render, so every "38m" on the page is measured from
  // the same instant and two rows one second apart cannot disagree.
  const now = Date.now()
  const [openRow, setOpenRow] = useState<string | null>(null)

  const crewsQuery = useQuery({
    queryKey: ['issue-radar', 'crews', scope],
    queryFn: () => issueRadarApi.crews(active),
    refetchInterval: ROSTER_POLL_MS,
  })
  const escalationsQuery = useQuery({
    queryKey: ['issue-radar', 'crew-escalations', scope],
    queryFn: () => issueRadarApi.crewEscalations(active),
    refetchInterval: QUEUE_POLL_MS,
  })
  /* Read-only here, and only for the hand-back window the "needs your decision"
     tile quotes. The fields that EDIT this document live on the repo's settings
     page (`views/settings/CrewProtocolSettings.tsx`); the query key is shared, so
     a save there lands in this cache without a refetch. */
  const settingsQuery = useQuery({
    queryKey: ['issue-radar', 'crew-settings', scope],
    queryFn: () => issueRadarApi.getCrewSettings(active),
  })

  const crews = crewsQuery.data?.crews ?? []

  /* The two "· 24h" tiles count FINISHED work, and no single route serves that:
     `/crews` returns live tallies only (on-duty / working / needs-you / paused)
     and `/crews/escalations` returns escalated items only. The finished items
     live on each crew's own record, so they are read per crew — the same request
     and the same query key the crew page uses, so opening a crew afterwards is
     already warm and no request is made twice. Gated on the roster landing, and
     on the slower clock. The cheaper fix is a server-side 24h tally added to
     `_crews_page`'s counts, which would retire this fan-out. */
  const detailQueries = useQueries({
    queries: crews.map((c) => ({
      queryKey: ['issue-radar', 'crew', scope, c.id],
      queryFn: () => issueRadarApi.crew(active, c.id),
      refetchInterval: ROSTER_POLL_MS,
      staleTime: ROSTER_POLL_MS,
    })),
  })
  const talliesReady = crewsQuery.isSuccess && detailQueries.every((q) => q.isSuccess)
  // Not memoized: one pass over a handful of work items per crew, and the input
  // is the query array itself — which is a fresh object every render, so a memo
  // keyed on it would recompute anyway while pretending not to.
  const tallies: DeskTallies = { resolved: 0, resolvedWithPr: 0, skipped: 0 }
  for (const q of detailQueries) {
    for (const item of q.data?.items ?? []) {
      const finished = toDate(item.finished_at)
      if (!finished || finished.getTime() < now - DAY_MS) continue
      if (item.phase === 'resolved') {
        tallies.resolved += 1
        if (item.pr_number !== null) tallies.resolvedWithPr += 1
      } else if (item.phase === 'skipped') {
        tallies.skipped += 1
      }
    }
  }

  /* Oldest first: the desk exists to surface what has been stuck longest, and a
     queue ordered by anything else buries the item closest to being handed back
     on its own. */
  const rows = useMemo<CrewEscalationRow[]>(
    () => [...(escalationsQuery.data?.escalations ?? [])].sort(
      (a, b) => waitingSince(a.item) - waitingSince(b.item),
    ),
    [escalationsQuery.data],
  )

  // The first row is expanded until the human picks another. Derived rather than
  // seeded into state by an effect, so it stays correct when polling reorders the
  // queue and never renders a frame with everything collapsed.
  const firstKey = rows.length > 0 ? rowKey(rows[0].crew.id, rows[0].item.number) : null
  const expandedKey = openRow ?? firstKey

  return (
    <div className="px-6 pt-4 pb-8 overflow-y-auto flex-1 min-h-0 flex flex-col gap-4" data-testid="crew-desk">
      <StatRow
        counts={crewsQuery.data?.counts}
        tallies={tallies}
        talliesReady={talliesReady}
        oldestWaitMs={rows.length > 0 ? waitingSince(rows[0].item) : 0}
        handbackDays={settingsQuery.data?.settings.escalation_handback_days}
        now={now}
      />

      {/* Its own row between the stats and the queue, right-aligned. Not in the
          stat grid (it is not a statistic) and not in the queue (the queue is a
          list of things waiting on the human) — and right-aligned so it reads as
          the row's trailing action rather than competing with the first
          escalation card for the eye. Deliberately NOT `primary`: answering an
          escalation is the desk's primary act, and two accent buttons on one
          screen would make the page argue with itself. */}
      <div className="flex justify-end">
        <Btn onClick={onCreate} data-testid="crew-create">
          <Plus className="lucide-inline" />
          {t('apps.issueRadar.views.crews.new_crew')}
        </Btn>
      </div>

      <div data-testid="crew-desk-queue">
        {rows.length === 0
          ? (
            escalationsQuery.isPending
              ? <div className="text-muted text-[13px] py-6 text-center" data-testid="crew-desk-queue-loading">{t('apps.issueRadar.views.crews.desk.queue_loading')}</div>
              : (
                <EmptyState
                  icon={<Inbox className="lucide-inline" />}
                  title={t('apps.issueRadar.views.crews.desk.empty_title')}
                  subtitle={t('apps.issueRadar.views.crews.desk.empty_subtitle')}
                  testId="crew-desk-empty"
                />
              )
          )
          : rows.map(({ crew, item }) => {
            const key = rowKey(crew.id, item.number)
            return (
              <EscalationCard
                key={key}
                crew={crew}
                item={item}
                expanded={key === expandedKey}
                onToggle={() => setOpenRow(key === expandedKey ? '' : key)}
              />
            )
          })}
      </div>
    </div>
  )
}
