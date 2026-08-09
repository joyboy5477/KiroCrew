/**
 * CrewDesk — Your Desk, the human's escalation inbox.
 *
 * Five behaviours are pinned here, each of which was a real decision rather than
 * an implementation detail:
 *
 *   1. Nothing escalated is an EMPTY STATE, not a blank region — a desk with no
 *      queue must say so, or the human cannot tell it apart from a failed load.
 *   2. The guidance composer posts `(ref, crewId, issueNumber, text)` in that
 *      order. The crew id and the issue number are different identifiers of
 *      similar shape, so a transposition is silent at runtime.
 *   3. `injected: false` renders the "recorded but not delivered" notice, NOT an
 *      error. The write succeeded; only the live hand-off to the crew's session
 *      did not, and reporting that as a failure would make a human re-send
 *      guidance the crew already holds.
 *   4. The create button sits BETWEEN the stat row and the queue. It is the only
 *      way to hire a crew now that the roster column carries no "+", so its
 *      presence and its wiring are both asserted.
 *   5. The repo's protocol settings are NOT on this page — they moved to that
 *      repo's own settings page, since they govern every crew in the repo rather
 *      than this human's queue. Pinned here so a re-add is caught. Their own
 *      behaviour lives in `CrewProtocolSettings.test.tsx`.
 *
 * The api module is mocked wholesale: no test here touches the network.
 *
 * Assertions are on `data-testid` / `data-state` rather than on rendered English.
 * The view's catalog keys are handed to the i18n owner as a manifest and are not
 * in `en.json` yet, so i18next currently echoes the raw key back — text
 * assertions would pin that placeholder rather than the behaviour.
 */
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { Crew, CrewSettings, WorkItem } from '../apps/issue-radar/api'

const ACTIVE = { owner: 'kirodotdev', repo: 'KiroCrew' } // brand-ok: the repository name

vi.mock('../apps/issue-radar/context', () => ({
  useIssueRadar: () => ({ active: ACTIVE }),
}))

// The avatar paints on a canvas; happy-dom has no 2D context worth exercising
// here, and the desk's behaviour does not depend on the pixels.
vi.mock('../apps/issue-radar/components/CrewGhost', () => ({
  default: ({ seed }: { seed: string }) => <div data-testid="crew-ghost" data-seed={seed} />,
}))

const api = {
  crews: vi.fn(),
  crew: vi.fn(),
  crewEscalations: vi.fn(),
  getCrewSettings: vi.fn(),
  sendCrewGuidance: vi.fn(),
  recordCrewWork: vi.fn(),
}
vi.mock('../apps/issue-radar/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../apps/issue-radar/api')>()),
  issueRadarApi: api,
}))

const CrewDesk = (await import('../apps/issue-radar/views/CrewDesk')).default

const SETTINGS: CrewSettings = {
  schema: 1,
  claim_ttl_hours: 48,
  escalation_handback_days: 3,
  commit_trailer: 'Crew: {name} (Kiro Crew Issue Radar)',
}

function crew(over: Partial<Crew> = {}): Crew {
  return {
    schema: 1,
    id: 'crew-whirlpool',
    name: 'Whirlpool',
    avatar_seed: 'crew-whirlpool',
    avatar_variant: null,
    agent: 'kirocrew',
    model: '',
    extra_prompt: '',
    labels: [],
    auto_resolve_conflicts: false,
    auto_merge: false,
    unattended: false,
    max_open: 3,
    max_escalated: 2,
    worktree_root: '',
    slot_key: 'chat-whirlpool',
    enabled: true,
    paused_reason: '',
    created_at: '2026-08-08T00:00:00Z',
    retired_at: null,
    ...over,
  }
}

function workItem(over: Partial<WorkItem> = {}): WorkItem {
  return {
    schema: 1,
    crew_id: 'crew-whirlpool',
    owner: ACTIVE.owner,
    repo: ACTIVE.repo,
    number: 2251,
    phase: 'escalated',
    outcome: null,
    decision: '',
    why: 'os.fchmod crashes the gateway on Windows',
    next: '',
    tried: [],
    worktree: '',
    branch: '',
    base_sha: '',
    pr_number: null,
    ci_state: {},
    claim_comment_id: 1,
    labels_applied: ['crew: needs decision'],
    escalation: {
      question: 'Two fixes both stop the crash but they are not equivalent.',
      options: ['Do (b) instead', 'Ask the requester'],
      recommendation: '(a) plus a warning log.',
      at: new Date(Date.now() - 38 * 60_000).toISOString(),
    },
    claimed_at: '2026-08-08T00:00:00Z',
    last_progress_at: '2026-08-08T00:00:00Z',
    finished_at: null,
    ...over,
  }
}

