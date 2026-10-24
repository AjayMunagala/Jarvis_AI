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
  type WorkflowStatus,
} from '../services/api'
import { ApprovalRequested, ConfirmButton, formatTime, shortJson, type ApprovalMemo } from './ApprovalGate'
import './ActivityPanel.css'
import './OperationsPanels.css'

/** How often a run started from this view is checked until it finishes. */
export const RUN_POLL_MS = 2_000
const TERMINAL: ReadonlySet<WorkflowStatus> = new Set(['completed', 'failed', 'cancelled'])

const EXAMPLE_STEPS = JSON.stringify([
  { step_id: 'collect', name: 'Collect inputs', step_type: 'transform' },
  { step_id: 'finish', name: 'Finish', step_type: 'noop', depends_on: ['collect'] },
], null, 2)

// Mirrors jarvis/workflow/models.py (WorkflowStepDefinition, DAGSpec); the server still validates everything.
const STEP_KEYS = new Set(['step_id', 'name', 'step_type', 'action', 'parameters', 'depends_on'])
const STEP_TYPES = new Set(['tool', 'assistant', 'transform', 'noop'])
const STEP_ID = /^[a-zA-Z0-9_-]+$/

type Props = { auth: ApiAuth; onUnlock: () => void; onOpenApprovals?: () => void; approvalMemo?: ApprovalMemo }
type Steps = WorkflowDefinitionCreate['dag_spec']['steps']

function message(cause: unknown, fallback: string) {
  return cause instanceof Error ? cause.message : fallback
}

export function parseSteps(text: string): Steps {
  let value: unknown
  try { value = JSON.parse(text) } catch { throw new Error('Steps must be valid JSON: a list of step objects.') }
  if (!Array.isArray(value) || value.length === 0 || value.length > 50) throw new Error('Steps must be a list of 1 to 50 step objects.')
  const ids = new Set<string>()
  for (const step of value as Record<string, unknown>[]) {
    if (!step || typeof step !== 'object' || Array.isArray(step)) throw new Error('Every step must be an object.')
    const unknown = Object.keys(step).filter((key) => !STEP_KEYS.has(key))
    if (unknown.length) throw new Error(`Unknown step field "${unknown[0]}". Allowed: ${[...STEP_KEYS].join(', ')}.`)
    const id = step.step_id
    if (typeof id !== 'string' || !STEP_ID.test(id.trim()) || id.trim().length > 100) {
      throw new Error('Every step needs a step_id of 1 to 100 letters, digits, "_" or "-".')
    }
    if (typeof step.name !== 'string' || !step.name.trim() || step.name.trim().length > 200) throw new Error(`Step "${id}" needs a name of 1 to 200 characters.`)
    if (step.step_type !== undefined && (typeof step.step_type !== 'string' || !STEP_TYPES.has(step.step_type))) {
      throw new Error(`Step "${id}" has an unknown step_type; use noop, transform, assistant or tool.`)
    }
    if (step.action !== undefined && (typeof step.action !== 'string' || step.action.length > 200)) throw new Error(`Step "${id}" has an invalid action.`)
    if (step.parameters !== undefined && (!step.parameters || typeof step.parameters !== 'object' || Array.isArray(step.parameters))) {
      throw new Error(`Step "${id}" parameters must be an object.`)
    }
    if (step.depends_on !== undefined && (!Array.isArray(step.depends_on) || step.depends_on.length > 50 || !step.depends_on.every((item) => typeof item === 'string'))) {
      throw new Error(`Step "${id}" depends_on must be a list of step_ids.`)
    }
    if (ids.has(id.trim())) throw new Error(`The step_id "${id}" is used more than once.`)
    ids.add(id.trim())
  }
  const deps = new Map<string, string[]>()
  for (const step of value as { step_id: string; depends_on?: string[] }[]) {
    const id = step.step_id.trim()
    const list = (step.depends_on ?? []).map((item) => item.trim())
    for (const dependency of list) {
      if (dependency === id) throw new Error(`Step "${id}" cannot depend on itself.`)
      if (!ids.has(dependency)) throw new Error(`Step "${id}" depends on an unknown step "${dependency}".`)
    }
    deps.set(id, list)
  }
  // Kahn's algorithm, as on the server: every step must be reachable in topological order.
  const remaining = new Map([...deps].map(([id, list]) => [id, new Set(list).size]))
  const queue = [...remaining].filter(([, count]) => count === 0).map(([id]) => id)
  let visited = 0
  while (queue.length) {
    const current = queue.shift() as string
    visited += 1
    for (const [id, list] of deps) {
      if (new Set(list).has(current)) {
        const left = (remaining.get(id) ?? 0) - 1
        remaining.set(id, left)
        if (left === 0) queue.push(id)
      }
    }
  }
  if (visited !== ids.size) throw new Error('The steps contain a cycle; a workflow must be acyclic.')
  return value as Steps
}

