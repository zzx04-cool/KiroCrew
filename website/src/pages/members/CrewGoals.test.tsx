import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import type { MemberGoals } from '../../api/client'

const memberGoals = vi.hoisted(() => ({
  get: vi.fn(),
  setLongTerm: vi.fn(),
  setKeyResults: vi.fn(),
  addShort: vi.fn(),
  updateShort: vi.fn(),
  removeShort: vi.fn(),
  removeKeyResult: vi.fn(),
  restoreKeyResult: vi.fn(),
  undo: vi.fn(),
  setSchedules: vi.fn(),
}))

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return { ...actual, api: { memberGoals } }
})

import CrewGoalsPane, { CrewGoalsSummary, keyResultProgress } from './CrewGoals'

const NOW = 1_800_000_000

const record = (over: Partial<MemberGoals> = {}): MemberGoals => ({
  slug: 'scout',
  member: 'scout',
  long_term: 'Keep the release notes current',
  long_term_updated_at: NOW,
  long_term_updated_by: 'user',
  key_results: [
    { id: 'k00000001', text: 'PRs with a description', target: '27', current: '19', updated_at: NOW - 3600, readings: [] },
    { id: 'k00000002', text: 'Notes published', target: 'yes', current: '', updated_at: 0, readings: [] },
  ],
  short_term: [
    { id: 'g00000001', text: 'Draft the notes', status: 'open', updated_at: NOW - 60, updated_by: 'crewmate' },
    { id: 'g00000002', text: 'Collect PRs', status: 'done', updated_at: NOW - 600, updated_by: 'user' },
  ],
  schedules: { time: '09:00', timezone: '', daily: false, weekly: false, weekday: 1, monthly: false, month_day: 1 },
  reports: [{ kind: 'weekly', slug: 'week-41', title: 'Week 41', at: NOW - 86400 }],
  last_crewmate_change: { id: 'c1', op: 'add', goal_id: 'g00000001', text: 'Draft the notes', at: NOW - 60 },
  removed_key_result: null,
  limits: {
    long_term_max_chars: 2000,
    short_text_max_chars: 300,
    max_short_term: 20,
    max_key_results: 3,
    key_result_text_max_chars: 200,
    key_result_value_max_chars: 40,
  },
  ...over,
})

beforeEach(() => {
  for (const fn of Object.values(memberGoals)) fn.mockReset()
})

describe('keyResultProgress', () => {
  it('reads numbers as a fraction of the target and a word target as reached or not', () => {
    expect(keyResultProgress({ current: '19', target: '27' })).toBeCloseTo(19 / 27)
    expect(keyResultProgress({ current: '40', target: '27' })).toBe(1)
    expect(keyResultProgress({ current: 'yes', target: 'Yes' })).toBe(1)
    expect(keyResultProgress({ current: 'no', target: 'yes' })).toBe(0)
    expect(keyResultProgress({ current: '', target: '27' })).toBeNull()
  })
})

