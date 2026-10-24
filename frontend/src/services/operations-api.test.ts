import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  activateEnrollment, cancelWorkflowRun, createEnrollment, createJob, createWorkflow, deleteJob, deleteWorkflow, getWorkflowRun,
  isApprovalRequired, listAdapters, listEnrollments, listJobs, listWorkflowRuns, listWorkflows, revokeEnrollment,
  runIntegrationAction, runJob, runWorkflow, updateJob,
} from './api'

const auth = { token: 'fake-test-token' }
const gated = {
  status: 'approval_required', detail: 'Approval required: approve AR-0123456789abcdef, then repeat this request.',
  approval: { approval_id: 'AR-0123456789abcdef', state: 'pending' },
}

function stub(body: unknown = {}, status = 200) {
  const fetchMock = vi.fn(async () => status === 204 ? new Response(null, { status }) : new Response(JSON.stringify(body), { status }))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}
function call(fetchMock: ReturnType<typeof stub>, index = 0) {
  const [url, init] = fetchMock.mock.calls[index] as unknown as [string, RequestInit | undefined]
  return { url, method: init?.method ?? 'GET', body: init?.body === undefined ? undefined : JSON.parse(String(init.body)), headers: new Headers(init?.headers) }
}

afterEach(() => { vi.unstubAllGlobals() })

describe('workflow API client', () => {
  it('lists definitions and runs with exact query parameters', async () => {
    const fetchMock = stub({ items: [], total: 0 })
    await listWorkflows(50, 0, auth)
    await listWorkflowRuns({ limit: 20 }, auth)
    await listWorkflowRuns({ workflowId: 'wf-1', status: 'failed', limit: 10, offset: 5 }, auth)
    expect(call(fetchMock, 0)).toMatchObject({ url: '/api/workflows/definitions?limit=50&offset=0', method: 'GET' })
    expect(call(fetchMock, 1).url).toBe('/api/workflows/runs?limit=20&offset=0')
    expect(call(fetchMock, 2).url).toBe('/api/workflows/runs?limit=10&offset=5&workflow_id=wf-1&status=failed')
    expect(call(fetchMock, 0).headers.get('authorization')).toBe('Bearer fake-test-token')
  })

  it('creates and deletes definitions', async () => {
    const fetchMock = stub({ id: 'wf-1' })
    const body = { name: 'Morning', description: null, dag_spec: { steps: [{ step_id: 'a', name: 'A', step_type: 'noop' as const }] } }
    await createWorkflow(body, auth)
    expect(call(fetchMock)).toMatchObject({ url: '/api/workflows/definitions', method: 'POST', body })
    const deleteMock = stub(undefined, 204)
    await expect(deleteWorkflow('wf/1', auth)).resolves.toBeUndefined()
    expect(call(deleteMock)).toMatchObject({ url: '/api/workflows/definitions/wf%2F1', method: 'DELETE' })
  })

  it('triggers a run, passes the approval ID and returns a 202 approval body as data', async () => {
    const fetchMock = stub(gated, 202)
    const result = await runWorkflow('wf-1', { inputs: { a: 1 } }, auth)
    expect(isApprovalRequired(result)).toBe(true)
    expect(call(fetchMock)).toMatchObject({ url: '/api/workflows/definitions/wf-1/run', method: 'POST', body: { inputs: { a: 1 } } })
    const runMock = stub({ run: { id: 'r-1', status: 'completed' }, steps: [] }, 201)
    const detail = await runWorkflow('wf-1', { inputs: {}, approval_id: 'AR-0123456789abcdef' }, auth)
    expect(isApprovalRequired(detail)).toBe(false)
    expect(call(runMock).body).toEqual({ inputs: {}, approval_id: 'AR-0123456789abcdef' })
  })

  it('reads and cancels runs', async () => {
    const fetchMock = stub({ run: { id: 'r-1' }, steps: [] })
    await getWorkflowRun('r-1', auth)
    await cancelWorkflowRun('r-1', auth)
    expect(call(fetchMock, 0)).toMatchObject({ url: '/api/workflows/runs/r-1', method: 'GET' })
    expect(call(fetchMock, 1)).toMatchObject({ url: '/api/workflows/runs/r-1/cancel', method: 'POST' })
  })

  it('surfaces sanitized server details for workflow errors', async () => {
    stub({ detail: '2 workflow runs are already active; try again later.' }, 409)
    await expect(runWorkflow('wf-1', { inputs: {} }, auth)).rejects.toThrow('2 workflow runs are already active')
  })
})

