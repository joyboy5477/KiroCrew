/**
 * CrewProtocolSettings — the repo-wide rules every crew in one repository
 * negotiates by: how long a claim lives, how long an escalation waits on a human
 * before the claim is released, and the trailer each crew signs its commits with.
 *
 * ## Why this is a settings page and not part of Your Desk
 *
 * These three values are PER-REPO, not per-desk and not per-crew: the API behind
 * them is `GET`/`PUT /crews/settings?owner&repo`, and two crews in one repo
 * negotiating with different TTLs is exactly how a short-TTL crew steals a
 * long-TTL crew's live work. So they belong beside the repo's other preferences,
 * where a value that governs the whole repository is expected to live — not on
 * the escalation inbox, which is a queue a human works through.
 *
 * Your Desk still READS them (the hand-back window is part of what its "needs
 * your decision" tile says), through the same query key this component writes,
 * so a save here is visible there without a refetch.
 *
 * ## One key per write
 *
 * Every commit sends a ONE-KEY merge patch, never the whole document.
 * `putCrewSettings` merges server-side, so a one-key patch needs no revision
 * guard and two tabs editing different fields cannot erase each other. That is
 * what makes this page safe to leave open next to Your Desk.
 *
 * The section chrome (icon, heading, description, footnote) is supplied by
 * `RepoSettings`, so this renders the FIELDS only and inherits that page's look
 * instead of re-deriving it.
 */
import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'

import { Input } from '../../../../components/ui'
import {
  issueRadarApi,
  type CrewSettings, type CrewSettingsPatch, type RepoRef,
} from '../../api'
import { repoScopeKey } from '../../lib/links'

/** Which settings field a draft edit belongs to. */
type SettingsField = 'claim_ttl_hours' | 'escalation_handback_days' | 'commit_trailer'

