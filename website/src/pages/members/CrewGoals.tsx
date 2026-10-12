import { useId, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { ChevronDown, FileText, Goal, Pencil, Plus, Trash2, Undo2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { api, type GoalKeyResult, type GoalSchedules, type MemberGoals, type ShortTermGoal } from '../../api/client'
import { Btn, Checkbox, IconButton, Input, Toggle } from '../../components/ui'
import SimpleSelect from '../../components/SimpleSelect'
import ErrorNotice from '../../components/ErrorNotice'
import { fmtRelative, fmtWeekday } from '../../i18n/format'
import { cn } from '../../lib/utils'

/** The query key every view of one crewmate's goal record shares. */
export const memberGoalsKey = (slug: string, member: string) => ['member-goals', slug, member] as const

export function useMemberGoals(slug: string, member: string, enabled = true) {
  return useQuery({
    queryKey: memberGoalsKey(slug, member),
    queryFn: () => api.memberGoals.get(slug, member),
    enabled: enabled && Boolean(slug && member),
  })
}

/** How far a reading is toward its target, 0..1, or null when it cannot be told.
 *  Numbers compare as numbers ("19" of "27"); a word target ("yes") is reached
 *  when the reading is that word. */
export function keyResultProgress(kr: Pick<GoalKeyResult, 'current' | 'target'>): number | null {
  if (!kr.current) return null
  const cur = parseFloat(kr.current)
  const tgt = parseFloat(kr.target)
  if (Number.isFinite(cur) && Number.isFinite(tgt) && tgt !== 0) return Math.max(0, Math.min(1, cur / tgt))
  return kr.current.trim().toLowerCase() === kr.target.trim().toLowerCase() ? 1 : 0
}

function Section({ title, children, testId, trailing }: { title: string; children: ReactNode; testId: string; trailing?: ReactNode }) {
  return (
    <section className="rounded-2xl border border-border bg-bg px-3.5 py-3" data-testid={testId}>
      <div className="flex items-center gap-2 mb-1.5">
        <div className="flex-1 text-[11px] font-semibold tracking-wide uppercase text-muted">{title}</div>
        {trailing}
      </div>
      {children}
    </section>
  )
}

function KeyResultBar({ kr }: { kr: GoalKeyResult }) {
  const { t } = useTranslation()
  const progress = keyResultProgress(kr)
  return (
    <div className="flex flex-col gap-1 min-w-0" data-testid="goal-key-result">
      <div className="flex items-baseline gap-2 min-w-0">
        <span className="flex-1 min-w-0 text-[13px] text-text truncate" title={kr.text}>{kr.text}</span>
        <span className="text-[12px] text-muted shrink-0 tabular-nums">
          {t('pages.membersPage.goals_kr_reading', { current: kr.current || '—', target: kr.target })}
        </span>
      </div>
      <div
        className="h-1.5 rounded-full bg-bg-hover overflow-hidden"
        role="progressbar"
        aria-label={kr.text}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progress === null ? undefined : Math.round(progress * 100)}
      >
        <div className="h-full rounded-full bg-accent" style={{ width: `${Math.round((progress ?? 0) * 100)}%` }} />
      </div>
      <span className="text-[12px] text-muted">
        {kr.updated_at
          ? t('pages.membersPage.goals_kr_read_at', { when: fmtRelative(kr.updated_at * 1000) })
          : t('pages.membersPage.goals_kr_not_read')}
      </span>
    </div>
  )
}

function Reports({ goals, limit }: { goals: MemberGoals; limit: number }) {
  const { t } = useTranslation()
  if (!goals.reports.length) return <p className="text-[13px] text-muted">{t('pages.membersPage.goals_reports_none')}</p>
  return (
    <ul className="flex flex-col gap-1.5" data-testid="goal-reports">
      {goals.reports.slice(0, limit).map(r => (
        <li key={r.slug} className="flex items-center gap-2 min-w-0">
          <FileText size={14} className="text-muted shrink-0" aria-hidden="true" />
          <Link to={'/artifacts/' + encodeURIComponent(r.slug)} className="flex-1 min-w-0 truncate text-[13px] text-accent hover:underline">{r.title}</Link>
          <span className="text-[12px] text-muted shrink-0">
            {t(r.kind === 'weekly' ? 'pages.membersPage.goals_report_weekly' : 'pages.membersPage.goals_report_monthly')} · {fmtRelative(r.at * 1000)}
          </span>
        </li>
      ))}
    </ul>
  )
}

/** The live goal picture for the crewmate's Dashboard tab: every value is read
 *  by the host from the goal record, so it shows nothing the record does not hold. */
export function CrewGoalsSummary({ slug, member }: { slug: string; member: string }) {
  const { t } = useTranslation()
  const q = useMemberGoals(slug, member)
  const goals = q.data
  // A failed read says so: hiding the summary would read as "no goal yet".
  if (q.isError) {
    return (
      <div className="px-4 pt-3">
        <ErrorNotice message={t('pages.membersPage.goals_load_failed')} askAgent testId="crew-goals-summary-error" />
      </div>
    )
  }
  if (!goals || !goals.long_term) return null
  const open = goals.short_term.filter(i => i.status === 'open')
  return (
    <div className="flex flex-col gap-2.5 px-4 pt-3" data-testid="crew-goals-summary">
      <Section title={t('pages.membersPage.goals_long_term')} testId="crew-goals-summary-goal">
        <p className="text-[13px] leading-relaxed text-text whitespace-pre-wrap">{goals.long_term}</p>
        {goals.key_results.length > 0 && (
          <div className="flex flex-col gap-2.5 mt-2.5">{goals.key_results.map(kr => <KeyResultBar key={kr.id} kr={kr} />)}</div>
        )}
        {open.length > 0 && (
          <ul className="mt-2.5 flex flex-col gap-1">
            {open.map(i => <li key={i.id} className="text-[13px] text-text truncate">• {i.text}</li>)}
          </ul>
        )}
      </Section>
      <Section title={t('pages.membersPage.goals_reports')} testId="crew-goals-summary-reports">
        <Reports goals={goals} limit={3} />
      </Section>
    </div>
  )
}

/** One short-term goal: its checkbox, who changed it last, and Remove. */
function ShortGoalRow({ item, name, slug, member, run }: {
  item: ShortTermGoal
  name: string
  slug: string
  member: string
  run: (fn: () => Promise<MemberGoals>) => void
}) {
  const { t } = useTranslation()
  return (
    <li className="flex items-start gap-2" data-testid="crew-goals-short-item">
      <Checkbox
        checked={item.status === 'done'}
        onChange={e => run(() => api.memberGoals.updateShort(slug, member, item.id, { status: e.target.checked ? 'done' : 'open' }))}
        aria-label={item.text}
        className="mt-0.5"
      />
      <span className="flex-1 min-w-0">
        <span className={cn('block text-[13px] text-text', item.status !== 'open' && 'line-through text-muted')}>{item.text}</span>
        <span className="block text-[12px] text-muted">
          {t(item.updated_by === 'crewmate' ? 'pages.membersPage.goals_by_crewmate' : 'pages.membersPage.goals_by_you', { name, when: fmtRelative(item.updated_at * 1000) })}
        </span>
      </span>
      <IconButton variant="danger" onClick={() => run(() => api.memberGoals.removeShort(slug, member, item.id))} className="w-7 h-7 grid place-items-center shrink-0" aria-label={t('pages.membersPage.goals_short_remove')} title={t('pages.membersPage.goals_short_remove')}>
        <Trash2 size={13} aria-hidden="true" />
      </IconButton>
    </li>
  )
}

const WEEKDAYS = [1, 2, 3, 4, 5, 6, 0] as const
const MONTH_DAYS = Array.from({ length: 28 }, (_, i) => i + 1)

/** The crewmate's Goals tab: the long-term goal, its key results, the short-term
 *  goals toward it, and the three check-in switches. */
export default function CrewGoalsPane({ slug, member, name }: { slug: string; member: string; name: string }) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const q = useMemberGoals(slug, member)
  const goals = q.data
  // Each draft remembers what it was started from: its save compares against
  // that (the routes' compare-and-set), never against whatever the list holds by
  // the time Save is pressed, so a change made meanwhile is refused, not overwritten.
  const [editing, setEditing] = useState<{ text: string; expected: string } | null>(null)
  const [newShort, setNewShort] = useState('')
  const [krEdit, setKrEdit] = useState<{
    id: string
    text: string
    target: string
    /** The whole set as it read when editing began, ids included. */
    base: { id: string; text: string; target: string }[]
  } | null>(null)
  const [showFinished, setShowFinished] = useState(false)
  const timeId = useId()
  const finishedId = useId()
  const save = useMutation({
    mutationFn: (fn: () => Promise<MemberGoals>) => fn(),
    onSuccess: data => { qc.setQueryData(memberGoalsKey(slug, member), data) },
  })
  // `onSaved` clears a draft only once the save landed: a failed save keeps
  // what was typed so the user can try again.
  const run = (fn: () => Promise<MemberGoals>, onSaved?: () => void) =>
    save.mutate(fn, { onSuccess: () => onSaved?.() })

  if (q.isError) return <ErrorNotice message={t('pages.membersPage.goals_load_failed')} askAgent testId="crew-goals-error" />
  if (!goals) return <p role="status" className="text-[13px] text-muted">{t('pages.membersPage.goals_loading')}</p>

  const hasGoal = Boolean(goals.long_term)
  // Open goals stay in view; done and dropped ones fold behind one disclosure.
  const openGoals = goals.short_term.filter(i => i.status === 'open')
  const finished = goals.short_term.filter(i => i.status !== 'open')
  const change = goals.last_crewmate_change
  const removedKr = goals.removed_key_result
  const setSchedule = (changes: Partial<GoalSchedules>) => run(() => api.memberGoals.setSchedules(slug, member, changes))

  return (
    <div className="flex flex-col gap-3" data-testid="crew-goals-pane">
      {/* No hand-off: the drafts on this tab (the long-term goal being edited,
          a key result being edited, the new short-term goal) stay in their
          fields after a failed save, so the user retries here. */}
      {save.isError && <ErrorNotice message={t('pages.membersPage.goals_save_failed')} testId="crew-goals-save-error" />}

      <Section
        title={t('pages.membersPage.goals_long_term')}
        testId="crew-goals-long-term"
        trailing={editing === null && (
          <IconButton variant="accent" onClick={() => setEditing({ text: goals.long_term, expected: goals.long_term })} className="w-7 h-7 grid place-items-center shrink-0" aria-label={t('pages.membersPage.goals_edit_long_term')} title={t('pages.membersPage.goals_edit_long_term')} data-testid="crew-goals-edit">
            <Pencil size={14} aria-hidden="true" />
          </IconButton>
        )}
      >
        {editing !== null ? (
          <div className="flex flex-col gap-2">
            <textarea
              value={editing.text}
              onChange={e => setEditing({ ...editing, text: e.target.value })}
              maxLength={goals.limits.long_term_max_chars}
              rows={4}
              aria-label={t('pages.membersPage.goals_long_term')}
              className="w-full rounded-lg border border-border bg-bg px-2.5 py-2 text-[13px] text-text focus-ring"
              data-testid="crew-goals-long-term-input"
            />
            <div className="flex gap-2 justify-end">
              <Btn type="button" onClick={() => setEditing(null)}>{t('pages.membersPage.create_cancel')}</Btn>
              <Btn type="button" primary onClick={() => { const { text, expected } = editing; run(() => api.memberGoals.setLongTerm(slug, member, text, expected), () => setEditing(null)) }} data-testid="crew-goals-long-term-save">
                {t('pages.membersPage.goals_save')}
              </Btn>
            </div>
          </div>
        ) : hasGoal ? (
          <p className="text-[13px] leading-relaxed text-text whitespace-pre-wrap" data-testid="crew-goals-long-term-text">{goals.long_term}</p>
        ) : (
          <div className="flex flex-col items-center gap-2 py-3 text-center text-[13px] text-muted">
            <Goal size={32} className="opacity-50" aria-hidden="true" />
            {t('pages.membersPage.goals_none', { name })}
          </div>
        )}
      </Section>

      <Section title={t('pages.membersPage.goals_key_results')} testId="crew-goals-key-results">
        {removedKr && (
          <div className="flex items-center gap-2 mb-2 rounded-lg bg-bg-hover px-2.5 py-1.5 text-[12px] text-muted" data-testid="crew-goals-kr-undo-row">
            <span className="flex-1 min-w-0 truncate" title={removedKr.text}>{t('pages.membersPage.goals_kr_removed')}</span>
            <Btn type="button" onClick={() => run(() => api.memberGoals.restoreKeyResult(slug, member, removedKr.id))} className="gap-1 h-7 px-2" data-testid="crew-goals-kr-undo">
              <Undo2 size={12} aria-hidden="true" />{t('pages.membersPage.goals_undo')}
            </Btn>
          </div>
        )}
        {goals.key_results.length === 0 ? (
          <p className="text-[13px] text-muted">{t('pages.membersPage.goals_kr_empty', { name })}</p>
        ) : (
          <ul className="flex flex-col gap-3">
            {goals.key_results.map(kr => (
              <li key={kr.id} className="flex items-start gap-2">
                {krEdit?.id === kr.id ? (
                  <div className="flex-1 flex flex-col gap-1.5">
                    <Input value={krEdit.text} maxLength={goals.limits.key_result_text_max_chars} onChange={e => setKrEdit({ ...krEdit, text: e.target.value })} aria-label={t('pages.membersPage.goals_kr_text')} />
                    <Input value={krEdit.target} maxLength={goals.limits.key_result_value_max_chars} onChange={e => setKrEdit({ ...krEdit, target: e.target.value })} aria-label={t('pages.membersPage.goals_kr_target')} />
                    <div className="flex gap-2 justify-end">
                      <Btn type="button" onClick={() => setKrEdit(null)}>{t('pages.membersPage.create_cancel')}</Btn>
                      <Btn type="button" primary onClick={() => {
                        const { base } = krEdit
                        const items = base.map(k => (k.id === krEdit.id ? { text: krEdit.text, target: krEdit.target } : { text: k.text, target: k.target }))
                        const expected = base.map(k => ({ text: k.text, target: k.target }))
                        run(() => api.memberGoals.setKeyResults(slug, member, items, expected), () => setKrEdit(null))
                      }}>{t('pages.membersPage.goals_save')}</Btn>
                    </div>
                  </div>
                ) : (
                  <>
                    <div className="flex-1 min-w-0"><KeyResultBar kr={kr} /></div>
                    <IconButton variant="accent" onClick={() => setKrEdit({ id: kr.id, text: kr.text, target: kr.target, base: goals.key_results.map(k => ({ id: k.id, text: k.text, target: k.target })) })} className="w-7 h-7 grid place-items-center shrink-0" aria-label={t('pages.membersPage.goals_kr_edit')} title={t('pages.membersPage.goals_kr_edit')}>
                      <Pencil size={13} aria-hidden="true" />
                    </IconButton>
                    <IconButton variant="danger" onClick={() => run(() => api.memberGoals.removeKeyResult(slug, member, kr.id))} className="w-7 h-7 grid place-items-center shrink-0" aria-label={t('pages.membersPage.goals_kr_remove')} title={t('pages.membersPage.goals_kr_remove')} data-testid="crew-goals-kr-remove">
                      <Trash2 size={13} aria-hidden="true" />
                    </IconButton>
                  </>
                )}
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title={t('pages.membersPage.goals_short_term')} testId="crew-goals-short-term">
        {change && (
          <div className="flex items-center gap-2 mb-2 rounded-lg bg-bg-hover px-2.5 py-1.5 text-[12px] text-muted" data-testid="crew-goals-undo-row">
            <span className="flex-1 min-w-0 truncate">
              {t(change.op === 'add' ? 'pages.membersPage.goals_change_added' : 'pages.membersPage.goals_change_updated', { name, text: change.text })}
            </span>
            <Btn type="button" onClick={() => run(() => api.memberGoals.undo(slug, member, change.id))} className="gap-1 h-7 px-2" data-testid="crew-goals-undo">
              <Undo2 size={12} aria-hidden="true" />{t('pages.membersPage.goals_undo')}
            </Btn>
          </div>
        )}
        {goals.short_term.length === 0 && <p className="text-[13px] text-muted">{t('pages.membersPage.goals_short_empty')}</p>}
        <ul className="flex flex-col gap-1.5">
          {openGoals.map(item => <ShortGoalRow key={item.id} item={item} name={name} run={run} slug={slug} member={member} />)}
        </ul>
        {finished.length > 0 && (
          <div className="mt-2">
            <Btn
              type="button"
              onClick={() => setShowFinished(v => !v)}
              aria-expanded={showFinished}
              aria-controls={finishedId}
              className="w-full justify-between border-transparent px-1 text-left text-[12px] text-muted hover:text-text active:scale-100 focus-ring"
              data-testid="crew-goals-finished-toggle"
            >
              <span className="flex-1">
                {showFinished
                  ? t('pages.membersPage.goals_short_hide_done')
                  : t('pages.membersPage.goals_short_show_done', { count: finished.length })}
              </span>
              <ChevronDown size={14} aria-hidden="true" className={cn('shrink-0 transition-transform duration-200 motion-reduce:transition-none', showFinished && 'rotate-180')} />
            </Btn>
            <ul id={finishedId} hidden={!showFinished} className="flex flex-col gap-1.5 mt-1.5" data-testid="crew-goals-finished">
              {showFinished && finished.map(item => <ShortGoalRow key={item.id} item={item} name={name} run={run} slug={slug} member={member} />)}
            </ul>
          </div>
        )}
        <form
          className="flex gap-2 mt-2"
          onSubmit={e => { e.preventDefault(); const text = newShort.trim(); if (!text) return; run(() => api.memberGoals.addShort(slug, member, text), () => setNewShort('')) }}
        >
          <Input value={newShort} maxLength={goals.limits.short_text_max_chars} onChange={e => setNewShort(e.target.value)} placeholder={t('pages.membersPage.goals_short_add_placeholder')} aria-label={t('pages.membersPage.goals_short_add')} className="flex-1" data-testid="crew-goals-short-input" />
          <Btn type="submit" disabled={!newShort.trim()} className="gap-1" data-testid="crew-goals-short-add">
            <Plus size={14} aria-hidden="true" />{t('pages.membersPage.goals_short_add')}
          </Btn>
        </form>
      </Section>

      <Section title={t('pages.membersPage.goals_checkins')} testId="crew-goals-checkins">
        {!hasGoal && <p className="text-[12px] text-muted mb-1.5">{t('pages.membersPage.goals_checkins_need_goal')}</p>}
        <div className="flex flex-col gap-2.5">
          <div className="flex items-center gap-2"><Toggle checked={goals.schedules.daily} disabled={!hasGoal} onChange={v => setSchedule({ daily: v })} label={t('pages.membersPage.goals_daily')} /><span className="text-[13px] text-text">{t('pages.membersPage.goals_daily')}</span></div>
          <div className="flex items-center gap-2 flex-wrap">
            <Toggle checked={goals.schedules.weekly} disabled={!hasGoal} onChange={v => setSchedule({ weekly: v })} label={t('pages.membersPage.goals_weekly')} />
            <span className="text-[13px] text-text">{t('pages.membersPage.goals_weekly')}</span>
            <SimpleSelect
              options={WEEKDAYS.map(String)}
              optionLabels={WEEKDAYS.map(d => fmtWeekday(d === 0 ? 7 : d, 'long'))}
              value={String(goals.schedules.weekday)}
              onChange={v => setSchedule({ weekday: Number(v) })}
              disabled={!hasGoal || !goals.schedules.weekly}
              aria-label={t('pages.membersPage.goals_weekday')}
              className="h-8"
            />
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            <Toggle checked={goals.schedules.monthly} disabled={!hasGoal} onChange={v => setSchedule({ monthly: v })} label={t('pages.membersPage.goals_monthly')} />
            <span className="text-[13px] text-text">{t('pages.membersPage.goals_monthly')}</span>
            <SimpleSelect
              options={MONTH_DAYS.map(String)}
              optionLabels={MONTH_DAYS.map(day => t('pages.membersPage.goals_month_day_option', { day }))}
              value={String(goals.schedules.month_day)}
              onChange={v => setSchedule({ month_day: Number(v) })}
              disabled={!hasGoal || !goals.schedules.monthly}
              aria-label={t('pages.membersPage.goals_month_day')}
              className="h-8"
            />
          </div>
          <label htmlFor={timeId} className="flex items-center gap-2 text-[13px] text-text">
            {t('pages.membersPage.goals_time')}
            <input
              id={timeId}
              type="time"
              aria-label={t('pages.membersPage.goals_time')}
              value={goals.schedules.time}
              onChange={e => { if (e.target.value) setSchedule({ time: e.target.value }) }}
              className="h-8 rounded-lg border border-border bg-bg px-2 text-[13px] text-text focus-ring"
              data-testid="crew-goals-time"
            />
          </label>
        </div>
      </Section>

      <Section title={t('pages.membersPage.goals_reports')} testId="crew-goals-reports">
        <Reports goals={goals} limit={6} />
      </Section>
    </div>
  )
}
