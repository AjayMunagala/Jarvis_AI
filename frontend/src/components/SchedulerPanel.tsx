import { useEffect, useRef, useState, type FormEvent } from 'react'
import {
  createJob,
  deleteJob,
  getActivity,
  listJobs,
  runJob,
  updateJob,
  type ApiAuth,
  type AuditRecord,
  type JobCreate,
  type JobRunResult,
  type ScheduleType,
  type ScheduledJob,
} from '../services/api'
import { ConfirmButton, formatTime, formatUtc, shortJson } from './ApprovalGate'
import './ActivityPanel.css'
import './OperationsPanels.css'

const ACTIONS: { value: string; label: string }[] = [
  { value: 'daily_briefing', label: 'Daily briefing (notification)' },
  { value: 'reminder', label: 'Reminder (notification)' },
  { value: 'health_check', label: 'Health check' },
  { value: 'workflow', label: 'Run a workflow' },
]

const EXPRESSION_HELP: Record<ScheduleType, string> = {
  cron: 'Five fields: minute hour day-of-month month day-of-week, evaluated in UTC.',
  interval: 'Seconds between runs, from 60 seconds to 366 days.',
  once: 'Seconds from now, or an ISO 8601 date-time (UTC unless it includes an offset).',
}

function message(cause: unknown, fallback: string) {
  return cause instanceof Error ? cause.message : fallback
}

