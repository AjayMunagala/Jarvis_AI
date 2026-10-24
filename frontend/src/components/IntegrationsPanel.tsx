import { useEffect, useRef, useState, type FormEvent } from 'react'
import {
  SECRET_REF_PATTERN,
  activateEnrollment,
  createEnrollment,
  isApprovalRequired,
  listAdapters,
  listEnrollments,
  revokeEnrollment,
  runIntegrationAction,
  type AdapterDescriptor,
  type ApiAuth,
  type EnrollmentPage,
  type EnrollmentView,
  type IntegrationActionResult,
} from '../services/api'
import { ApprovalRequested, ConfirmButton, formatTime, shortJson, type ApprovalMemo } from './ApprovalGate'
import './ActivityPanel.css'
import './OperationsPanels.css'

type Props = { auth: ApiAuth; onUnlock: () => void; onOpenApprovals?: () => void; approvalMemo?: ApprovalMemo }
type ActionReplay = { action: string; arguments: Record<string, unknown> }

const SECRET_LABELS = { not_required: 'no secret needed', present: 'secret reference resolves', missing: 'secret reference missing' } as const

function message(cause: unknown, fallback: string) {
  return cause instanceof Error ? cause.message : fallback
}

/** The server accepts at most this many argument keys (ActionRequest.arguments, max_length=10). */
export const MAX_ACTION_ARGUMENTS = 10

function ActionForm({ view, adapter, disabled, approvalPending, onRun }: {
  view: EnrollmentView
  adapter: AdapterDescriptor | undefined
  disabled: boolean
  /** A state-changing action already waits for approval: run that one (or forget it) before asking for another. */
  approvalPending: boolean
  onRun: (replay: ActionReplay) => void
}) {
  const actions = adapter?.actions ?? []
  const [action, setAction] = useState(actions[0]?.name ?? '')
  const [args, setArgs] = useState('{}')
  const [problem, setProblem] = useState('')
  if (!actions.length) return <p className="activity-meta">The adapter for this enrollment is not available.</p>
  const spec = actions.find((item) => item.name === action)
  const label = view.enrollment.display_name
  const gated = approvalPending && !!spec && !spec.read_only
  return <form className="ops-form" aria-label={`Actions for ${label}`} onSubmit={(event: FormEvent) => {
    event.preventDefault()
    let parsed: unknown
    try { parsed = JSON.parse(args || '{}') } catch { setProblem('Arguments must be a JSON object, for example {"on": true}.'); return }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) { setProblem('Arguments must be a JSON object, for example {"on": true}.'); return }
    if (Object.keys(parsed).length > MAX_ACTION_ARGUMENTS) { setProblem(`Arguments can have at most ${MAX_ACTION_ARGUMENTS} keys.`); return }
    setProblem('')
    onRun({ action, arguments: parsed as Record<string, unknown> })
  }}>
    <label>Action<select value={action} onChange={(event) => setAction(event.target.value)}>{actions.map((item) => <option key={item.name} value={item.name}>{item.name}{item.read_only ? ' (read only)' : ' (needs approval)'}</option>)}</select></label>
    {spec && <small className="activity-meta">{spec.description}</small>}
    <label>Arguments (JSON)<input value={args} onChange={(event) => setArgs(event.target.value)} spellCheck={false} /></label>
    {problem && <p role="alert">{problem}</p>}
    {gated && <small className="activity-meta">An approval for a state-changing action is already pending below. Run it with that approval or forget it first.</small>}
    <div><button type="submit" disabled={disabled || gated}>Run action</button></div>
  </form>
}