export default function CrewProtocolSettings({
  repoRef, settings,
}: {
  repoRef: RepoRef
  /** The repo's saved protocol settings, or `undefined` while they load — every
   *  field is disabled until they arrive, because a commit needs the old value to
   *  tell a real edit from a no-op. */
  settings: CrewSettings | undefined
}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const scope = repoScopeKey(repoRef)
  // An OVERLAY of uncommitted edits, not a copy of the record: the displayed
  // value falls back to the server's whenever no edit is in flight, so a poll
  // that lands while a field is untouched is picked up without an effect and
  // without clobbering what is being typed in a different field.
  const [drafts, setDrafts] = useState<Partial<Record<SettingsField, string>>>({})
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)

  const save = useMutation({
    mutationFn: (patch: CrewSettingsPatch) => issueRadarApi.putCrewSettings(repoRef, patch),
    onMutate: () => { setError(''); setSaved(false) },
    onSuccess: (res) => {
      setSaved(true)
      queryClient.setQueryData(['issue-radar', 'crew-settings', scope], res)
      void queryClient.invalidateQueries({ queryKey: ['issue-radar', 'crews', scope] })
    },
    onError: (e: unknown) => setError(e instanceof Error ? e.message : String(e)),
  })

  const shown = (field: SettingsField): string => {
    const draft = drafts[field]
    if (draft !== undefined) return draft
    if (!settings) return ''
    return String(settings[field])
  }

  const edit = (field: SettingsField, value: string) => setDrafts((d) => ({ ...d, [field]: value }))

  /** Commit ONE field as a one-key merge patch.
   *
   * One key, never the whole document: `putCrewSettings` merges server-side, so
   * sending only what changed means two tabs editing different fields cannot
   * erase each other. A no-op edit sends nothing at all. */
  const commit = (field: SettingsField) => {
    const raw = drafts[field]
    if (raw === undefined || !settings) return
    setDrafts((d) => { const next = { ...d }; delete next[field]; return next })
    if (field === 'commit_trailer') {
      const trailer = raw.trim()
      if (!trailer || trailer === settings.commit_trailer) return
      save.mutate({ commit_trailer: trailer })
      return
    }
    const n = Number.parseInt(raw, 10)
    if (!Number.isFinite(n) || n <= 0 || n === settings[field]) return
    save.mutate({ [field]: n } as CrewSettingsPatch)
  }

  const numericField = (field: 'claim_ttl_hours' | 'escalation_handback_days', label: string, hint: string, unit: string, testId: string) => (
    <div className="flex flex-col gap-1.5">
      <label className="text-[13px] font-semibold text-text-strong" htmlFor={`crew-desk-${field}`}>{label}</label>
      <div className="flex items-center gap-2">
        <Input
          id={`crew-desk-${field}`}
          type="number"
          min={1}
          value={shown(field)}
          onChange={(e) => edit(field, e.target.value)}
          onBlur={() => commit(field)}
          onKeyDown={(e) => { if (e.key === 'Enter') commit(field) }}
          disabled={!settings}
          className="max-w-[140px] flex-none"
          data-testid={testId}
        />
        <span className="text-[13px] text-muted">{unit}</span>
      </div>
      <div className="text-[13px] text-muted">{hint}</div>
    </div>
  )

  const ttl = settings?.claim_ttl_hours ?? 0
  const handback = settings?.escalation_handback_days ?? 0

  // The `crew-desk-*` element ids and test ids are kept verbatim from where this
  // block used to render, so a harness or test that already addresses a field
  // keeps addressing the same one across the move.
  return (
    <div data-testid="crew-desk-protocol">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-4">
        {numericField(
          'claim_ttl_hours',
          t('apps.issueRadar.views.crews.desk.claim_ttl_label'),
          t('apps.issueRadar.views.crews.desk.claim_ttl_hint'),
          t('apps.issueRadar.views.crews.desk.unit_hours', { count: ttl }),
          'crew-desk-claim-ttl',
        )}
        {numericField(
          'escalation_handback_days',
          t('apps.issueRadar.views.crews.desk.handback_label'),
          t('apps.issueRadar.views.crews.desk.handback_hint'),
          t('apps.issueRadar.views.crews.desk.unit_days', { count: handback }),
          'crew-desk-handback',
        )}
      </div>
      <div className="mt-4 flex flex-col gap-1.5">
        <label className="text-[13px] font-semibold text-text-strong" htmlFor="crew-desk-commit-trailer">
          {t('apps.issueRadar.views.crews.desk.trailer_label')}
        </label>
        {/* Mono on purpose, and the one field here that keeps it: the value is a
            git trailer template written verbatim into commit messages, so it is
            code, not prose. */}
        <Input
          id="crew-desk-commit-trailer"
          value={shown('commit_trailer')}
          onChange={(e) => edit('commit_trailer', e.target.value)}
          onBlur={() => commit('commit_trailer')}
          onKeyDown={(e) => { if (e.key === 'Enter') commit('commit_trailer') }}
          disabled={!settings}
          className="font-mono w-full"
          data-testid="crew-desk-commit-trailer"
        />
        <div className="text-[13px] text-muted">{t('apps.issueRadar.views.crews.desk.trailer_hint')}</div>
      </div>
      {/* Updates in place after a save, so it must announce itself. */}
      <div
        aria-live="polite"
        className="mt-3 text-[13px] min-h-[1.25rem]"
        data-testid="crew-desk-protocol-status"
        data-state={save.isPending ? 'saving' : error ? 'failed' : saved ? 'saved' : 'idle'}
      >
        {save.isPending && <span className="text-muted">{t('apps.issueRadar.views.crews.desk.settings_saving')}</span>}
        {!save.isPending && error && <span className="text-danger">{t('apps.issueRadar.views.crews.desk.settings_failed', { error })}</span>}
        {!save.isPending && !error && saved && <span className="text-ok">{t('apps.issueRadar.views.crews.desk.settings_saved')}</span>}
      </div>
    </div>
  )
}
