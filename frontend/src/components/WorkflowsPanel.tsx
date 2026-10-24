import { useEffect, useRef, useState, type FormEvent } from 'react'
import {
  cancelWorkflowRun,
  createWorkflow,
  deleteWorkflow,
  getWorkflowRun,
  isApprovalRequired,
  listWorkflowRuns,
  listWorkflows,
  runWorkflow,
  type ApiAuth,
  type WorkflowDefinition,
  type WorkflowDefinitionCreate,
  type WorkflowRun,
  type WorkflowRunDetail,
} from '../services/api'
import { ApprovalRequested, ConfirmButton, formatTime, shortJson, type ApprovalMemo } from './ApprovalGate'
import './ActivityPanel.css'
import './OperationsPanels.css'

const EXAMPLE_STEPS = JSON.stringify([
  { step_id: 'collect', name: 'Collect inputs', step_type: 'transform' },
  { step_id: 'finish', name: 'Finish', step_type: 'noop', depends_on: ['collect'] },
], null, 2)

type Props = { auth: ApiAuth; onUnlock: () => void; onOpenApprovals?: () => void; approvalMemo?: ApprovalMemo }

function message(cause: unknown, fallback: string) {
  return cause instanceof Error ? cause.message : fallback
}

function parseSteps(text: string): WorkflowDefinitionCreate['dag_spec']['steps'] {
  let value: unknown
  try { value = JSON.parse(text) } catch { throw new Error('Steps must be valid JSON: a list of step objects.') }
  if (!Array.isArray(value) || value.length === 0 || value.length > 50) throw new Error('Steps must be a list of 1 to 50 step objects.')
  const ids = new Set<string>()
  for (const step of value) {
    if (!step || typeof step !== 'object' || typeof step.step_id !== 'string' || typeof step.name !== 'string') {
      throw new Error('Every step needs a step_id and a name.')
    }
    if (ids.has(step.step_id)) throw new Error(`The step_id "${step.step_id}" is used more than once.`)
    ids.add(step.step_id)
  }
  for (const step of value) {
    for (const dependency of (step.depends_on ?? []) as unknown[]) {
      if (typeof dependency !== 'string' || !ids.has(dependency)) throw new Error(`Step "${step.step_id}" depends on an unknown step.`)
    }
  }
  return value
}

