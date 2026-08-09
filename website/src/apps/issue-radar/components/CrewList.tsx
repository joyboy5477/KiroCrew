// Issue Radar — column 2 of the crews surface: Your Desk pinned first, then the
// crew roster.
//
// Layout follows the issue and PR list columns: one rounded card per row in a
// scrolling stack, a pinned Your Desk card, and a `CREW · N` group label. The
// filter and sort controls are in the rail's Crews accordion, not here.
import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { Briefcase, Users } from 'lucide-react'
import { useIssueRadar } from '../context'
import type { Crew } from '../api'
import { compareText, fmtNumber } from '../../../i18n/format'
import { Badge, EmptyState } from '../../../components/ui'
import Clickable from '../../../components/Clickable'
import CrewGhost from './CrewGhost'
import ListSkeleton from './ListSkeleton'

/** The four statuses `GET /crews` derives per crew, mirroring `_crew_status` in
 * `backend/crew_routes.py`. A dot can only be one colour, so the backend picks
 * one by what the USER has to do about it: paused (doing nothing regardless of
 * what it holds) → needs_you (waiting on a human) → working (needs nothing) →
 * idle (none of the three). */
const CREW_STATUSES = ['paused', 'needs_you', 'working', 'idle'] as const
type CrewStatus = (typeof CREW_STATUSES)[number]

/**
 * Read the route-derived `status` field off a crew record.
 *
 * `Crew` in `api.ts` mirrors the STORE record, and `status` is computed by the
 * crews ROUTE from each crew's open work items (`_crews_page`) — it exists so the
 * phase taxonomy stays in one language instead of `PARKED_PHASES` being re-encoded
 * in TypeScript. Read structurally, and validated against the list above, because
 * `api.ts` is owned elsewhere in this change: the moment `Crew` declares
 * `status: CrewStatus` this collapses to `c.status`. Unknown/absent → `idle`,
 * which renders a neutral dot rather than claiming work that may not exist.
 */
function crewStatus(c: Crew): CrewStatus {
  const raw = (c as { status?: unknown }).status
  return (CREW_STATUSES as readonly unknown[]).includes(raw) ? (raw as CrewStatus) : 'idle'
}

/** Dot colour per status — a traffic light, so the roster is readable without
 * reading any word: red needs you, green is making progress, yellow is on duty
 * with nothing in flight, grey is switched off.
 *
 * `--ok` / `--warn` / `--danger` rather than the accent, because the accent is the
 * theme's brand colour and changes per theme: a dot that means "healthy" cannot be
 * the same hue as a selected border. */
const DOT_CLASS: Record<CrewStatus, string> = {
  needs_you: 'bg-danger',
  working: 'bg-ok',
  idle: 'bg-warn',
  paused: 'bg-muted-strong',
}

/** Status word per status, as FULL literal catalog keys.
 *
 * Not `t(`…status_${status}`)`: a key assembled from parts exists nowhere in the
 * source, so the extractor cannot see it and the dangling-reference gate cannot
 * verify it — it simply renders as the raw key the day it goes missing. Same
 * pattern as `STATUS_LABEL_KEY` in `pages/chat/McpToolsPanel.tsx`. */
const STATUS_KEY: Record<CrewStatus, string> = {
  working: 'apps.issueRadar.views.crews.status_working',
  needs_you: 'apps.issueRadar.views.crews.status_needs_you',
  paused: 'apps.issueRadar.views.crews.status_paused',
  idle: 'apps.issueRadar.views.crews.status_idle',
}

/** Rank for the `status` sort, ascending = what needs a human first.
 *
 * Mirrors the priority `_crew_status` resolves ties with, but ordered for a
 * READER rather than for a dot's colour: a crew waiting on you is the only row
 * that cannot make progress without you, so it leads; paused sinks to the bottom
 * because it is doing nothing by your own instruction. */
const STATUS_RANK: Record<CrewStatus, number> = {
  needs_you: 0,
  working: 1,
  idle: 2,
  paused: 3,
}

/** True when a crew is switched off but not retired — the backend's own `paused`
 * flag (`_crew_flags`), re-derived here rather than read from `status` because
 * `status` picks ONE label: a paused crew is filtered on this predicate, so the
 * Paused chip's rows always match its count. */
const isPaused = (c: Crew) => c.enabled === false && !c.retired_at