describe('CrewGoalsPane', () => {
  it('shows the goal, a progress bar per key result and the short-term goals', async () => {
    memberGoals.get.mockResolvedValue(record())
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    expect(await screen.findByTestId('crew-goals-long-term-text')).toHaveTextContent('Keep the release notes current')
    const bars = screen.getAllByRole('progressbar')
    expect(bars).toHaveLength(2)
    expect(bars[0]).toHaveAttribute('aria-valuenow', String(Math.round((19 / 27) * 100)))
    expect(bars[1]).not.toHaveAttribute('aria-valuenow')
    // Only the open one shows; the done one waits behind the collapsed disclosure.
    expect(screen.getAllByTestId('crew-goals-short-item')).toHaveLength(1)
    expect(within(screen.getByTestId('crew-goals-reports')).getByRole('link', { name: 'Week 41' })).toHaveAttribute('href', '/artifacts/week-41')
  })

  it('undoes the crewmate\'s newest change', async () => {
    memberGoals.get.mockResolvedValue(record())
    memberGoals.undo.mockResolvedValue(record({ last_crewmate_change: null }))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    fireEvent.click(await screen.findByTestId('crew-goals-undo'))
    await waitFor(() => expect(memberGoals.undo).toHaveBeenCalledWith('scout', 'scout', 'c1'))
  })

  it('removes a key result and offers an Undo that restores it', async () => {
    const full = record()
    const removed = record({
      key_results: full.key_results.slice(1),
      removed_key_result: { id: 'k00000001', text: 'PRs with a description', at: NOW },
    })
    memberGoals.get.mockResolvedValue(full)
    memberGoals.removeKeyResult.mockResolvedValue(removed)
    memberGoals.restoreKeyResult.mockResolvedValue(full)
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    const section = await screen.findByTestId('crew-goals-key-results')
    expect(within(section).queryByTestId('crew-goals-kr-undo')).toBeNull()
    fireEvent.click(within(section).getAllByTestId('crew-goals-kr-remove')[0])
    await waitFor(() => expect(memberGoals.removeKeyResult).toHaveBeenCalledWith('scout', 'scout', 'k00000001'))
    expect(memberGoals.setKeyResults).not.toHaveBeenCalled()
    expect(await within(section).findByText('Key result removed')).toBeInTheDocument()
    fireEvent.click(within(section).getByRole('button', { name: 'Undo' }))
    await waitFor(() => expect(memberGoals.restoreKeyResult).toHaveBeenCalledWith('scout', 'scout', 'k00000001'))
    await waitFor(() => expect(within(section).queryByTestId('crew-goals-kr-undo')).toBeNull())
    expect(within(section).getAllByTestId('goal-key-result')).toHaveLength(2)
  })

  it('says when each key result was updated, or that it has no reading yet', async () => {
    memberGoals.get.mockResolvedValue(record())
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    const section = await screen.findByTestId('crew-goals-key-results')
    expect(within(section).getByText(/^Updated /)).toBeInTheDocument()
    expect(within(section).getByText('No reading yet')).toBeInTheDocument()
  })

  it('folds done and dropped short-term goals behind a collapsed disclosure', async () => {
    const short_term: MemberGoals['short_term'] = [
      { id: 'g00000001', text: 'Draft the notes', status: 'open', updated_at: NOW - 60, updated_by: 'crewmate' },
      { id: 'g00000002', text: 'Collect PRs', status: 'done', updated_at: NOW - 600, updated_by: 'user' },
      { id: 'g00000003', text: 'Old idea', status: 'dropped', updated_at: NOW - 900, updated_by: 'crewmate' },
    ]
    memberGoals.get.mockResolvedValue(record({ short_term }))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    const section = await screen.findByTestId('crew-goals-short-term')
    expect(within(section).getByText('Draft the notes')).toBeInTheDocument()
    expect(within(section).queryByText('Collect PRs')).toBeNull()
    expect(within(section).queryByText('Old idea')).toBeNull()
    const toggle = within(section).getByRole('button', { name: 'Show 2 done' })
    expect(toggle.tagName).toBe('BUTTON')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByTestId('crew-goals-finished')).not.toBeVisible()
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(toggle).toHaveAttribute('aria-controls', screen.getByTestId('crew-goals-finished').id)
    expect(within(section).getByText('Collect PRs')).toBeVisible()
    expect(within(section).getByText('Old idea')).toBeVisible()
    expect(within(section).getAllByTestId('crew-goals-short-item')).toHaveLength(3)
    fireEvent.click(within(section).getByRole('button', { name: 'Hide done' }))
    expect(within(section).queryByText('Collect PRs')).toBeNull()
  })

  it('shows no disclosure when every short-term goal is open', async () => {
    memberGoals.get.mockResolvedValue(record({
      short_term: [{ id: 'g00000001', text: 'Draft the notes', status: 'open', updated_at: NOW - 60, updated_by: 'crewmate' }],
    }))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    await screen.findByTestId('crew-goals-short-term')
    expect(screen.queryByTestId('crew-goals-finished-toggle')).toBeNull()
  })

  it('keeps every check-in switch disabled while there is no goal', async () => {
    memberGoals.get.mockResolvedValue(record({ long_term: '', key_results: [] }))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    const section = await screen.findByTestId('crew-goals-checkins')
    const switches = within(section).getAllByRole('switch')
    expect(switches).toHaveLength(3)
    for (const sw of switches) expect(sw).toHaveAttribute('aria-disabled', 'true')
  })

  it('disables the weekday and day-of-month pickers while their switch is off', async () => {
    const off = record()
    memberGoals.get.mockResolvedValue(off)
    const { unmount } = renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    expect(await screen.findByRole('combobox', { name: 'Day of the week' })).toBeDisabled()
    expect(screen.getByRole('combobox', { name: 'Day of the month' })).toBeDisabled()
    unmount()
    memberGoals.get.mockResolvedValue(record({ schedules: { ...off.schedules, weekly: true } }))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    expect(await screen.findByRole('combobox', { name: 'Day of the week' })).toBeEnabled()
    expect(screen.getByRole('combobox', { name: 'Day of the month' })).toBeDisabled()
  })

  it('disables both pickers when there is no goal, whatever the switches say', async () => {
    const base = record()
    memberGoals.get.mockResolvedValue(record({ long_term: '', schedules: { ...base.schedules, weekly: true, monthly: true } }))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    expect(await screen.findByRole('combobox', { name: 'Day of the week' })).toBeDisabled()
    expect(screen.getByRole('combobox', { name: 'Day of the month' })).toBeDisabled()
  })

  it('keeps every draft when its save fails and clears it once a save lands', async () => {
    memberGoals.get.mockResolvedValue(record())
    memberGoals.setLongTerm.mockRejectedValueOnce(new Error('boom'))
    memberGoals.addShort.mockRejectedValueOnce(new Error('boom'))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    fireEvent.click(await screen.findByTestId('crew-goals-edit'))
    fireEvent.change(screen.getByTestId('crew-goals-long-term-input'), { target: { value: 'A new goal' } })
    fireEvent.click(screen.getByTestId('crew-goals-long-term-save'))
    expect(await screen.findByTestId('crew-goals-save-error')).toBeInTheDocument()
    expect(screen.getByTestId('crew-goals-long-term-input')).toHaveValue('A new goal')
    const input = screen.getByTestId('crew-goals-short-input')
    fireEvent.change(input, { target: { value: 'Ship it' } })
    fireEvent.click(screen.getByTestId('crew-goals-short-add'))
    await waitFor(() => expect(memberGoals.addShort).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByTestId('crew-goals-save-error')).toBeInTheDocument())
    expect(input).toHaveValue('Ship it')
    memberGoals.addShort.mockResolvedValueOnce(record())
    fireEvent.click(screen.getByTestId('crew-goals-short-add'))
    await waitFor(() => expect(input).toHaveValue(''))
  })

  it('saves a key result edit against the set it started from, keyed by id', async () => {
    const a = { id: 'k0000000a', text: 'A', target: '1', current: '', updated_at: 0, readings: [] }
    const b = { id: 'k0000000b', text: 'B', target: '2', current: '', updated_at: 0, readings: [] }
    const c = { id: 'k0000000c', text: 'C', target: '3', current: '', updated_at: 0, readings: [] }
    memberGoals.get.mockResolvedValue(record({ key_results: [a, b, c] }))
    memberGoals.removeKeyResult.mockResolvedValue(record({ key_results: [b, c] }))
    memberGoals.setKeyResults.mockRejectedValue(new Error('changed_meanwhile'))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    const section = await screen.findByTestId('crew-goals-key-results')
    fireEvent.click(within(section).getAllByRole('button', { name: 'Edit key result' })[1])
    fireEvent.change(within(section).getByDisplayValue('B'), { target: { value: 'B2' } })
    // A goes away underneath the open editor.
    fireEvent.click(within(section).getAllByTestId('crew-goals-kr-remove')[0])
    await waitFor(() => expect(within(section).getAllByTestId('goal-key-result')).toHaveLength(1))
    // The editor stayed on B; C is still shown as it was.
    expect(within(section).getByDisplayValue('B2')).toBeInTheDocument()
    expect(within(section).getByText('C')).toBeInTheDocument()
    fireEvent.click(within(section).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(memberGoals.setKeyResults).toHaveBeenCalledTimes(1))
    const [, , items, expected] = memberGoals.setKeyResults.mock.calls[0]
    expect(items).toEqual([{ text: 'A', target: '1' }, { text: 'B2', target: '2' }, { text: 'C', target: '3' }])
    expect(expected).toEqual([{ text: 'A', target: '1' }, { text: 'B', target: '2' }, { text: 'C', target: '3' }])
  })

  it('saves the long-term goal against the text it started editing from', async () => {
    memberGoals.get.mockResolvedValue(record())
    memberGoals.addShort.mockResolvedValue(record({ long_term: 'Changed meanwhile' }))
    memberGoals.setLongTerm.mockResolvedValue(record({ long_term: 'Mine' }))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    fireEvent.click(await screen.findByTestId('crew-goals-edit'))
    fireEvent.change(screen.getByTestId('crew-goals-long-term-input'), { target: { value: 'Mine' } })
    fireEvent.change(screen.getByTestId('crew-goals-short-input'), { target: { value: 'Ship it' } })
    fireEvent.click(screen.getByTestId('crew-goals-short-add'))
    await waitFor(() => expect(memberGoals.addShort).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByTestId('crew-goals-short-input')).toHaveValue(''))
    fireEvent.click(screen.getByTestId('crew-goals-long-term-save'))
    await waitFor(() => expect(memberGoals.setLongTerm).toHaveBeenCalledWith('scout', 'scout', 'Mine', 'Keep the release notes current'))
  })

  it('turns on one check-in at a time', async () => {
    memberGoals.get.mockResolvedValue(record())
    memberGoals.setSchedules.mockResolvedValue(record())
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    const section = await screen.findByTestId('crew-goals-checkins')
    fireEvent.click(within(section).getByRole('switch', { name: 'Weekly report' }))
    await waitFor(() => expect(memberGoals.setSchedules).toHaveBeenCalledWith('scout', 'scout', { weekly: true }))
  })
})

