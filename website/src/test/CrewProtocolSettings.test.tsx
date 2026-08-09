/**
 * CrewProtocolSettings — the repo's crew protocol, on the repo's settings page.
 *
 * These assertions moved here with the component (they used to live in
 * `CrewDesk.test.tsx`), because the block itself moved: the values are per-repo,
 * so they belong on the per-repo settings page rather than on the human's
 * escalation inbox.
 *
 * Three behaviours are pinned:
 *
 *   1. A write is a ONE-KEY merge patch, never the whole document. That is what
 *      stops two tabs editing different fields from erasing each other, and it is
 *      why the write path needs no revision guard.
 *   2. Committing a field UNCHANGED writes nothing, so tabbing across the form
 *      does not generate traffic or a spurious "Saved."
 *   3. The write is addressed to the repo the PAGE is for, which is not
 *      necessarily the active repo — this page can be opened for any connected
 *      repository from the rail.
 *
 * Assertions are on `data-testid` / `data-state`, not rendered English: the
 * catalog keys are shared with Your Desk and i18next echoes an unresolved key
 * back, so a text assertion would pin the placeholder rather than the behaviour.
 */
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { CrewSettings } from '../apps/issue-radar/api'

// brand-ok: the repository name
const PAGE_REPO = { owner: 'kirodotdev', repo: 'KiroCrew' } // brand-ok: the repository name

const api = {
  getCrewSettings: vi.fn(),
  putCrewSettings: vi.fn(),
}
vi.mock('../apps/issue-radar/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../apps/issue-radar/api')>()),
  issueRadarApi: api,
}))

const CrewProtocolSettings = (
  await import('../apps/issue-radar/views/settings/CrewProtocolSettings')
).default

const SETTINGS: CrewSettings = {
  schema: 1,
  claim_ttl_hours: 48,
  escalation_handback_days: 3,
  commit_trailer: 'Crew: {name} (Kiro Crew Issue Radar)',
}

/** `settings` is passed EXPLICITLY, with no default: a default parameter is used
 *  for `undefined` too, so the "still loading" case could never be reached. */
function mount(settings: CrewSettings | undefined) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <CrewProtocolSettings repoRef={PAGE_REPO} settings={settings} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  api.getCrewSettings.mockResolvedValue({ settings: SETTINGS })
  api.putCrewSettings.mockImplementation(async (_ref: unknown, patch: Partial<CrewSettings>) => ({
    settings: { ...SETTINGS, ...patch },
  }))
})

describe('CrewProtocolSettings', () => {
  it('sends a one-key merge patch for the field that changed', async () => {
    // A whole-document write would need a revision guard; a one-key merge does
    // not, and it is what makes two tabs editing different fields safe.
    mount(SETTINGS)
    const ttl = screen.getByTestId('crew-desk-claim-ttl')
    expect((ttl as HTMLInputElement).value).toBe('48')
    await userEvent.clear(ttl)
    await userEvent.type(ttl, '24')
    await userEvent.tab()
    await waitFor(() => expect(api.putCrewSettings).toHaveBeenCalledTimes(1))
    // Addressed to the repo whose settings page this is — NOT to the active repo.
    // The rail opens this page for any connected repository.
    expect(api.putCrewSettings).toHaveBeenCalledWith(PAGE_REPO, { claim_ttl_hours: 24 })
    // Exactly one key: the other two fields must not ride along.
    expect(Object.keys(api.putCrewSettings.mock.calls[0][1])).toEqual(['claim_ttl_hours'])
  })

  it('writes nothing when a field is committed unchanged', async () => {
    mount(SETTINGS)
    const trailer = screen.getByTestId('crew-desk-commit-trailer')
    expect((trailer as HTMLInputElement).value).toBe(SETTINGS.commit_trailer)
    await userEvent.click(trailer)
    await userEvent.tab()
    expect(api.putCrewSettings).not.toHaveBeenCalled()
  })

  it('reports a rejected write in place, and says so out loud', async () => {
    api.putCrewSettings.mockRejectedValue(new Error('403 forbidden'))
    mount(SETTINGS)
    const days = screen.getByTestId('crew-desk-handback')
    await userEvent.clear(days)
    await userEvent.type(days, '5')
    await userEvent.tab()
    const status = await screen.findByTestId('crew-desk-protocol-status')
    await waitFor(() => expect(status.getAttribute('data-state')).toBe('failed'))
    // It updates in place, so assistive technology has to be told.
    expect(status.getAttribute('aria-live')).toBe('polite')
  })

  it('disables every field until the settings have loaded', () => {
    // Without the saved values a commit cannot tell a real edit from a no-op, so
    // the form is disabled rather than merely empty.
    mount(undefined)
    for (const id of ['crew-desk-claim-ttl', 'crew-desk-handback', 'crew-desk-commit-trailer']) {
      expect((screen.getByTestId(id) as HTMLInputElement).disabled).toBe(true)
    }
  })
})
