import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import WorkflowsPanel from './WorkflowsPanel'
import type { ApprovalMemo } from './ApprovalGate'
import {
  cancelWorkflowRun, createWorkflow, deleteWorkflow, getWorkflowRun, listWorkflowRuns, listWorkflows, runWorkflow,
  type ApprovalRequiredResponse, type WorkflowDefinition, type WorkflowRun, type WorkflowRunDetail,
} from '../services/api'

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>()
  return {
    isApprovalRequired: actual.isApprovalRequired,
    cancelWorkflowRun: vi.fn(), createWorkflow: vi.fn(), deleteWorkflow: vi.fn(), getWorkflowRun: vi.fn(),
    listWorkflowRuns: vi.fn(), listWorkflows: vi.fn(), runWorkflow: vi.fn(),
  }
})

const auth = { token: 'fake-test-token' }
const definition = (overrides: Partial<WorkflowDefinition> = {}): WorkflowDefinition => ({
  id: 'wf-1', owner_id: 'o', name: 'Weekly review', description: 'Collects tasks', created_at: '2026-10-24T07:00:00Z', updated_at: '2026-10-24T07:00:00Z',
  dag_spec: { steps: [
    { step_id: 'list', name: 'List tasks', step_type: 'assistant', action: 'tasks.list', parameters: {}, depends_on: [] },
    { step_id: 'make', name: 'Create task', step_type: 'assistant', action: 'tasks.create', parameters: {}, depends_on: ['list'] },
  ] },
  ...overrides,
})
const run = (overrides: Partial<WorkflowRun> = {}): WorkflowRun => ({
  id: 'run-1', workflow_id: 'wf-1', owner_id: 'o', status: 'failed', inputs: { week: 43 }, outputs: {}, error_message: 'Step make failed.',
  created_at: '2026-10-24T07:00:00Z', started_at: '2026-10-24T07:00:00Z', completed_at: '2026-10-24T07:00:01Z', ...overrides,
})
const detail = (overrides: Partial<WorkflowRun> = {}): WorkflowRunDetail => ({
  run: run({ id: 'run-2', status: 'completed', error_message: null, ...overrides }),
  steps: [{ id: 's-1', run_id: 'run-2', step_id: 'list', step_name: 'List tasks', status: 'completed', inputs: {}, outputs: { total: 3 },
    error_message: null, recovery_metadata: {}, started_at: null, completed_at: null }],
})
const gated: ApprovalRequiredResponse = {
  status: 'approval_required', detail: 'Approval required.',
  approval: { approval_id: 'AR-0123456789abcdef', owner_id: 'o', category: 'workflow', action: 'workflow.run', target: 'Weekly review', reason: 'r', risk: 'medium',
    expected_side_effects: 'e', scope_sha256: 'a'.repeat(64), correlation_id: null, created_at: '', expires_at: '', state: 'pending', decided_at: null,
    decision_note: null, execution: 'single_use', consumed_at: null, durable: false },
}