export default function CrewList() {
  const { t } = useTranslation()
  const {
    crews, crewCounts, crewsLoading, crewsError,
    crewView, setCrewView, crewFilter, crewSortKey, crewSortDir,
  } = useIssueRadar()

  /** The rows the active filter shows.
   *
   * `paused` uses the backend's own flag (see `isPaused`), so those rows and that
   * filter's count always agree. `working` / `needs_you` fall back to the
   * single-valued `status`, which differs from the flags in exactly one case: a
   * PAUSED crew that also has work in flight or an escalation is shown under
   * Paused only, while the other count still includes it. Closing that gap needs
   * the three per-crew booleans in the payload, not a different client-side rule —
   * re-deriving them here would mean re-encoding the phase taxonomy the `status`
   * field exists to keep server-side. */
  const shown = useMemo(() => {
    const filtered = crews.filter((c) => {
      if (crewFilter === 'all') return true
      if (crewFilter === 'paused') return isPaused(c)
      return crewStatus(c) === crewFilter
    })
    const dir = crewSortDir === 'asc' ? 1 : -1
    // Name is the tiebreak on every field, so a poll that returns the roster in a
    // different order cannot reshuffle equal rows under the reader's cursor.
    // `compareText` collates in the APP's language; `localeCompare` would use the
    // host's and ignore the language the user picked.
    const byName = (a: Crew, b: Crew) => compareText(a.name, b.name)
    return [...filtered].sort((a, b) => {
      if (crewSortKey === 'name') return dir * byName(a, b)
      if (crewSortKey === 'created') {
        const d = Date.parse(a.created_at) - Date.parse(b.created_at)
        return d !== 0 ? dir * d : byName(a, b)
      }
      const r = STATUS_RANK[crewStatus(a)] - STATUS_RANK[crewStatus(b)]
      return r !== 0 ? dir * r : byName(a, b)
    })
  }, [crews, crewFilter, crewSortKey, crewSortDir])

  const deskSelected = crewView.kind === 'desk'

  /** Card contract copied from the issue and PR list columns, so all three
   * columns read as one component: rounded, bordered, `bg-card`, accent border
   * when selected. */
  const cardClass = (isSel: boolean) =>
    `w-full text-left rounded-lg border p-2.5 cursor-pointer bg-card hover:bg-bg-hover transition-colors ${
      isSel ? 'border-accent' : 'border-border'
    }`

  return (
    // No right border, matching the issue and PR list columns: in this app the
    // ResizeHandle is the seam between columns, and a border here would make the
    // roster the only one of the three with a hard edge.
    <section className="flex flex-col min-h-0 h-full">
      {/* The filter and sort controls for this column live in the rail's Crews
          accordion, exactly as the issue and PR columns take theirs from theirs —
          this column is the roster and nothing else. */}
      <div
        className="flex-1 min-h-0 overflow-y-auto scrollbar-none px-2 pt-2 pb-2 flex flex-col gap-2"
        style={{ scrollbarWidth: 'none' }}
      >
        {/* Your Desk — pinned above the roster, never filtered out: the filters
            narrow the CREW list, and the desk is where the escalations they point
            at are answered. */}
        <Clickable
          onClick={() => setCrewView({ kind: 'desk' })}
          aria-current={deskSelected ? 'page' : undefined}
          data-testid="crew-desk-row"
          className={`${cardClass(deskSelected)} flex items-start gap-2.5 flex-shrink-0`}
        >
          <span className="w-[34px] flex justify-center pt-0.5 flex-shrink-0">
            <Briefcase size={17} className="text-accent" />
          </span>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-1.5 text-[13px] font-semibold text-text-strong">
              <span>{t('apps.issueRadar.views.crews.your_desk')}</span>
              {crewCounts.needs_you > 0 && (
                <Badge variant="err" className="text-[11px] px-1.5 py-0">
                  {fmtNumber(crewCounts.needs_you)}
                </Badge>
              )}
            </div>
            <div className="text-[12px] text-muted mt-0.5 leading-snug">
              {crewCounts.needs_you > 0
                ? t('apps.issueRadar.views.crews.desk_sub_blocked', {
                  n: fmtNumber(crewCounts.needs_you), total: fmtNumber(crewCounts.on_duty),
                })
                : t('apps.issueRadar.views.crews.desk_sub_clear', {
                  n: fmtNumber(crewCounts.working), total: fmtNumber(crewCounts.on_duty),
                })}
            </div>
          </div>
        </Clickable>

        {/* Group label — the roster's size AS SHOWN, so a filter that hides rows
            is visible in the count rather than contradicting it. No band or rule
            of its own now that each row is a card. */}
        <div className="px-1 pt-1 text-[11px] uppercase tracking-[.06em] text-muted flex-shrink-0">
          {t('apps.issueRadar.views.crews.group_crew')} · {fmtNumber(shown.length)}
        </div>

        {crewsError && (
          <div className="px-1 text-[13px] text-danger">{crewsError.message}</div>
        )}

        {crewsLoading && <ListSkeleton count={3} />}

        {!crewsLoading && !crewsError && shown.length === 0 && (
          <EmptyState
            icon={<Users size={34} strokeWidth={1.5} />}
            title={crews.length === 0
              ? t('apps.issueRadar.views.crews.empty_title')
              : t('apps.issueRadar.views.crews.filtered_empty_title')}
            subtitle={crews.length === 0
              ? t('apps.issueRadar.views.crews.empty_sub')
              : t('apps.issueRadar.views.crews.filtered_empty_sub')}
            testId="crew-list-empty"
          />
        )}

        {shown.map((c) => (
          <CrewRow
            key={c.id}
            crew={c}
            selected={crewView.kind === 'crew' && crewView.id === c.id}
            onSelect={() => setCrewView({ kind: 'crew', id: c.id })}
            cardClass={cardClass}
          />
        ))}
      </div>
    </section>
  )
}

