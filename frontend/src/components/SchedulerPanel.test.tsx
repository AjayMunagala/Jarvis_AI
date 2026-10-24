import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import SchedulerPanel from './SchedulerPanel'
import { createJob, deleteJob, getActivity, listJobs, runJob, updateJob, type ActivityPage, type ScheduledJob } from '../services/api'

vi.mock('../services/api', () => ({
  createJob: vi.fn(), deleteJob: vi.fn(), getActivity: vi.fn(), listJobs: vi.fn(), runJob: vi.fn(), updateJob: vi.fn(),
}))

const auth = { token: 'fake-test-token' }
const job = (overrides: Partial<ScheduledJob> = {}): ScheduledJob => ({
  id: 'job-1', owner_id: 'o', name: 'Morning briefing', schedule_type: 'cron', schedule_expr: '30 1 * * *', action_type: 'daily_briefing',
  payload: {}, enabled: true, next_run_at: '2026-10-25T01:30:00Z', last_run_at: null, last_status: 'idle',
  created_at: '2026-10-24T07:00:00Z', updated_at: '2026-10-24T07:00:00Z', ...overrides,
})
const activity: ActivityPage = {
  durable_storage: false, storage_note: 'In memory.',
  events: [
    { event_id: 'AE-1', occurred_at: '2026-10-24T01:30:00Z', owner_id: 'o', category: 'scheduler', event_type: 'scheduler.job.execute', decision: 'allowed',
      status: 'succeeded', subject: 'scheduler:job:job-1', correlation_id: null, details: { status: 'success', trigger: 'manual' }, durable: false },
    { event_id: 'AE-2', occurred_at: '2026-10-24T01:00:00Z', owner_id: 'o', category: 'scheduler', event_type: 'scheduler.job.create', decision: 'allowed',
      status: 'succeeded', subject: 'scheduler:job:job-1', correlation_id: null, details: {}, durable: false },
  ],
}