function mount(onCreate: () => void = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <CrewDesk onCreate={onCreate} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  api.crews.mockResolvedValue({
    ...ACTIVE,
    crews: [crew()],
    settings: SETTINGS,
    counts: { on_duty: 6, working: 3, needs_you: 1, paused: 1 },
  })
  api.crew.mockResolvedValue({ crew: crew(), items: [], events: [], counts: { open: 1, escalated: 1 } })
  api.crewEscalations.mockResolvedValue({ escalations: [{ crew: crew(), item: workItem() }] })
  api.getCrewSettings.mockResolvedValue({ settings: SETTINGS })
  api.sendCrewGuidance.mockResolvedValue({ ok: true, injected: true })
  api.recordCrewWork.mockResolvedValue({ item: workItem({ phase: 'handed-back' }), event: null })
})

describe('CrewDesk — empty queue', () => {
  it('says the desk is clear instead of rendering an empty region', async () => {
    // A blank area is indistinguishable from a failed load. The desk is the one
    // surface whose whole job is "is anything waiting on me", so zero has to be
    // stated.
    api.crewEscalations.mockResolvedValue({ escalations: [] })
    mount()
    await waitFor(() => expect(screen.getByTestId('crew-desk-empty')).toBeTruthy())
    expect(screen.queryByTestId('crew-desk-row')).toBeNull()
    // The rest of the desk still renders — the stat row and the create button are
    // not part of the queue.
    expect(screen.getByTestId('crew-desk-stats')).toBeTruthy()
    expect(screen.getByTestId('crew-create')).toBeTruthy()
  })
})

describe('CrewDesk — guidance composer', () => {
  it('posts the ref, crew id, issue number and text, in that order', async () => {
    mount()
    const box = await screen.findByTestId('crew-desk-guidance-input')
    await userEvent.type(box, 'Go with (a).')
    await userEvent.click(screen.getByTestId('crew-desk-guidance-send'))
    await waitFor(() => expect(api.sendCrewGuidance).toHaveBeenCalledTimes(1))
    // The crew id and the issue number are both "the identifier of the thing
    // being answered"; only the argument position distinguishes them.
    expect(api.sendCrewGuidance).toHaveBeenCalledWith(ACTIVE, 'crew-whirlpool', 2251, 'Go with (a).')
  })

  it('prefills the composer from a quick action rather than sending it', async () => {
    // The crew's recommendation is the fast path, but "agree" is rarely
    // unqualified — the human almost always adds a constraint. Sending on click
    // would take that chance away.
    mount()
    await userEvent.click(await screen.findByTestId('crew-desk-quick-approve'))
    const box = screen.getByTestId('crew-desk-guidance-input') as HTMLTextAreaElement
    // Trailing blank line, so the constraint typed next starts on its own
    // paragraph. A recording of this flow showed the two running together
    // ("…directory alreadyGo with (a), but…") when the prefill ended flush.
    expect(box.value).toBe('(a) plus a warning log.\n\n')
    await userEvent.type(box, 'Also file the follow-up.')
    expect(box.value).toBe('(a) plus a warning log.\n\nAlso file the follow-up.')
    expect(api.sendCrewGuidance).not.toHaveBeenCalled()
  })

  it('trims the prefill whitespace off the sent guidance', async () => {
    // The separator is a composer affordance, not part of the message — it must
    // not reach the crew's session as trailing blank lines.
    mount()
    await userEvent.click(await screen.findByTestId('crew-desk-quick-approve'))
    await userEvent.click(screen.getByTestId('crew-desk-guidance-send'))
    expect(api.sendCrewGuidance).toHaveBeenCalledWith(
      expect.anything(), expect.anything(), expect.anything(), '(a) plus a warning log.',
    )
  })

  it('reports an unreachable session as recorded-not-delivered, not as an error', async () => {
    // `ok: true, injected: false` — the write landed, the crew's session was not
    // live to take it. Rendering that as an error makes the human re-send
    // guidance the crew already holds.
    api.sendCrewGuidance.mockResolvedValue({ ok: true, injected: false })
    mount()
    await userEvent.type(await screen.findByTestId('crew-desk-guidance-input'), 'Go with (a).')
    await userEvent.click(screen.getByTestId('crew-desk-guidance-send'))
    const status = await screen.findByTestId('crew-desk-guidance-status')
    await waitFor(() => expect(status.getAttribute('data-state')).toBe('stored'))
    expect(status.getAttribute('data-state')).not.toBe('failed')
    // It updates in place, so assistive technology has to be told.
    expect(status.getAttribute('aria-live')).toBe('polite')
    // The notice is styled as a caution, never as a failure.
    expect(status.querySelector('.text-warn')).toBeTruthy()
    expect(status.querySelector('.text-danger')).toBeNull()
  })

  it('does report a rejected write as a failure', async () => {
    // The counterpart to the case above: without this, "not an error" could be
    // satisfied by never showing an error at all.
    api.sendCrewGuidance.mockRejectedValue(new Error('403 forbidden'))
    mount()
    await userEvent.type(await screen.findByTestId('crew-desk-guidance-input'), 'Go with (a).')
    await userEvent.click(screen.getByTestId('crew-desk-guidance-send'))
    const status = await screen.findByTestId('crew-desk-guidance-status')
    await waitFor(() => expect(status.getAttribute('data-state')).toBe('failed'))
  })
})

