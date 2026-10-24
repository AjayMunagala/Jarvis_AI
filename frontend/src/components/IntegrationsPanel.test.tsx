import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import IntegrationsPanel from './IntegrationsPanel'
import type { ApprovalMemo } from './ApprovalGate'
import {
  activateEnrollment, createEnrollment, listAdapters, listEnrollments, revokeEnrollment, runIntegrationAction,
  type AdapterDescriptor, type ApprovalRequiredResponse, type EnrollmentPage, type EnrollmentView,
} from '../services/api'

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>()
  return {
    isApprovalRequired: actual.isApprovalRequired, SECRET_REF_PATTERN: actual.SECRET_REF_PATTERN,
    activateEnrollment: vi.fn(), createEnrollment: vi.fn(), listAdapters: vi.fn(), listEnrollments: vi.fn(),
    revokeEnrollment: vi.fn(), runIntegrationAction: vi.fn(),
  }
})

const auth = { token: 'fake-test-token' }
const lamp: AdapterDescriptor = {
  adapter_id: 'mock.lamp', name: 'Simulated lamp', mode: 'mock', requires_secret: false,
  actions: [
    { name: 'read_state', read_only: true, description: 'Read the simulated power and brightness.' },
    { name: 'set_power', read_only: false, description: 'Turn the simulated lamp on or off.' },
  ],
  limitations: ['Simulation only: no real device is contacted.'],
}
const view = (overrides: Partial<EnrollmentView['enrollment']> = {}, secret: EnrollmentView['secret_status'] = 'not_required'): EnrollmentView => ({
  secret_status: secret,
  enrollment: { enrollment_id: 'EN-0123456789abcdef', owner_id: 'o', adapter_id: 'mock.lamp', display_name: 'Desk lamp', secret_ref: null, state: 'active',
    created_at: '2026-10-24T07:00:00Z', activated_at: null, revoked_at: null, last_action_at: null, durable: false, ...overrides },
})
const page = (items: EnrollmentView[]): EnrollmentPage => ({ durable_storage: false, enrollments: items })
const gated: ApprovalRequiredResponse = {
  status: 'approval_required', detail: 'Approval required.',
  approval: { approval_id: 'AR-fedcba9876543210', owner_id: 'o', category: 'integration', action: 'integration.action', target: 'Desk lamp: set_power', reason: 'r',
    risk: 'medium', expected_side_effects: 'e', scope_sha256: 'a'.repeat(64), correlation_id: null, created_at: '', expires_at: '', state: 'pending',
    decided_at: null, decision_note: null, execution: 'single_use', consumed_at: null, durable: false },
}