function load(runs: WorkflowRun[] = [run()]) {
  vi.mocked(listWorkflows).mockResolvedValue({ items: [definition()], total: 1 })
  vi.mocked(listWorkflowRuns).mockResolvedValue({ items: runs, total: runs.length })
}

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('WorkflowsPanel', () => {
  it('requires unlocking and makes no request without a token', async () => {
    const onUnlock = vi.fn()
    render(<WorkflowsPanel auth={{}} onUnlock={onUnlock} />)
    await userEvent.click(screen.getByRole('button', { name: 'Unlock workflows' }))
    expect(onUnlock).toHaveBeenCalled()
    expect(listWorkflows).not.toHaveBeenCalled()
    expect(listWorkflowRuns).not.toHaveBeenCalled()
  })

  it('lists definitions with their steps, and runs', async () => {
    load()
    render(<WorkflowsPanel auth={auth} onUnlock={() => undefined} />)
    expect(screen.getByText('Loading workflows…')).toBeInTheDocument()
    await screen.findByLabelText('Workflow definitions')
    expect(listWorkflows).toHaveBeenCalledWith(50, 0, auth)
    expect(listWorkflowRuns).toHaveBeenCalledWith({ limit: 20 }, auth)
    expect(screen.getByText('Steps (2)')).toBeInTheDocument()
    expect(screen.getByText(/tasks.create · after list/)).toBeInTheDocument()
    const runs = screen.getByLabelText('Workflow runs')
    expect(within(runs).getByText('failed')).toBeInTheDocument()
    expect(within(runs).getByText('Step make failed.')).toBeInTheDocument()
  })

  it('shows empty states', async () => {
    vi.mocked(listWorkflows).mockResolvedValue({ items: [], total: 0 })
    vi.mocked(listWorkflowRuns).mockResolvedValue({ items: [], total: 0 })
    render(<WorkflowsPanel auth={auth} onUnlock={() => undefined} />)
    expect(await screen.findByText('No workflows yet.')).toBeInTheDocument()
    expect(screen.getByText('No workflow runs yet.')).toBeInTheDocument()
  })

  it('shows a load error', async () => {
    vi.mocked(listWorkflows).mockRejectedValue(new Error('The workflow service is unavailable.'))
    vi.mocked(listWorkflowRuns).mockResolvedValue({ items: [], total: 0 })
    render(<WorkflowsPanel auth={auth} onUnlock={() => undefined} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('The workflow service is unavailable.')
  })

  it('runs a workflow and shows the step results', async () => {
    load([])
    vi.mocked(runWorkflow).mockResolvedValue(detail())
    render(<WorkflowsPanel auth={auth} onUnlock={() => undefined} />)
    await userEvent.click(await screen.findByRole('button', { name: 'Run Weekly review' }))
    expect(runWorkflow).toHaveBeenCalledWith('wf-1', { inputs: {} }, auth)
    expect(await screen.findByText('Run run-2 completed.')).toBeInTheDocument()
    const result = screen.getByLabelText('Run details')
    expect(within(result).getByText('{"total":3}')).toBeInTheDocument()
  })

  it('handles 202 approval-required, links to approvals and repeats the run with the approval ID', async () => {
    load([])
    const openApprovals = vi.fn()
    const memo: ApprovalMemo = new Map()
    vi.mocked(runWorkflow).mockResolvedValueOnce(gated).mockResolvedValueOnce(detail())
    const { unmount } = render(<WorkflowsPanel auth={auth} onUnlock={() => undefined} onOpenApprovals={openApprovals} approvalMemo={memo} />)
    await userEvent.click(await screen.findByRole('button', { name: 'Run Weekly review' }))
    expect(await screen.findByText(/Approval requested for running workflow "Weekly review"/)).toHaveTextContent('Nothing was executed.')
    expect(screen.getByText('AR-0123456789abcdef')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Open approvals' }))
    expect(openApprovals).toHaveBeenCalled()
    // the pending approval survives leaving the view (the app owns the memo)
    unmount()
    render(<WorkflowsPanel auth={auth} onUnlock={() => undefined} onOpenApprovals={openApprovals} approvalMemo={memo} />)
    await userEvent.click(await screen.findByRole('button', { name: 'Run with approval' }))
    expect(runWorkflow).toHaveBeenLastCalledWith('wf-1', { inputs: {}, approval_id: 'AR-0123456789abcdef' }, auth)
    expect(await screen.findByText('Run run-2 completed.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Run with approval' })).toBeNull()
    expect(memo.size).toBe(0)
  })

  it('retries a failed run with the same inputs', async () => {
    load()
    vi.mocked(runWorkflow).mockResolvedValue(detail())
    render(<WorkflowsPanel auth={auth} onUnlock={() => undefined} />)
    await userEvent.click(await screen.findByRole('button', { name: 'Retry' }))
    expect(runWorkflow).toHaveBeenCalledWith('wf-1', { inputs: { week: 43 } }, auth)
  })

  it('shows run steps and cancels a running run only after confirmation', async () => {
    load([run({ status: 'running', error_message: null, completed_at: null })])
    vi.mocked(getWorkflowRun).mockResolvedValue(detail({ status: 'running' }))
    vi.mocked(cancelWorkflowRun).mockResolvedValue(detail({ status: 'cancelled' }))
    render(<WorkflowsPanel auth={auth} onUnlock={() => undefined} />)
    await userEvent.click(await screen.findByRole('button', { name: 'Show steps of run run-1' }))
    expect(getWorkflowRun).toHaveBeenCalledWith('run-1', auth)
    expect(await screen.findByLabelText('Run details')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Cancel run' }))
    expect(cancelWorkflowRun).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(cancelWorkflowRun).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Cancel run' }))
    await userEvent.click(screen.getByRole('button', { name: 'Cancel the run' }))
    expect(cancelWorkflowRun).toHaveBeenCalledWith('run-1', auth)
    expect(await screen.findByText('Run run-1 is cancelled.')).toBeInTheDocument()
  })

  it('deletes a definition only after confirmation', async () => {
    load([])
    vi.mocked(deleteWorkflow).mockResolvedValue(undefined)
    render(<WorkflowsPanel auth={auth} onUnlock={() => undefined} />)
    await userEvent.click(await screen.findByRole('button', { name: 'Delete' }))
    expect(screen.getByRole('alertdialog')).toHaveAccessibleName(/Delete workflow "Weekly review"/)
    expect(deleteWorkflow).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Delete workflow' }))
    expect(deleteWorkflow).toHaveBeenCalledWith('wf-1', auth)
  })

  it('creates a workflow from JSON steps and validates them first', async () => {
    load([])
    vi.mocked(createWorkflow).mockResolvedValue(definition({ id: 'wf-2', name: 'Tidy' }))
    render(<WorkflowsPanel auth={auth} onUnlock={() => undefined} />)
    await screen.findByLabelText('Workflow definitions')
    await userEvent.type(screen.getByLabelText('Name'), 'Tidy')
    const steps = screen.getByLabelText('Steps (JSON)')
    await userEvent.clear(steps)
    await userEvent.click(steps); await userEvent.paste('[{"step_id":"a","name":"A","depends_on":["zzz"]}]')
    await userEvent.click(screen.getByRole('button', { name: 'Create workflow' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('depends on an unknown step')
    expect(createWorkflow).not.toHaveBeenCalled()
    await userEvent.clear(steps)
    await userEvent.click(steps); await userEvent.paste('[{"step_id":"a","name":"A","step_type":"noop"}]')
    await userEvent.click(screen.getByRole('button', { name: 'Create workflow' }))
    expect(createWorkflow).toHaveBeenCalledWith({ name: 'Tidy', description: null, dag_spec: { steps: [{ step_id: 'a', name: 'A', step_type: 'noop' }] } }, auth)
    expect(await screen.findByText('Workflow "Tidy" created. Nothing has run.')).toBeInTheDocument()
  })

  it('shows action errors', async () => {
    load([])
    vi.mocked(runWorkflow).mockRejectedValue(new Error('The approval request is not usable.'))
    render(<WorkflowsPanel auth={auth} onUnlock={() => undefined} />)
    await userEvent.click(await screen.findByRole('button', { name: 'Run Weekly review' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The approval request is not usable.')
  })

  it('ignores a response that arrives after the workspace was locked', async () => {
    let resolve: (value: { items: WorkflowDefinition[]; total: number }) => void = () => undefined
    vi.mocked(listWorkflows).mockReturnValue(new Promise((done) => { resolve = done }))
    vi.mocked(listWorkflowRuns).mockResolvedValue({ items: [], total: 0 })
    const { rerender } = render(<WorkflowsPanel auth={auth} onUnlock={() => undefined} />)
    rerender(<WorkflowsPanel auth={{}} onUnlock={() => undefined} />)
    await act(async () => { resolve({ items: [definition()], total: 1 }) })
    await waitFor(() => expect(screen.getByText('Unlock the workspace to view and run workflows.')).toBeInTheDocument())
    expect(screen.queryByText('Weekly review')).toBeNull()
  })
})