/** One roster card: the crew's face, a status dot, its name, the status word, and
 * a one-line summary of what it is doing. */
function CrewRow({ crew, selected, onSelect, cardClass }: {
  crew: Crew
  selected: boolean
  onSelect: () => void
  /** The column's shared card style, so this row cannot drift from Your Desk's. */
  cardClass: (isSel: boolean) => string
}) {
  const { t } = useTranslation()
  const status = crewStatus(crew)
  const statusWord = t(STATUS_KEY[status])

  /** The row's second line.
   *
   * `GET /crews` answers with crew RECORDS plus repo-wide tallies — it carries no
   * work items, so the newest item's issue number and phase are not available to
   * this list without one request per crew. The line therefore says the most
   * specific true thing the record supports: why it is paused, that it is waiting
   * on you, or what it is scoped to. Once the payload carries a per-crew summary
   * of the newest open item, this is the one function to change.
   */
  const summary = status === 'paused'
    ? (crew.paused_reason || t('apps.issueRadar.views.crews.sub_paused'))
    : status === 'needs_you'
      ? t('apps.issueRadar.views.crews.sub_needs_you')
      : crew.labels.length > 0
        ? crew.labels.join(' · ')
        : t('apps.issueRadar.views.crews.sub_any_issue')

  return (
    <Clickable
      onClick={onSelect}
      aria-current={selected ? 'page' : undefined}
      title={crew.name}
      data-testid={`crew-row-${crew.id}`}
      className={`${cardClass(selected)} flex items-center gap-2.5 flex-shrink-0`}
    >
      {/* The ghost sprite is near-white, so on a light theme it disappears into
          the card. The tinted rounded tile is what makes it legible there; it is
          a theme token rather than a fixed grey so a dark theme keeps the same
          gentle lift instead of a bright patch. */}
      <span className="w-[38px] h-[38px] rounded-lg bg-bg-accent border border-border flex items-center justify-center flex-shrink-0 overflow-hidden">
        {/* Decorative: the crew's name is rendered as text beside it, so the
            avatar adds no information a reader would otherwise miss. */}
        <CrewGhost seed={crew.avatar_seed} variant={crew.avatar_variant} size={34} />
      </span>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-1.5">
          <span className="text-[13px] font-semibold text-text-strong truncate">{crew.name}</span>
          <span className="text-[11px] text-muted flex-shrink-0">{statusWord}</span>
        </div>
        {/* One line, clipped: a row is a summary, and a wrapping reason would
            reflow the whole roster as phases change under a poll. */}
        <div className="text-[12px] text-muted mt-0.5 leading-snug truncate">{summary}</div>
      </div>
      {/* Far right, vertically centred: the dots line up in a single column down
          the roster, so "which crew needs me" is one glance rather than a scan
          through names of differing length. `title` carries the same word the row
          already shows, for a pointer that lands on the dot itself. */}
      <span
        className={`w-[9px] h-[9px] rounded-full flex-shrink-0 ${DOT_CLASS[status]}`}
        title={statusWord}
        data-testid={`crew-row-dot-${crew.id}`}
      />
    </Clickable>
  )
}