describe('CrewDesk — create a crew', () => {
  it('raises the create dialog from the desk, between the stats and the queue', async () => {
    // The roster column no longer carries a "+", so this button is the ONLY way
    // to hire a crew. If it stops being wired the feature has no entry point at
    // all, and nothing else in the app would fail.
    const onCreate = vi.fn()
    mount(onCreate)
    const button = await screen.findByTestId('crew-create')
    await userEvent.click(button)
    expect(onCreate).toHaveBeenCalledTimes(1)

    // Order on the page, asserted structurally rather than by pixel: the button
    // belongs after the stat row and before the queue. `compareDocumentPosition`
    // is the only check that survives a class-name change.
    const stats = screen.getByTestId('crew-desk-stats')
    const queue = screen.getByTestId('crew-desk-queue')
    expect(stats.compareDocumentPosition(button) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(queue.compareDocumentPosition(button) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy()
  })
})

describe('CrewDesk — protocol settings live on the settings page', () => {
  it('renders no protocol fields on the desk', async () => {
    // These moved to the repo's own settings page: they are repo-wide, and a
    // second copy on the desk would be a second place to change one value.
    mount()
    await waitFor(() => expect(screen.getByTestId('crew-desk-stats')).toBeTruthy())
    expect(screen.queryByTestId('crew-desk-protocol')).toBeNull()
    expect(screen.queryByTestId('crew-desk-claim-ttl')).toBeNull()
    expect(screen.queryByTestId('crew-desk-commit-trailer')).toBeNull()
  })
})

describe('CrewDesk — queue', () => {
  it('expands the longest-waiting escalation and collapses the rest', async () => {
    const older = crew({ id: 'crew-cocoon', name: 'Cocoon', avatar_seed: 'crew-cocoon', slot_key: 'chat-cocoon' })
    api.crewEscalations.mockResolvedValue({
      escalations: [
        // Deliberately newest-first on the wire, so ordering cannot pass by luck.
        { crew: crew(), item: workItem({ escalation: { ...workItem().escalation!, at: new Date(Date.now() - 60_000).toISOString() } }) },
        {
          crew: older,
          item: workItem({
            crew_id: 'crew-cocoon',
            number: 2263,
            escalation: { question: 'Needs a schema migration.', options: [], recommendation: '', at: new Date(Date.now() - 3 * 3_600_000).toISOString() },
          }),
        },
      ],
    })
    mount()
    await waitFor(() => expect(screen.getAllByTestId('crew-desk-row').length).toBe(2))
    const names = screen.getAllByTestId('crew-desk-row-name').map((n) => n.textContent)
    expect(names).toEqual(['Cocoon', 'Whirlpool'])
    // Only the first card carries the composer.
    expect(screen.getAllByTestId('crew-desk-guidance-input').length).toBe(1)
    expect(screen.getAllByTestId('crew-desk-row')[0].contains(screen.getByTestId('crew-desk-guidance-input'))).toBe(true)
  })

  it('hands an escalation back through the work-item ledger', async () => {
    mount()
    await userEvent.click(await screen.findByTestId('crew-desk-hand-back'))
    await waitFor(() => expect(api.recordCrewWork).toHaveBeenCalledTimes(1))
    const [ref, id, number, patch] = api.recordCrewWork.mock.calls[0]
    expect([ref, id, number]).toEqual([ACTIVE, 'crew-whirlpool', 2251])
    expect(patch.phase).toBe('handed-back')
    // A phase change with no logged reason is invisible on the crew page and in
    // the claim comment, so the ledger line rides along in the same request.
    expect(patch.event_kind).toBe('handback')
    expect(patch.event).toBeTruthy()
  })

  it('links out to the issue and in to the crew session', async () => {
    mount()
    const issue = await screen.findByTestId('crew-desk-row-issue-link')
    expect(issue.getAttribute('href')).toBe('https://github.com/kirodotdev/KiroCrew/issues/2251')
    expect(issue.getAttribute('rel')).toBe('noreferrer')
    // The crew's session is a dashboard route, so it must not reload the app.
    expect(screen.getByTestId('crew-desk-row-session-link').getAttribute('href'))
      .toBe('/chat?sid=chat-whirlpool')
  })

})