describe('scheduler API client', () => {
  it('lists, creates, updates, deletes and runs jobs with exact methods and bodies', async () => {
    const fetchMock = stub({ items: [], total: 0 })
    await listJobs(50, 0, auth)
    const job = { name: 'Briefing', schedule_type: 'cron' as const, schedule_expr: '30 1 * * *', action_type: 'daily_briefing', payload: {}, enabled: true }
    await createJob(job, auth)
    await updateJob('job-1', { enabled: false }, auth)
    await runJob('job-1', auth)
    expect(call(fetchMock, 0)).toMatchObject({ url: '/api/scheduler/jobs?limit=50&offset=0', method: 'GET' })
    expect(call(fetchMock, 1)).toMatchObject({ url: '/api/scheduler/jobs', method: 'POST', body: job })
    expect(call(fetchMock, 1).headers.get('content-type')).toBe('application/json')
    expect(call(fetchMock, 2)).toMatchObject({ url: '/api/scheduler/jobs/job-1', method: 'PATCH', body: { enabled: false } })
    expect(call(fetchMock, 3)).toMatchObject({ url: '/api/scheduler/jobs/job-1/run', method: 'POST', body: undefined })
    const deleteMock = stub(undefined, 204)
    await deleteJob('job-1', auth)
    expect(call(deleteMock)).toMatchObject({ url: '/api/scheduler/jobs/job-1', method: 'DELETE' })
  })

  it('shows the schedule validation detail', async () => {
    stub({ error: 'invalid_schedule', detail: 'A cron expression needs five fields: minute hour day-of-month month day-of-week.' }, 422)
    await expect(createJob({ name: 'x', schedule_type: 'cron', schedule_expr: '* *', action_type: 'reminder' }, auth)).rejects.toThrow('A cron expression needs five fields')
  })
})

describe('integrations API client', () => {
  it('lists adapters and enrollments and enrolls with a secret reference only', async () => {
    const fetchMock = stub([])
    await listAdapters(auth)
    await listEnrollments(50, auth)
    await createEnrollment({ adapter_id: 'mock.lamp', display_name: 'Desk lamp', secret_ref: 'env:JARVIS_INTEGRATION_LAMP' }, auth)
    expect(call(fetchMock, 0)).toMatchObject({ url: '/api/integrations/adapters', method: 'GET' })
    expect(call(fetchMock, 1).url).toBe('/api/integrations/enrollments?limit=50')
    expect(call(fetchMock, 2)).toMatchObject({ url: '/api/integrations/enrollments', method: 'POST', body: { adapter_id: 'mock.lamp', display_name: 'Desk lamp', secret_ref: 'env:JARVIS_INTEGRATION_LAMP' } })
  })

  it('activates with the approval ID as a query parameter, and revokes', async () => {
    const fetchMock = stub(gated, 202)
    expect(isApprovalRequired(await activateEnrollment('EN-0123456789abcdef', undefined, auth))).toBe(true)
    await activateEnrollment('EN-0123456789abcdef', 'AR-0123456789abcdef', auth)
    await revokeEnrollment('EN-0123456789abcdef', auth)
    expect(call(fetchMock, 0)).toMatchObject({ url: '/api/integrations/enrollments/EN-0123456789abcdef/activate', method: 'POST', body: undefined })
    expect(call(fetchMock, 1)).toMatchObject({ url: '/api/integrations/enrollments/EN-0123456789abcdef/activate?approval_id=AR-0123456789abcdef', method: 'POST' })
    expect(call(fetchMock, 2)).toMatchObject({ url: '/api/integrations/enrollments/EN-0123456789abcdef/revoke', method: 'POST' })
  })

  it('posts device actions with arguments and the approval ID', async () => {
    const fetchMock = stub({ action: 'set_power' })
    await runIntegrationAction('EN-0123456789abcdef', { action: 'set_power', arguments: { on: true }, approval_id: 'AR-0123456789abcdef' }, auth)
    expect(call(fetchMock)).toMatchObject({
      url: '/api/integrations/enrollments/EN-0123456789abcdef/actions', method: 'POST',
      body: { action: 'set_power', arguments: { on: true }, approval_id: 'AR-0123456789abcdef' },
    })
  })

  it('surfaces sanitized details and a neutral fallback for integration errors', async () => {
    stub({ detail: 'The enrollment is revoked; actions need an active enrollment.' }, 409)
    await expect(runIntegrationAction('EN-0123456789abcdef', { action: 'read_state', arguments: {} }, auth)).rejects.toThrow('actions need an active enrollment')
    stub({}, 500)
    await expect(listEnrollments(50, auth)).rejects.toThrow('JARVIS could not complete that request. Check the fields and try again.')
  })

  it('recognizes only well-formed approval-required bodies', () => {
    expect(isApprovalRequired(gated)).toBe(true)
    expect(isApprovalRequired({ status: 'approval_required' })).toBe(false)
    expect(isApprovalRequired({ run: {}, steps: [] })).toBe(false)
    expect(isApprovalRequired(null)).toBe(false)
  })

  it('updates assistant tasks, notes and reminders with PATCH (fixed in v6.7; they sent POST since v3.1)', async () => {
    const { updateTask, updateNote, updateReminder } = await import('./api')
    const fetchMock = vi.fn().mockImplementation(async () => new Response(JSON.stringify({}), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await updateTask('t1', { title: 'x' } as never, { token: 'local-token' })
    await updateNote('n1', { title: 'x' } as never, { token: 'local-token' })
    await updateReminder('r1', { title: 'x' } as never, { token: 'local-token' })
    expect(fetchMock.mock.calls.map(([url, init]) => [url, init?.method])).toEqual([
      ['/api/assistant/tasks/t1', 'PATCH'], ['/api/assistant/notes/n1', 'PATCH'], ['/api/assistant/reminders/r1', 'PATCH'],
    ])
    expect(fetchMock.mock.calls[0][1]?.body).toBe(JSON.stringify({ title: 'x' }))
  })
})