export default function WorkflowsPanel({ auth, onUnlock, onOpenApprovals, approvalMemo }: Props) {
  const [workflows, setWorkflows] = useState<WorkflowDefinition[] | null>(null)
  const [runs, setRuns] = useState<WorkflowRun[] | null>(null)
  const [detail, setDetail] = useState<WorkflowRunDetail | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [steps, setSteps] = useState(EXAMPLE_STEPS)
  const [, setMemoVersion] = useState(0)
  const ownMemo = useRef<ApprovalMemo>(new Map())
  const memo = approvalMemo ?? ownMemo.current
  const generation = useRef(0)

  async function refresh(version = generation.current) {
    try {
      const [definitions, runPage] = await Promise.all([listWorkflows(50, 0, auth), listWorkflowRuns({ limit: 20 }, auth)])
      if (version !== generation.current) return
      setWorkflows(definitions.items); setRuns(runPage.items)
    } catch (cause) {
      if (version === generation.current) setError(message(cause, 'Workflows are unavailable.'))
    }
  }

  useEffect(() => {
    generation.current += 1
    setWorkflows(null); setRuns(null); setDetail(null); setError(''); setNotice(''); setBusy(null)
    if (auth.token) void refresh(generation.current)
  }, [auth.token]) // eslint-disable-line react-hooks/exhaustive-deps

  /** Runs one action with the busy flag and the stale-response guard; returns false when the result is stale. */
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

  function trigger(workflowId: string, workflowName: string, inputs: Record<string, unknown>, approvalId?: string) {
    const key = `workflow:${workflowId}`
    return act(key, async (version) => {
      const result = await runWorkflow(workflowId, approvalId ? { inputs, approval_id: approvalId } : { inputs }, auth)
      if (version !== generation.current) return
      if (isApprovalRequired(result)) {
        memo.set(key, { approvalId: result.approval.approval_id, summary: `running workflow "${workflowName}"`, replay: inputs })
        setMemoVersion((value) => value + 1)
        return
      }
      if (approvalId) { memo.delete(key); setMemoVersion((value) => value + 1) }
      setDetail(result)
      setNotice(`Run ${result.run.id} ${result.run.status}.`)
      await refresh(version)
    }, 'The workflow could not be started.')
  }

  function create(event: FormEvent) {
    event.preventDefault()
    let parsed: WorkflowDefinitionCreate['dag_spec']['steps']
    try { parsed = parseSteps(steps) } catch (cause) { setError(message(cause, 'The steps are invalid.')); setNotice(''); return }
    const body: WorkflowDefinitionCreate = { name: name.trim(), description: description.trim() || null, dag_spec: { steps: parsed } }
    void act('create', async (version) => {
      const created = await createWorkflow(body, auth)
      if (version !== generation.current) return
      setName(''); setDescription(''); setSteps(EXAMPLE_STEPS)
      setNotice(`Workflow "${created.name}" created. Nothing has run.`)
      await refresh(version)
    }, 'The workflow could not be created.')
  }

  function remove(workflow: WorkflowDefinition) {
    void act(`delete:${workflow.id}`, async (version) => {
      await deleteWorkflow(workflow.id, auth)
      if (version !== generation.current) return
      memo.delete(`workflow:${workflow.id}`)
      setNotice(`Workflow "${workflow.name}" deleted.`)
      await refresh(version)
    }, 'The workflow could not be deleted.')
  }

  function show(run: WorkflowRun) {
    void act(`detail:${run.id}`, async (version) => {
      const loaded = await getWorkflowRun(run.id, auth)
      if (version === generation.current) setDetail(loaded)
    }, 'The run could not be loaded.')
  }

  function cancel(run: WorkflowRun) {
    void act(`cancel:${run.id}`, async (version) => {
      const updated = await cancelWorkflowRun(run.id, auth)
      if (version !== generation.current) return
      setDetail(updated)
      setNotice(`Run ${run.id} is ${updated.run.status}.`)
      await refresh(version)
    }, 'The run could not be cancelled.')
  }

  const names = new Map((workflows ?? []).map((item) => [item.id, item.name]))

  return <section className="activity-panel" aria-labelledby="workflows-heading">
    <p className="activity-kicker">AUTOMATION · WORKFLOWS</p>
    <h1 id="workflows-heading">Workflows</h1>
    <p>Workflows are ordered steps that JARVIS runs for you. A run whose steps only read data starts at once. A run with steps that write data needs your approval first: JARVIS records an approval request and runs nothing until you approve it and run the workflow again with that approval ID.</p>
    {!auth.token && <div role="status"><p>Unlock the workspace to view and run workflows.</p><button type="button" onClick={onUnlock}>Unlock workflows</button></div>}
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status" className="activity-notice">{notice}</p>}
    {auth.token && !workflows && !error && <p role="status">Loading workflows…</p>}

    {workflows && <section aria-label="Workflow definitions">
      <h2>Definitions</h2>
      {workflows.length ? <ul className="ops-list">{workflows.map((workflow) => {
        const pending = memo.get(`workflow:${workflow.id}`)
        return <li key={workflow.id}>
          <div><strong>{workflow.name}</strong> <code>{workflow.id}</code></div>
          {workflow.description && <p>{workflow.description}</p>}
          <details><summary>Steps ({workflow.dag_spec.steps.length})</summary><ol className="ops-steps">{workflow.dag_spec.steps.map((step) => <li key={step.step_id}>
            <code>{step.step_id}</code> {step.name} · {step.step_type}{step.action ? ` · ${step.action}` : ''}{step.depends_on.length ? ` · after ${step.depends_on.join(', ')}` : ''}
          </li>)}</ol></details>
          <div className="activity-actions">
            <button type="button" onClick={() => void trigger(workflow.id, workflow.name, {})} disabled={busy !== null} aria-label={`Run ${workflow.name}`}>Run</button>
            <ConfirmButton label="Delete" question={`Delete workflow "${workflow.name}"? Its definition is removed permanently.`} confirmLabel="Delete workflow" onConfirm={() => remove(workflow)} disabled={busy !== null} />
          </div>
          {pending && <ApprovalRequested pending={pending} retryLabel="Run with approval" disabled={busy !== null}
            onRetry={() => void trigger(workflow.id, workflow.name, (pending.replay as Record<string, unknown>) ?? {}, pending.approvalId)}
            onForget={() => { memo.delete(`workflow:${workflow.id}`); setMemoVersion((value) => value + 1) }}
            onOpenApprovals={onOpenApprovals} />}
        </li>
      })}</ul> : <p>No workflows yet.</p>}
    </section>}

    {runs && <section aria-label="Workflow runs">
      <h2>Recent runs</h2>
      {runs.length ? <ul className="ops-list">{runs.map((run) => {
        const workflowName = (run.workflow_id && names.get(run.workflow_id)) || 'Unknown workflow'
        return <li key={run.id}>
          <div><strong>{workflowName}</strong> <span className={`ops-status ops-status-${run.status}`}>{run.status}</span> <code>{run.id}</code></div>
          <small>Created {formatTime(run.created_at)}{run.completed_at ? ` · finished ${formatTime(run.completed_at)}` : ''}</small>
          {run.error_message && <p>{run.error_message}</p>}
          <div className="activity-actions">
            <button type="button" onClick={() => show(run)} disabled={busy !== null} aria-label={`Show steps of run ${run.id}`}>Show steps</button>
            {(run.status === 'pending' || run.status === 'running') && <ConfirmButton label="Cancel run" question="Cancel this run? Steps that already ran are not undone." confirmLabel="Cancel the run" onConfirm={() => cancel(run)} disabled={busy !== null} />}
            {(run.status === 'failed' || run.status === 'cancelled') && run.workflow_id && <button type="button" disabled={busy !== null}
              onClick={() => void trigger(run.workflow_id as string, workflowName, run.inputs)}>Retry</button>}
          </div>
        </li>
      })}</ul> : <p>No workflow runs yet.</p>}
    </section>}

    {detail && <section aria-label="Run details">
      <h2>Run {detail.run.id}</h2>
      <p><span className={`ops-status ops-status-${detail.run.status}`}>{detail.run.status}</span>{detail.run.error_message ? ` ${detail.run.error_message}` : ''}</p>
      <div className="activity-actions"><button type="button" onClick={() => show(detail.run)} disabled={busy !== null}>Refresh run</button></div>
      <ol className="ops-steps">{detail.steps.map((step) => <li key={step.id}>
        <strong>{step.step_name}</strong> <code>{step.step_id}</code> <span className={`ops-status ops-status-${step.status}`}>{step.status}</span>
        {step.error_message && <p>{step.error_message}</p>}
        {Object.keys(step.outputs).length > 0 && <pre className="ops-output">{shortJson(step.outputs)}</pre>}
      </li>)}</ol>
      {!detail.steps.length && <p>No step results recorded.</p>}
    </section>}

    {auth.token && <form className="ops-form" onSubmit={create} aria-label="Create workflow">
      <h2>Create a workflow</h2>
      <label>Name<input value={name} onChange={(event) => setName(event.target.value)} maxLength={200} required /></label>
      <label>Description (optional)<input value={description} onChange={(event) => setDescription(event.target.value)} maxLength={2000} /></label>
      <label>Steps (JSON)<textarea value={steps} onChange={(event) => setSteps(event.target.value)} spellCheck={false} /></label>
      <small className="activity-meta">Each step needs a step_id and a name; step_type is noop, transform, assistant or tool; depends_on lists earlier step_ids. Creating a workflow runs nothing.</small>
      <div><button type="submit" disabled={busy !== null || !name.trim()}>Create workflow</button></div>
    </form>}
  </section>
}