export default function WorkflowsPanel({ auth, onUnlock, onOpenApprovals, approvalMemo }: Props) {
  const [workflows, setWorkflows] = useState<WorkflowDefinition[] | null>(null)
  const [runs, setRuns] = useState<WorkflowRun[] | null>(null)
  const [detail, setDetail] = useState<WorkflowRunDetail | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  // Runs started from this view that have not finished yet: run ID -> workflow ID. They are polled until done.
  const [tracked, setTracked] = useState<Record<string, string>>({})
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [steps, setSteps] = useState(EXAMPLE_STEPS)
  const [, setMemoVersion] = useState(0)
  const ownMemo = useRef<ApprovalMemo>(new Map())
  const memo = approvalMemo ?? ownMemo.current
  const generation = useRef(0)
  const polling = useRef(false)

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
    setWorkflows(null); setRuns(null); setDetail(null); setError(''); setNotice(''); setBusy(null); setTracked({})
    if (auth.token) void refresh(generation.current)
  }, [auth.token]) // eslint-disable-line react-hooks/exhaustive-deps

  function untrack(runId: string) {
    setTracked((current) => {
      if (!(runId in current)) return current
      const next = { ...current }
      delete next[runId]
      return next
    })
  }

  /** Shows the latest state of one run everywhere it appears, and stops polling it once it has finished. */
  function applyRun(loaded: WorkflowRunDetail, announce: boolean) {
    const run = loaded.run
    setRuns((current) => current && (current.some((item) => item.id === run.id)
      ? current.map((item) => item.id === run.id ? run : item)
      : [run, ...current]))
    setDetail((current) => current && current.run.id === run.id ? loaded : current)
    if (TERMINAL.has(run.status)) {
      untrack(run.id)
      if (announce) setNotice(`Run ${run.id} ${run.status}.`)
    }
  }

  async function poll(ids: string[], version: number) {
    if (polling.current) return
    polling.current = true
    try {
      for (const id of ids) {
        let loaded: WorkflowRunDetail
        try {
          loaded = await getWorkflowRun(id, auth)
        } catch (cause) {
          if (version !== generation.current) return
          setError(message(cause, 'The run status could not be read.'))
          untrack(id)
          continue
        }
        if (version !== generation.current) return
        applyRun(loaded, true)
      }
    } finally {
      polling.current = false
    }
  }

  useEffect(() => {
    const ids = Object.keys(tracked)
    if (!ids.length || !auth.token) return
    const version = generation.current
    const timer = window.setInterval(() => { void poll(ids, version) }, RUN_POLL_MS)
    return () => window.clearInterval(timer)
  }, [tracked, auth.token]) // eslint-disable-line react-hooks/exhaustive-deps

  /** Runs one short request with the busy flag and the stale-response guard. */
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
      // wait=false: the server starts the run in the background and answers at once; it is not part of the
      // approval scope, so an approved repeat still matches.
      const result = await runWorkflow(workflowId, approvalId ? { inputs, approval_id: approvalId, wait: false } : { inputs, wait: false }, auth)
      if (version !== generation.current) return
      if (isApprovalRequired(result)) {
        memo.set(key, { approvalId: result.approval.approval_id, summary: `running workflow "${workflowName}"`, replay: inputs })
        setMemoVersion((value) => value + 1)
        return
      }
      if (approvalId) { memo.delete(key); setMemoVersion((value) => value + 1) }
      setDetail(result)
      setNotice(TERMINAL.has(result.run.status) ? `Run ${result.run.id} ${result.run.status}.` : `Run ${result.run.id} started; it is ${result.run.status}.`)
      await refresh(version)
      if (version !== generation.current) return
      applyRun(result, false)
      if (!TERMINAL.has(result.run.status)) setTracked((current) => ({ ...current, [result.run.id]: workflowId }))
    }, 'The workflow could not be started.')
  }

  function create(event: FormEvent) {
    event.preventDefault()
    let parsed: Steps
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
      if (version !== generation.current) return
      setDetail(loaded)
      applyRun(loaded, false)
    }, 'The run could not be loaded.')
  }

  function cancel(run: WorkflowRun) {
    void act(`cancel:${run.id}`, async (version) => {
      const updated = await cancelWorkflowRun(run.id, auth)
      if (version !== generation.current) return
      setDetail(updated)
      applyRun(updated, false)
      setNotice(`Run ${run.id} is ${updated.run.status}.`)
    }, 'The run could not be cancelled.')
  }

  const names = new Map((workflows ?? []).map((item) => [item.id, item.name]))
  const runningHere = new Set(Object.values(tracked))

  return <section className="activity-panel" aria-labelledby="workflows-heading">
    <p className="activity-kicker">AUTOMATION · WORKFLOWS</p>
    <h1 id="workflows-heading">Workflows</h1>
    <p>Workflows are ordered steps that JARVIS runs for you. A run whose steps only read data starts at once. A run with steps that write data needs your approval first: JARVIS records an approval request and runs nothing until you approve it and run the workflow again with that approval ID. Runs continue in the background; this view checks on them every few seconds.</p>
    {!auth.token && <div role="status"><p>Unlock the workspace to view and run workflows.</p><button type="button" onClick={onUnlock}>Unlock workflows</button></div>}
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status" className="activity-notice">{notice}</p>}
    {auth.token && !workflows && !error && <p role="status">Loading workflows…</p>}

    {workflows && <section aria-label="Workflow definitions">
      <h2>Definitions</h2>
      {workflows.length ? <ul className="ops-list">{workflows.map((workflow) => {
        const pending = memo.get(`workflow:${workflow.id}`)
        const running = runningHere.has(workflow.id)
        return <li key={workflow.id}>
          <div><strong>{workflow.name}</strong> <code>{workflow.id}</code>{running && <> <span className="ops-status ops-status-running">running</span></>}</div>
          {workflow.description && <p>{workflow.description}</p>}
          <details><summary>Steps ({workflow.dag_spec.steps.length})</summary><ol className="ops-steps">{workflow.dag_spec.steps.map((step) => <li key={step.step_id}>
            <code>{step.step_id}</code> {step.name} · {step.step_type}{step.action ? ` · ${step.action}` : ''}{step.depends_on.length ? ` · after ${step.depends_on.join(', ')}` : ''}
          </li>)}</ol></details>
          <div className="activity-actions">
            {/* While an approval is pending, the only way to run is with that approval, so it cannot be replaced silently. */}
            {!pending && <button type="button" onClick={() => void trigger(workflow.id, workflow.name, {})} disabled={busy !== null || running}
              aria-label={`Run ${workflow.name}`} title={running ? 'A run started here is still running.' : undefined}>Run</button>}
            <ConfirmButton label="Delete" question={`Delete workflow "${workflow.name}"? Its definition is removed permanently.`} confirmLabel="Delete workflow" onConfirm={() => remove(workflow)} disabled={busy !== null} />
          </div>
          {pending && <ApprovalRequested pending={pending} retryLabel="Run with approval" disabled={busy !== null || running}
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
        const blocked = !!run.workflow_id && (runningHere.has(run.workflow_id) || memo.has(`workflow:${run.workflow_id}`))
        return <li key={run.id}>
          <div><strong>{workflowName}</strong> <span className={`ops-status ops-status-${run.status}`}>{run.status}</span> <code>{run.id}</code></div>
          <small>Created {formatTime(run.created_at)}{run.completed_at ? ` · finished ${formatTime(run.completed_at)}` : ''}</small>
          {run.error_message && <p>{run.error_message}</p>}
          <div className="activity-actions">
            <button type="button" onClick={() => show(run)} disabled={busy !== null} aria-label={`Show steps of run ${run.id}`}>Show steps</button>
            {(run.status === 'pending' || run.status === 'running') && <ConfirmButton label="Cancel run" question="Cancel this run? Steps that already ran are not undone." confirmLabel="Cancel the run" onConfirm={() => cancel(run)} disabled={busy !== null} />}
            {(run.status === 'failed' || run.status === 'cancelled') && run.workflow_id && <ConfirmButton danger={false} label="Run again from the start"
              question="Run this workflow again from the start, with the same inputs? Steps that already completed will run again."
              confirmLabel="Run again" disabled={busy !== null || blocked}
              onConfirm={() => void trigger(run.workflow_id as string, workflowName, run.inputs)} />}
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
      {!detail.steps.length && <p>No step results recorded yet.</p>}
    </section>}

    {auth.token && <form className="ops-form" onSubmit={create} aria-label="Create workflow">
      <h2>Create a workflow</h2>
      <label>Name<input value={name} onChange={(event) => setName(event.target.value)} maxLength={200} required /></label>
      <label>Description (optional)<input value={description} onChange={(event) => setDescription(event.target.value)} maxLength={2000} /></label>
      <label>Steps (JSON)<textarea value={steps} onChange={(event) => setSteps(event.target.value)} spellCheck={false} /></label>
      <small className="activity-meta">Each step needs a step_id (letters, digits, "_" or "-") and a name; step_type is noop, transform, assistant or tool; optional fields are action, parameters and depends_on (earlier step_ids, no cycles). Creating a workflow runs nothing.</small>
      <div><button type="submit" disabled={busy !== null || !name.trim()}>Create workflow</button></div>
    </form>}
  </section>
}