describe('CrewGoalsSummary (the Dashboard tab)', () => {
  it('shows the live goal picture and the latest reports', async () => {
    memberGoals.get.mockResolvedValue(record())
    renderWithProviders(<CrewGoalsSummary slug="scout" member="scout" />)
    const summary = await screen.findByTestId('crew-goals-summary')
    expect(within(summary).getAllByRole('progressbar')).toHaveLength(2)
    expect(within(summary).getByText(/Draft the notes/)).toBeInTheDocument()
    expect(within(summary).queryByText(/Collect PRs/)).toBeNull()
    expect(within(summary).getByRole('link', { name: 'Week 41' })).toBeInTheDocument()
  })

  it('says the goals could not be loaded rather than showing nothing', async () => {
    memberGoals.get.mockRejectedValue(new Error('boom'))
    renderWithProviders(<CrewGoalsSummary slug="scout" member="scout" />)
    const notice = await screen.findByTestId('crew-goals-summary-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent("Couldn't load the goals.")
    expect(screen.queryByTestId('crew-goals-summary')).toBeNull()
  })

  it('renders nothing for a crewmate with no goal', async () => {
    memberGoals.get.mockResolvedValue(record({ long_term: '' }))
    renderWithProviders(<CrewGoalsSummary slug="scout" member="scout" />)
    await waitFor(() => expect(memberGoals.get).toHaveBeenCalled())
    expect(screen.queryByTestId('crew-goals-summary')).toBeNull()
  })
})
