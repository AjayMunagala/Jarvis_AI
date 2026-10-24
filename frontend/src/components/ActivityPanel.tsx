import { useEffect, useRef, useState } from 'react'
import {
  decideApproval,
  getActivity,
  getApprovals,
  type ActivityPage,
  type ApiAuth,
  type ApprovalPage,
  type ApprovalRequest,
} from '../services/api'
import './ActivityPanel.css'

function executionLabel(item: ApprovalRequest): string {
  if (item.execution !== 'single_use') return 'not available'
  if (item.consumed_at) return `used once at ${new Date(item.consumed_at).toLocaleTimeString()}`
  return item.state === 'approved' ? 'approved, not used yet' : 'not run'
}

export default function ActivityPanel({ auth, onUnlock }: { auth: ApiAuth; onUnlock: () => void }) {
  const [activity, setActivity] = useState<ActivityPage | null>(null)
  const [approvals, setApprovals] = useState<ApprovalPage | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const generation = useRef(0)

  async function refresh(version = generation.current) {
    try {
      const [nextActivity, nextApprovals] = await Promise.all([getActivity(50, auth), getApprovals(50, auth)])
      if (version !== generation.current) return
      setActivity(nextActivity); setApprovals(nextApprovals); setError('')
    } catch (cause) {
      if (version === generation.current) setError(cause instanceof Error ? cause.message : 'Activity is unavailable.')
    }
  }

  useEffect(() => {
    generation.current += 1
    setActivity(null); setApprovals(null); setError(''); setNotice(''); setBusy(null)
    if (auth.token) void refresh(generation.current)
  }, [auth.token]) // eslint-disable-line react-hooks/exhaustive-deps

  async function decide(item: ApprovalRequest, approve: boolean) {
    if (busy) return
    const version = generation.current
    setBusy(item.approval_id); setError(''); setNotice('')
    try {
      const updated = await decideApproval(item.approval_id, approve, undefined, auth)
      if (version !== generation.current) return
      setNotice(updated.execution === 'single_use' && updated.state === 'approved'
        ? `${updated.approval_id} approved. Nothing has run yet: repeat the action with approval_id=${updated.approval_id} to run it once.`
        : `${updated.approval_id} ${updated.state}. Nothing was executed.`)
      await refresh(version)
    } catch (cause) {
      if (version === generation.current) setError(cause instanceof Error ? cause.message : 'The decision failed.')
    } finally {
      if (version === generation.current) setBusy(null)
    }
  }

  const pending = approvals?.approvals.filter((item) => item.state === 'pending') ?? []
  const decided = approvals?.approvals.filter((item) => item.state !== 'pending') ?? []

  return <section className="activity-panel" aria-labelledby="activity-heading">
    <p className="activity-kicker">AUDIT TRAIL · APPROVALS</p>
    <h1 id="activity-heading">Activity</h1>
    <p>Every tool call, browser action and approval decision is recorded here. Approving a tool or browser request records your decision only. Autonomous, improvement and quarantine actions wait for your approval, then run once when the same action is submitted again with the approval ID.</p>
    {!auth.token && <div role="status"><p>Unlock the workspace to view activity.</p><button type="button" onClick={onUnlock}>Unlock activity</button></div>}
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status" className="activity-notice">{notice}</p>}

    {approvals && <section aria-label="Approval requests">
      <h2>Approval requests</h2>
      <p className="activity-meta">{approvals.execution_note} Storage: {approvals.durable_storage ? 'durable (PostgreSQL)' : 'in memory only, cleared on restart'}.</p>
      {pending.length ? <ul className="activity-approvals">{pending.map((item) => <li key={item.approval_id}>
        <div><strong>{item.action}</strong> <span className={`activity-risk activity-risk-${item.risk}`}>{item.risk}</span> <code>{item.approval_id}</code></div>
        <p>{item.target}</p>
        <small>{item.reason} Expected side effects: {item.expected_side_effects} Expires {new Date(item.expires_at).toLocaleTimeString()}.</small>
        <div className="activity-actions">
          <button type="button" onClick={() => void decide(item, true)} disabled={busy !== null}>{item.execution === 'single_use' ? 'Approve (allows one run)' : 'Approve (record only)'}</button>
          <button type="button" className="activity-reject" onClick={() => void decide(item, false)} disabled={busy !== null}>Reject</button>
        </div>
      </li>)}</ul> : <p>No pending approval requests.</p>}
      {decided.length > 0 && <details><summary>Decided and expired ({decided.length})</summary><ul className="activity-decided">{decided.map((item) => <li key={item.approval_id}>
        {item.action} · {item.state}{item.decided_at ? ` at ${new Date(item.decided_at).toLocaleTimeString()}` : ''} · execution: {executionLabel(item)}
      </li>)}</ul></details>}
    </section>}

    {activity && <section aria-label="Audit trail">
      <h2>Audit trail</h2>
      <p className="activity-meta">{activity.storage_note}</p>
      {activity.events.length ? <ol className="activity-events">{activity.events.map((item) => <li key={item.event_id}>
        <span>{new Date(item.occurred_at).toLocaleTimeString()}</span> <strong>{item.category}</strong> {item.event_type} · {item.subject} · {item.decision} / {item.status}
        {!item.durable && <em className="activity-volatile"> (not durable)</em>}
      </li>)}</ol> : <p>No activity recorded yet.</p>}
    </section>}
  </section>
}