function load(items: ScheduledJob[] = [job()]) {
  vi.mocked(listJobs).mockResolvedValue({ items, total: items.length })
  vi.mocked(getActivity).mockResolvedValue(activity)
}

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('SchedulerPanel', () => {
  it('requires unlocking and makes no request without a token', async () => {
    const onUnlock = vi.fn()
    render(<SchedulerPanel auth={{}} onUnlock={onUnlock} />)
    await userEvent.click(screen.getByRole('button', { name: 'Unlock scheduler' }))
    expect(onUnlock).toHaveBeenCalled()
    expect(listJobs).not.toHaveBeenCalled()
    expect(getActivity).not.toHaveBeenCalled()
  })

  it('lists jobs with schedule, action, state, next run and the recent run history, and explains UTC cron', async () => {
    load()
    render(<SchedulerPanel auth={auth} onUnlock={() => undefined} />)
    const jobs = await screen.findByLabelText('Scheduled jobs')
    expect(listJobs).toHaveBeenCalledWith(50, 0, auth)
    expect(getActivity).toHaveBeenCalledWith(200, auth, 'scheduler')
    expect(within(jobs).getByText('Morning briefing')).toBeInTheDocument()
    expect(within(jobs).getByText('daily_briefing')).toBeInTheDocument()
    expect(within(jobs).getByText('enabled')).toBeInTheDocument()
    expect(within(jobs).getByText('last: idle')).toBeInTheDocument()
    expect(within(jobs).getByText(/2026-10-25 01:30 UTC/)).toBeInTheDocument()
    const history = screen.getByLabelText('Recent job runs')
    expect(within(history).getAllByRole('listitem')).toHaveLength(1)
    expect(within(history).getByText(/success · manual/)).toBeInTheDocument()
    expect(screen.getByText(/Cron expressions are evaluated in UTC/)).toHaveTextContent('UTC+05:30')
  })

  it('shows empty and error states, and still lists jobs without the audit trail', async () => {
    vi.mocked(listJobs).mockResolvedValue({ items: [], total: 0 })
    vi.mocked(getActivity).mockRejectedValue(new Error('no audit'))
    render(<SchedulerPanel auth={auth} onUnlock={() => undefined} />)
    expect(await screen.findByText('No scheduled jobs yet.')).toBeInTheDocument()
    expect(screen.getByText(/Recent job runs are unavailable/)).toBeInTheDocument()
    cleanup()
    vi.mocked(listJobs).mockRejectedValue(new Error('The scheduler service is unavailable.'))
    render(<SchedulerPanel auth={auth} onUnlock={() => undefined} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('The scheduler service is unavailable.')
  })

  it('creates a daily briefing job with a cron expression', async () => {
    load([])
    vi.mocked(createJob).mockResolvedValue(job())
    render(<SchedulerPanel auth={auth} onUnlock={() => undefined} />)
    await screen.findByText('No scheduled jobs yet.')
    await userEvent.type(screen.getByLabelText('Name'), 'Morning briefing')
    await userEvent.click(screen.getByRole('button', { name: 'Create job' }))
    expect(createJob).toHaveBeenCalledWith({ name: 'Morning briefing', schedule_type: 'cron', schedule_expr: '30 1 * * *', action_type: 'daily_briefing', payload: {}, enabled: true }, auth)
    expect(await screen.findByText(/Job "Morning briefing" created. Next run/)).toBeInTheDocument()
  })

  it('creates a reminder job on an interval with its payload', async () => {
    load([])
    vi.mocked(createJob).mockResolvedValue(job({ name: 'Stretch', next_run_at: null }))
    render(<SchedulerPanel auth={auth} onUnlock={() => undefined} />)
    await screen.findByText('No scheduled jobs yet.')
    await userEvent.type(screen.getByLabelText('Name'), 'Stretch')
    await userEvent.selectOptions(screen.getByLabelText('Action'), 'reminder')
    await userEvent.selectOptions(screen.getByLabelText('Schedule type'), 'interval')
    expect(screen.getByText(/Seconds between runs/)).toBeInTheDocument()
    await userEvent.clear(screen.getByLabelText('Schedule expression'))
    await userEvent.type(screen.getByLabelText('Schedule expression'), '3600')
    await userEvent.type(screen.getByLabelText('Reminder title (optional)'), 'Stand up')
    await userEvent.click(screen.getByLabelText('Enabled'))
    await userEvent.click(screen.getByRole('button', { name: 'Create job' }))
    expect(createJob).toHaveBeenCalledWith({ name: 'Stretch', schedule_type: 'interval', schedule_expr: '3600', action_type: 'reminder', payload: { title: 'Stand up' }, enabled: false }, auth)
  })

  it('shows schedule validation errors from the server', async () => {
    load([])
    vi.mocked(createJob).mockRejectedValue(new Error('A cron expression needs five fields: minute hour day-of-month month day-of-week.'))
    render(<SchedulerPanel auth={auth} onUnlock={() => undefined} />)
    await screen.findByText('No scheduled jobs yet.')
    await userEvent.type(screen.getByLabelText('Name'), 'Bad')
    await userEvent.click(screen.getByRole('button', { name: 'Create job' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('needs five fields')
  })

  it('disables and enables a job', async () => {
    load([job(), job({ id: 'job-2', name: 'Paused', enabled: false })])
    vi.mocked(updateJob).mockImplementation(async (id, body) => job({ id, enabled: Boolean(body.enabled) }))
    render(<SchedulerPanel auth={auth} onUnlock={() => undefined} />)
    await userEvent.click(await screen.findByRole('button', { name: 'Disable Morning briefing' }))
    expect(updateJob).toHaveBeenCalledWith('job-1', { enabled: false }, auth)
    await userEvent.click(await screen.findByRole('button', { name: 'Enable Paused' }))
    expect(updateJob).toHaveBeenCalledWith('job-2', { enabled: true }, auth)
  })

  it('runs a job now and reports a failed run', async () => {
    load()
    vi.mocked(runJob).mockResolvedValueOnce({ job_id: 'job-1', status: 'success', started_at: '2026-10-24T07:00:00Z', completed_at: '2026-10-24T07:00:01Z', output: { action: 'daily_briefing', notified: true }, error: null })
      .mockResolvedValueOnce({ job_id: 'job-1', status: 'failure', started_at: '2026-10-24T07:00:00Z', completed_at: '2026-10-24T07:00:01Z', output: {}, error: 'The assistant is not connected; no briefing was created.' })
    render(<SchedulerPanel auth={auth} onUnlock={() => undefined} />)
    await userEvent.click(await screen.findByRole('button', { name: 'Run Morning briefing now' }))
    expect(runJob).toHaveBeenCalledWith('job-1', auth)
    expect(await screen.findByText('Job "Morning briefing" ran successfully.')).toBeInTheDocument()
    expect(screen.getByText('{"action":"daily_briefing","notified":true}')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Run Morning briefing now' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('no briefing was created')
  })

  it('deletes a job only after confirmation', async () => {
    load()
    vi.mocked(deleteJob).mockResolvedValue(undefined)
    render(<SchedulerPanel auth={auth} onUnlock={() => undefined} />)
    await userEvent.click(await screen.findByRole('button', { name: 'Delete' }))
    expect(deleteJob).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(deleteJob).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await userEvent.click(screen.getByRole('button', { name: 'Delete job' }))
    expect(deleteJob).toHaveBeenCalledWith('job-1', auth)
    expect(await screen.findByText('Job "Morning briefing" deleted.')).toBeInTheDocument()
  })

  it('ignores a response that arrives after the workspace was locked', async () => {
    let resolve: (value: { items: ScheduledJob[]; total: number }) => void = () => undefined
    vi.mocked(listJobs).mockReturnValue(new Promise((done) => { resolve = done }))
    vi.mocked(getActivity).mockResolvedValue(activity)
    const { rerender } = render(<SchedulerPanel auth={auth} onUnlock={() => undefined} />)
    rerender(<SchedulerPanel auth={{}} onUnlock={() => undefined} />)
    await act(async () => { resolve({ items: [job()], total: 1 }) })
    await waitFor(() => expect(screen.getByText('Unlock the workspace to view and manage scheduled jobs.')).toBeInTheDocument())
    expect(screen.queryByText('Morning briefing')).toBeNull()
  })
})