function load(items: EnrollmentView[] = [view()]) {
  vi.mocked(listAdapters).mockResolvedValue([lamp])
  vi.mocked(listEnrollments).mockResolvedValue(page(items))
}

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('IntegrationsPanel', () => {
  it('requires unlocking and makes no request without a token', async () => {
    const onUnlock = vi.fn()
    render(<IntegrationsPanel auth={{}} onUnlock={onUnlock} />)
    await userEvent.click(screen.getByRole('button', { name: 'Unlock integrations' }))
    expect(onUnlock).toHaveBeenCalled()
    expect(listAdapters).not.toHaveBeenCalled()
    expect(listEnrollments).not.toHaveBeenCalled()
  })

  it('lists enrollments with their health and only the secret reference, never a value', async () => {
    load([view({ secret_ref: 'env:JARVIS_INTEGRATION_LAMP', state: 'pending' }, 'missing'), view({ enrollment_id: 'EN-1111111111111111', display_name: 'Old lamp', state: 'revoked', revoked_at: '2026-10-24T08:00:00Z' })])
    render(<IntegrationsPanel auth={auth} onUnlock={() => undefined} />)
    const enrollments = await screen.findByLabelText('Enrollments')
    expect(listEnrollments).toHaveBeenCalledWith(50, auth)
    expect(within(enrollments).getByText('Desk lamp')).toBeInTheDocument()
    expect(within(enrollments).getByText('pending')).toBeInTheDocument()
    expect(within(enrollments).getByText('secret reference missing')).toBeInTheDocument()
    expect(within(enrollments).getByText('env:JARVIS_INTEGRATION_LAMP')).toBeInTheDocument()
    expect(within(enrollments).getByText('Removed (1)')).toBeInTheDocument()
    expect(screen.getByText(/in memory only/)).toBeInTheDocument()
    expect(screen.getByText('Simulation only: no real device is contacted.')).toBeInTheDocument()
  })

  it('shows empty and error states', async () => {
    load([])
    render(<IntegrationsPanel auth={auth} onUnlock={() => undefined} />)
    expect(await screen.findByText('No integrations enrolled.')).toBeInTheDocument()
    cleanup()
    vi.mocked(listEnrollments).mockRejectedValue(new Error('Integrations are unavailable.'))
    render(<IntegrationsPanel auth={auth} onUnlock={() => undefined} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Integrations are unavailable.')
  })

  it('enrolls the mock lamp and rejects anything that is not an env secret reference', async () => {
    load([])
    vi.mocked(createEnrollment).mockResolvedValue(view({ state: 'pending' }))
    render(<IntegrationsPanel auth={auth} onUnlock={() => undefined} />)
    await screen.findByText('No integrations enrolled.')
    await userEvent.type(screen.getByLabelText('Display name'), 'Desk lamp')
    await userEvent.type(screen.getByLabelText('Secret reference (optional)'), 'not-a-reference')
    await userEvent.click(screen.getByRole('button', { name: 'Enroll' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('env:JARVIS_INTEGRATION_NAME')
    expect(createEnrollment).not.toHaveBeenCalled()
    await userEvent.clear(screen.getByLabelText('Secret reference (optional)'))
    await userEvent.type(screen.getByLabelText('Secret reference (optional)'), 'env:JARVIS_INTEGRATION_LAMP')
    await userEvent.click(screen.getByRole('button', { name: 'Enroll' }))
    expect(createEnrollment).toHaveBeenCalledWith({ adapter_id: 'mock.lamp', display_name: 'Desk lamp', secret_ref: 'env:JARVIS_INTEGRATION_LAMP' }, auth)
    expect(await screen.findByText(/enrolled as pending/)).toBeInTheDocument()
  })

  it('requests approval to activate and activates with the approval ID', async () => {
    load([view({ state: 'pending' })])
    const openApprovals = vi.fn()
    const memo: ApprovalMemo = new Map()
    vi.mocked(activateEnrollment).mockResolvedValueOnce({ ...gated, approval: { ...gated.approval, approval_id: 'AR-0123456789abcdef' } }).mockResolvedValueOnce(view())
    render(<IntegrationsPanel auth={auth} onUnlock={() => undefined} onOpenApprovals={openApprovals} approvalMemo={memo} />)
    await userEvent.click(await screen.findByRole('button', { name: 'Activate Desk lamp' }))
    expect(activateEnrollment).toHaveBeenCalledWith('EN-0123456789abcdef', undefined, auth)
    expect(await screen.findByText(/Approval requested for activating "Desk lamp"/)).toHaveTextContent('Nothing was executed.')
    await userEvent.click(screen.getByRole('button', { name: 'Open approvals' }))
    expect(openApprovals).toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Activate with approval' }))
    expect(activateEnrollment).toHaveBeenLastCalledWith('EN-0123456789abcdef', 'AR-0123456789abcdef', auth)
    expect(await screen.findByText('"Desk lamp" is active.')).toBeInTheDocument()
    expect(memo.size).toBe(0)
  })

  it('runs a read-only action at once and gates a state-changing one behind approval', async () => {
    load()
    vi.mocked(runIntegrationAction)
      .mockResolvedValueOnce({ enrollment_id: 'EN-0123456789abcdef', action: 'read_state', read_only: true, mode: 'mock', output: { power: 'off', brightness: 0, simulated: true }, executed_at: '2026-10-24T07:00:00Z', approval_id: null })
      .mockResolvedValueOnce(gated)
      .mockResolvedValueOnce({ enrollment_id: 'EN-0123456789abcdef', action: 'set_power', read_only: false, mode: 'mock', output: { power: 'on', brightness: 0, simulated: true }, executed_at: '2026-10-24T07:01:00Z', approval_id: 'AR-fedcba9876543210' })
    render(<IntegrationsPanel auth={auth} onUnlock={() => undefined} />)
    const form = await screen.findByRole('form', { name: 'Actions for Desk lamp' })
    await userEvent.click(within(form).getByRole('button', { name: 'Run action' }))
    expect(runIntegrationAction).toHaveBeenCalledWith('EN-0123456789abcdef', { action: 'read_state', arguments: {} }, auth)
    expect(await screen.findByText('{"power":"off","brightness":0,"simulated":true}')).toBeInTheDocument()

    await userEvent.selectOptions(within(form).getByLabelText('Action'), 'set_power')
    const args = within(form).getByLabelText('Arguments (JSON)')
    await userEvent.clear(args)
    await userEvent.click(args); await userEvent.paste('{"on": true}')
    await userEvent.click(within(form).getByRole('button', { name: 'Run action' }))
    expect(runIntegrationAction).toHaveBeenLastCalledWith('EN-0123456789abcdef', { action: 'set_power', arguments: { on: true } }, auth)
    expect(await screen.findByText(/Approval requested for set_power/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Run with approval' }))
    expect(runIntegrationAction).toHaveBeenLastCalledWith('EN-0123456789abcdef', { action: 'set_power', arguments: { on: true }, approval_id: 'AR-fedcba9876543210' }, auth)
    expect(await screen.findByText('{"power":"on","brightness":0,"simulated":true}')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Run with approval' })).toBeNull()
  })

  it('rejects action arguments that are not a JSON object and shows server errors', async () => {
    load()
    vi.mocked(runIntegrationAction).mockRejectedValue(new Error('read_state takes no arguments.'))
    render(<IntegrationsPanel auth={auth} onUnlock={() => undefined} />)
    const form = await screen.findByRole('form', { name: 'Actions for Desk lamp' })
    const args = within(form).getByLabelText('Arguments (JSON)')
    await userEvent.clear(args)
    await userEvent.type(args, 'true')
    await userEvent.click(within(form).getByRole('button', { name: 'Run action' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent('must be a JSON object')
    expect(runIntegrationAction).not.toHaveBeenCalled()
    await userEvent.clear(args)
    await userEvent.click(args); await userEvent.paste('{"x": 1}')
    await userEvent.click(within(form).getByRole('button', { name: 'Run action' }))
    expect(await screen.findByText('read_state takes no arguments.')).toBeInTheDocument()
  })

  it('removes (revokes) an enrollment only after confirmation', async () => {
    load()
    vi.mocked(revokeEnrollment).mockResolvedValue(view({ state: 'revoked' }))
    render(<IntegrationsPanel auth={auth} onUnlock={() => undefined} />)
    await userEvent.click(await screen.findByRole('button', { name: 'Remove' }))
    expect(revokeEnrollment).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Remove integration' }))
    expect(revokeEnrollment).toHaveBeenCalledWith('EN-0123456789abcdef', auth)
    expect(await screen.findByText(/was removed \(revoked\)/)).toBeInTheDocument()
  })

  it('ignores a response that arrives after the workspace was locked', async () => {
    let resolve: (value: EnrollmentPage) => void = () => undefined
    vi.mocked(listAdapters).mockResolvedValue([lamp])
    vi.mocked(listEnrollments).mockReturnValue(new Promise((done) => { resolve = done }))
    const { rerender } = render(<IntegrationsPanel auth={auth} onUnlock={() => undefined} />)
    rerender(<IntegrationsPanel auth={{}} onUnlock={() => undefined} />)
    await act(async () => { resolve(page([view()])) })
    await waitFor(() => expect(screen.getByText('Unlock the workspace to view and manage integrations.')).toBeInTheDocument())
    expect(screen.queryByText('Desk lamp')).toBeNull()
  })
})