export default function IntegrationsPanel({ auth, onUnlock, onOpenApprovals, approvalMemo }: Props) {
  const [adapters, setAdapters] = useState<AdapterDescriptor[] | null>(null)
  const [page, setPage] = useState<EnrollmentPage | null>(null)
  const [result, setResult] = useState<{ label: string; data: IntegrationActionResult } | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [adapterId, setAdapterId] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [secretRef, setSecretRef] = useState('')
  const [, setMemoVersion] = useState(0)
  const ownMemo = useRef<ApprovalMemo>(new Map())
  const memo = approvalMemo ?? ownMemo.current
  const generation = useRef(0)

  async function refresh(version = generation.current) {
    try {
      const [nextAdapters, nextPage] = await Promise.all([listAdapters(auth), listEnrollments(50, auth)])
      if (version !== generation.current) return
      setAdapters(nextAdapters); setPage(nextPage)
      setAdapterId((current) => current || nextAdapters[0]?.adapter_id || '')
    } catch (cause) {
      if (version === generation.current) setError(message(cause, 'Integrations are unavailable.'))
    }
  }

  useEffect(() => {
    generation.current += 1
    setAdapters(null); setPage(null); setResult(null); setError(''); setNotice(''); setBusy(null)
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

  function forget(key: string) {
    memo.delete(key)
    setMemoVersion((value) => value + 1)
  }

  function enroll(event: FormEvent) {
    event.preventDefault()
    const ref = secretRef.trim()
    if (ref && !SECRET_REF_PATTERN.test(ref)) {
      setError('A secret reference must look like env:JARVIS_INTEGRATION_NAME (capital letters, digits and underscores). Never enter the secret itself.')
      setNotice('')
      return
    }
    void act('enroll', async (version) => {
      const created = await createEnrollment({ adapter_id: adapterId, display_name: displayName.trim(), ...(ref ? { secret_ref: ref } : {}) }, auth)
      if (version !== generation.current) return
      setDisplayName(''); setSecretRef('')
      setNotice(`"${created.enrollment.display_name}" enrolled as pending. Activate it to use it; activation needs your approval.`)
      await refresh(version)
    }, 'The integration could not be enrolled.')
  }

  function activate(view: EnrollmentView, approvalId?: string) {
    const key = `integration-activate:${view.enrollment.enrollment_id}`
    void act(key, async (version) => {
      const response = await activateEnrollment(view.enrollment.enrollment_id, approvalId, auth)
      if (version !== generation.current) return
      if (isApprovalRequired(response)) {
        memo.set(key, { approvalId: response.approval.approval_id, summary: `activating "${view.enrollment.display_name}"` })
        setMemoVersion((value) => value + 1)
        return
      }
      if (approvalId) forget(key)
      setNotice(`"${response.enrollment.display_name}" is ${response.enrollment.state}.`)
      await refresh(version)
    }, 'The integration could not be activated.')
  }

  function revoke(view: EnrollmentView) {
    void act(`revoke:${view.enrollment.enrollment_id}`, async (version) => {
      const updated = await revokeEnrollment(view.enrollment.enrollment_id, auth)
      if (version !== generation.current) return
      memo.delete(`integration-activate:${view.enrollment.enrollment_id}`)
      memo.delete(`integration-action:${view.enrollment.enrollment_id}`)
      setNotice(`"${updated.enrollment.display_name}" was removed (revoked). It can no longer run actions.`)
      await refresh(version)
    }, 'The integration could not be removed.')
  }

  function runAction(view: EnrollmentView, replay: ActionReplay, approvalId?: string) {
    const key = `integration-action:${view.enrollment.enrollment_id}`
    void act(key, async (version) => {
      const body = approvalId ? { ...replay, approval_id: approvalId } : replay
      const response = await runIntegrationAction(view.enrollment.enrollment_id, body, auth)
      if (version !== generation.current) return
      if (isApprovalRequired(response)) {
        memo.set(key, { approvalId: response.approval.approval_id, summary: `${replay.action} ${shortJson(replay.arguments, 120)} on "${view.enrollment.display_name}"`, replay })
        setMemoVersion((value) => value + 1)
        return
      }
      if (approvalId) forget(key)
      setResult({ label: view.enrollment.display_name, data: response })
      setNotice(`${response.action} ran on "${view.enrollment.display_name}" (${response.mode}).`)
      await refresh(version)
    }, 'The action failed. Nothing was changed.')
  }

  const byId = new Map((adapters ?? []).map((adapter) => [adapter.adapter_id, adapter]))
  const current = page?.enrollments.filter((view) => view.enrollment.state !== 'revoked') ?? []
  const revoked = page?.enrollments.filter((view) => view.enrollment.state === 'revoked') ?? []

  return <section className="activity-panel" aria-labelledby="integrations-heading">
    <p className="activity-kicker">DEVICES · INTEGRATIONS</p>
    <h1 id="integrations-heading">Integrations</h1>
    <p>Connections to external devices and services. Only a simulated (mock) lamp exists today; no real device is contacted. An enrollment starts as pending; activating it and every state-changing action need your approval. Credentials are never stored or shown: an enrollment only keeps the name of an environment variable.</p>
    {!auth.token && <div role="status"><p>Unlock the workspace to view and manage integrations.</p><button type="button" onClick={onUnlock}>Unlock integrations</button></div>}
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status" className="activity-notice">{notice}</p>}
    {auth.token && !page && !error && <p role="status">Loading integrations…</p>}

    {page && <section aria-label="Enrollments">
      <h2>Enrollments</h2>
      <p className="activity-meta">Storage: {page.durable_storage ? 'durable (PostgreSQL)' : 'in memory only, cleared on restart'}.</p>
      {current.length ? <ul className="ops-list">{current.map((view) => {
        const item = view.enrollment
        const activation = memo.get(`integration-activate:${item.enrollment_id}`)
        const action = memo.get(`integration-action:${item.enrollment_id}`)
        return <li key={item.enrollment_id}>
          <div><strong>{item.display_name}</strong> <span className={`ops-status ops-status-${item.state}`}>{item.state}</span> <span className={`ops-status ops-status-${view.secret_status}`}>{SECRET_LABELS[view.secret_status]}</span> <code>{item.enrollment_id}</code></div>
          <small>Adapter {byId.get(item.adapter_id)?.name ?? item.adapter_id}{item.secret_ref ? <> · secret reference <code>{item.secret_ref}</code></> : ''} · created {formatTime(item.created_at)}{item.last_action_at ? ` · last action ${formatTime(item.last_action_at)}` : ''}</small>
          <div className="activity-actions">
            {item.state === 'pending' && !activation && <button type="button" onClick={() => activate(view)} disabled={busy !== null} aria-label={`Activate ${item.display_name}`}>Activate</button>}
            <ConfirmButton label="Remove" question={`Remove "${item.display_name}"? It is revoked at once and permanently.`} confirmLabel="Remove integration" onConfirm={() => revoke(view)} disabled={busy !== null} />
          </div>
          {activation && <ApprovalRequested pending={activation} retryLabel="Activate with approval" disabled={busy !== null}
            onRetry={() => activate(view, activation.approvalId)} onForget={() => forget(`integration-activate:${item.enrollment_id}`)} onOpenApprovals={onOpenApprovals} />}
          {item.state === 'active' && <ActionForm view={view} adapter={byId.get(item.adapter_id)} disabled={busy !== null} approvalPending={!!action} onRun={(replay) => runAction(view, replay)} />}
          {action && <ApprovalRequested pending={action} retryLabel="Run with approval" disabled={busy !== null}
            onRetry={() => runAction(view, action.replay as ActionReplay, action.approvalId)} onForget={() => forget(`integration-action:${item.enrollment_id}`)} onOpenApprovals={onOpenApprovals} />}
        </li>
      })}</ul> : <p>No integrations enrolled.</p>}
      {revoked.length > 0 && <details><summary>Removed ({revoked.length})</summary><ul className="activity-decided">{revoked.map((view) => <li key={view.enrollment.enrollment_id}>
        {view.enrollment.display_name} · revoked {formatTime(view.enrollment.revoked_at)}
      </li>)}</ul></details>}
    </section>}

    {result && <section aria-label="Last action result">
      <h2>Last action result</h2>
      <p><strong>{result.label}</strong> · {result.data.action} · {result.data.read_only ? 'read only' : 'changed state'} · {result.data.mode} · {formatTime(result.data.executed_at)}</p>
      <pre className="ops-output">{shortJson(result.data.output)}</pre>
    </section>}

    {adapters && <section aria-label="Available adapters">
      <h2>Available adapters</h2>
      {adapters.length ? <ul className="ops-list">{adapters.map((adapter) => <li key={adapter.adapter_id}>
        <div><strong>{adapter.name}</strong> <code>{adapter.adapter_id}</code> <span className="ops-status">{adapter.mode}</span></div>
        <small>Actions: {adapter.actions.map((item) => `${item.name}${item.read_only ? ' (read only)' : ' (needs approval)'}`).join(', ')}</small>
        {adapter.limitations.map((line) => <p key={line} className="activity-meta">{line}</p>)}
      </li>)}</ul> : <p>No adapters are registered.</p>}
    </section>}

    {adapters && adapters.length > 0 && <form className="ops-form" onSubmit={enroll} aria-label="Enroll integration">
      <h2>Enroll an integration</h2>
      <label>Adapter<select value={adapterId} onChange={(event) => setAdapterId(event.target.value)}>{adapters.map((adapter) => <option key={adapter.adapter_id} value={adapter.adapter_id}>{adapter.name}</option>)}</select></label>
      <label>Display name<input value={displayName} onChange={(event) => setDisplayName(event.target.value)} maxLength={100} required /></label>
      <label>Secret reference (optional)<input value={secretRef} onChange={(event) => setSecretRef(event.target.value)} placeholder="env:JARVIS_INTEGRATION_LAMP" autoComplete="off" spellCheck={false} aria-describedby="secret-help" /></label>
      <small id="secret-help" className="activity-meta">Only the name of an environment variable on the JARVIS server. Never type a password, key or token here.</small>
      <div><button type="submit" disabled={busy !== null || !displayName.trim() || !adapterId}>Enroll</button></div>
    </form>}
  </section>
}