export default function SchedulerPanel({ auth, onUnlock }: { auth: ApiAuth; onUnlock: () => void }) {
  const [jobs, setJobs] = useState<ScheduledJob[] | null>(null)
  const [history, setHistory] = useState<AuditRecord[] | null>(null)
  const [lastRun, setLastRun] = useState<{ name: string; result: JobRunResult } | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [actionType, setActionType] = useState('daily_briefing')
  const [scheduleType, setScheduleType] = useState<ScheduleType>('cron')
  const [expression, setExpression] = useState('30 1 * * *')
  const [reminderTitle, setReminderTitle] = useState('')
  const [reminderMessage, setReminderMessage] = useState('')
  const [workflowId, setWorkflowId] = useState('')
  const [enabled, setEnabled] = useState(true)
  const generation = useRef(0)

  async function refresh(version = generation.current) {
    try {
      // The run history comes from the audit trail; if it is unavailable the jobs are still shown.
      const [page, activity] = await Promise.all([listJobs(50, 0, auth), getActivity(50, auth, 'scheduler').catch(() => null)])
      if (version !== generation.current) return
      setJobs(page.items)
      setHistory(activity ? activity.events.filter((event) => event.event_type === 'scheduler.job.execute') : null)
    } catch (cause) {
      if (version === generation.current) setError(message(cause, 'The scheduler is unavailable.'))
    }
  }

  useEffect(() => {
    generation.current += 1
    setJobs(null); setHistory(null); setLastRun(null); setError(''); setNotice(''); setBusy(null)
    if (auth.token) void refresh(generation.current)
  }, [auth.token]) // eslint-disable-line react-hooks/exhaustive-deps

  async function act(key: string, work: (version: number) => Promise<void>, fallback: string) {
    if (busy) return
    const version = generation.current
    setBusy(key); setError(''); setNotice('')
    try {
      await work(version)
    } catch (cause) {
      if (version === generation.current) setError(message(cause, fallback))
    } finally {
      if (version === generation.current) setBusy(null)
    }
  }

  function create(event: FormEvent) {
    event.preventDefault()
    const payload: Record<string, unknown> = {}
    if (actionType === 'reminder') {
      if (reminderTitle.trim()) payload.title = reminderTitle.trim()
      if (reminderMessage.trim()) payload.message = reminderMessage.trim()
    }
    if (actionType === 'workflow') payload.workflow_id = workflowId.trim()
    const body: JobCreate = { name: name.trim(), schedule_type: scheduleType, schedule_expr: expression.trim(), action_type: actionType, payload, enabled }
    void act('create', async (version) => {
      const created = await createJob(body, auth)
      if (version !== generation.current) return
      setName(''); setReminderTitle(''); setReminderMessage(''); setWorkflowId('')
      setNotice(`Job "${created.name}" created.${created.next_run_at ? ` Next run ${formatTime(created.next_run_at)} (${formatUtc(created.next_run_at)}).` : ''}`)
      await refresh(version)
    }, 'The job could not be created.')
  }

  function toggle(job: ScheduledJob) {
    void act(`toggle:${job.id}`, async (version) => {
      const updated = await updateJob(job.id, { enabled: !job.enabled }, auth)
      if (version !== generation.current) return
      setNotice(`Job "${updated.name}" ${updated.enabled ? 'enabled' : 'disabled'}.`)
      await refresh(version)
    }, 'The job could not be updated.')
  }

  function remove(job: ScheduledJob) {
    void act(`delete:${job.id}`, async (version) => {
      await deleteJob(job.id, auth)
      if (version !== generation.current) return
      setNotice(`Job "${job.name}" deleted.`)
      await refresh(version)
    }, 'The job could not be deleted.')
  }

  function runNow(job: ScheduledJob) {
    void act(`run:${job.id}`, async (version) => {
      const result = await runJob(job.id, auth)
      if (version !== generation.current) return
      setLastRun({ name: job.name, result })
      if (result.status === 'failure') setError(`Job "${job.name}" failed: ${result.error ?? 'no reason given.'}`)
      else setNotice(`Job "${job.name}" ran successfully.`)
      await refresh(version)
    }, 'The job could not be run.')
  }

  const jobNames = new Map((jobs ?? []).map((job) => [`scheduler:job:${job.id}`, job.name]))

  return <section className="activity-panel" aria-labelledby="scheduler-heading">
    <p className="activity-kicker">AUTOMATION · SCHEDULED JOBS</p>
    <h1 id="scheduler-heading">Scheduler</h1>
    <p>Scheduled jobs run while the JARVIS backend is running: daily briefings, reminders, health checks and workflows. A workflow job whose workflow writes data cannot run unattended; it pauses and asks for your approval instead.</p>
    <p className="activity-meta">Cron expressions are evaluated in UTC. Example: for 07:00 India time (UTC+05:30) use <code>30 1 * * *</code>, which is 01:30 UTC.</p>
    {!auth.token && <div role="status"><p>Unlock the workspace to view and manage scheduled jobs.</p><button type="button" onClick={onUnlock}>Unlock scheduler</button></div>}
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status" className="activity-notice">{notice}</p>}
    {auth.token && !jobs && !error && <p role="status">Loading scheduled jobs…</p>}

    {jobs && <section aria-label="Scheduled jobs">
      <h2>Jobs</h2>
      {jobs.length ? <ul className="ops-list">{jobs.map((job) => <li key={job.id}>
        <div><strong>{job.name}</strong> <span className={`ops-status ops-status-${job.enabled ? 'enabled' : 'disabled'}`}>{job.enabled ? 'enabled' : 'disabled'}</span> <span className={`ops-status ops-status-${job.last_status}`}>last: {job.last_status}</span></div>
        <p><code>{job.schedule_type}</code> <code>{job.schedule_expr}</code> · action <code>{job.action_type}</code></p>
        <small>Next run: {job.next_run_at ? `${formatTime(job.next_run_at)} (${formatUtc(job.next_run_at)})` : 'not scheduled'} · Last run: {formatTime(job.last_run_at)}</small>
        <div className="activity-actions">
          <button type="button" onClick={() => runNow(job)} disabled={busy !== null} aria-label={`Run ${job.name} now`}>Run now</button>
          <button type="button" onClick={() => toggle(job)} disabled={busy !== null} aria-label={`${job.enabled ? 'Disable' : 'Enable'} ${job.name}`}>{job.enabled ? 'Disable' : 'Enable'}</button>
          <ConfirmButton label="Delete" question={`Delete job "${job.name}"? It will no longer run.`} confirmLabel="Delete job" onConfirm={() => remove(job)} disabled={busy !== null} />
        </div>
      </li>)}</ul> : <p>No scheduled jobs yet.</p>}
    </section>}

    {lastRun && <section aria-label="Last manual run">
      <h2>Last manual run</h2>
      <p><strong>{lastRun.name}</strong> <span className={`ops-status ops-status-${lastRun.result.status}`}>{lastRun.result.status}</span> {formatTime(lastRun.result.completed_at)}</p>
      {lastRun.result.error && <p>{lastRun.result.error}</p>}
      {Object.keys(lastRun.result.output).length > 0 && <pre className="ops-output">{shortJson(lastRun.result.output)}</pre>}
    </section>}

    {jobs && !history && <p className="activity-meta">Recent job runs are unavailable (the audit trail could not be read).</p>}
    {history && <section aria-label="Recent job runs">
      <h2>Recent job runs</h2>
      <p className="activity-meta">From the audit trail (scheduled and manual runs).</p>
      {history.length ? <ol className="activity-events">{history.map((event) => <li key={event.event_id}>
        <span>{formatTime(event.occurred_at)}</span> <strong>{jobNames.get(event.subject) ?? event.subject}</strong> · {String(event.details.status ?? event.status)} · {String(event.details.trigger ?? 'scheduled')}{event.details.paused ? ' · paused for approval' : ''}
      </li>)}</ol> : <p>No job runs recorded yet.</p>}
    </section>}

    {auth.token && <form className="ops-form" onSubmit={create} aria-label="Create scheduled job">
      <h2>Create a job</h2>
      <label>Name<input value={name} onChange={(event) => setName(event.target.value)} maxLength={200} required /></label>
      <label>Action<select value={actionType} onChange={(event) => setActionType(event.target.value)}>{ACTIONS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
      <label>Schedule type<select value={scheduleType} onChange={(event) => setScheduleType(event.target.value as ScheduleType)}>
        <option value="cron">cron (UTC)</option><option value="interval">interval (seconds)</option><option value="once">once</option>
      </select></label>
      <label>Schedule expression<input value={expression} onChange={(event) => setExpression(event.target.value)} maxLength={100} required aria-describedby="schedule-help" /></label>
      <small id="schedule-help" className="activity-meta">{EXPRESSION_HELP[scheduleType]}</small>
      {actionType === 'reminder' && <>
        <label>Reminder title (optional)<input value={reminderTitle} onChange={(event) => setReminderTitle(event.target.value)} maxLength={300} /></label>
        <label>Reminder message (optional)<input value={reminderMessage} onChange={(event) => setReminderMessage(event.target.value)} /></label>
      </>}
      {actionType === 'workflow' && <label>Workflow ID<input value={workflowId} onChange={(event) => setWorkflowId(event.target.value)} required /></label>}
      <label className="ops-inline"><input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} /> Enabled</label>
      <div><button type="submit" disabled={busy !== null || !name.trim() || !expression.trim()}>Create job</button></div>
    </form>}
  </section>
}
