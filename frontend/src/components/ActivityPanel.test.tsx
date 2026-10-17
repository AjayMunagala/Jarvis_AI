import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ActivityPanel from './ActivityPanel'
import { decideApproval, getActivity, getApprovals, type ActivityPage, type ApprovalPage, type ApprovalRequest } from '../services/api'

vi.mock('../services/api', () => ({ decideApproval: vi.fn(), getActivity: vi.fn(), getApprovals: vi.fn() }))

const auth = { token: 'test-token' }
const approval = (overrides: Partial<ApprovalRequest> = {}): ApprovalRequest => ({
  approval_id: 'AR-0123456789abcdef', owner_id: 'o', category: 'browser', action: 'browser.click', target: 'https://site.example/ · Pay now',
  reason: 'Submits a form.', risk: 'consequential', expected_side_effects: 'May submit a payment.', scope_sha256: 'a'.repeat(64),
  correlation_id: null, created_at: '2026-10-17T10:00:00Z', expires_at: '2026-10-17T10:15:00Z', state: 'pending',
  decided_at: null, decision_note: null, execution: 'not_available', durable: false, ...overrides,
})
const approvals = (items: ApprovalRequest[], durable = false): ApprovalPage => ({
  durable_storage: durable, execution_note: 'Approving records your decision only.', approvals: items,
})
const activity = (durable = false): ActivityPage => ({
  durable_storage: durable, storage_note: durable ? 'PostgreSQL (durable)' : 'In-memory only; cleared on restart (persistence disabled).',
  events: [{ event_id: 'AE-0123456789abcdef', occurred_at: '2026-10-17T10:00:00Z', owner_id: 'o', category: 'tool', event_type: 'tool_call',
    decision: 'allowed', status: 'succeeded', subject: 'calculator', correlation_id: null, details: {}, durable }],
})

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('ActivityPanel', () => {
  it('requires unlocking and makes no request without a token', () => {
    render(<ActivityPanel auth={{}} onUnlock={() => undefined} />)
    expect(screen.getByText('Unlock the workspace to view activity.')).toBeInTheDocument()
    expect(getActivity).not.toHaveBeenCalled()
    expect(getApprovals).not.toHaveBeenCalled()
  })

  it('shows pending approvals, the execution note and non-durable labels', async () => {
    vi.mocked(getActivity).mockResolvedValue(activity())
    vi.mocked(getApprovals).mockResolvedValue(approvals([approval()]))
    render(<ActivityPanel auth={auth} onUnlock={() => undefined} />)
    await screen.findByLabelText('Approval requests')
    expect(screen.getByText(/Approving records your decision only/)).toHaveTextContent('in memory only, cleared on restart')
    expect(screen.getByText('consequential')).toBeInTheDocument()
    expect(screen.getByText('(not durable)')).toBeInTheDocument()
    expect(screen.getByText(/In-memory only; cleared on restart/)).toBeInTheDocument()
  })

  it('labels durable storage and has no pending requests', async () => {
    vi.mocked(getActivity).mockResolvedValue(activity(true))
    vi.mocked(getApprovals).mockResolvedValue(approvals([approval({ state: 'expired' })], true))
    render(<ActivityPanel auth={auth} onUnlock={() => undefined} />)
    await screen.findByText('No pending approval requests.')
    expect(screen.getByText(/durable \(PostgreSQL\)/)).toBeInTheDocument()
    expect(screen.queryByText('(not durable)')).toBeNull()
    expect(screen.getByText('Decided and expired (1)')).toBeInTheDocument()
  })

  it('records an approval decision and says nothing was executed', async () => {
    vi.mocked(getActivity).mockResolvedValue(activity())
    vi.mocked(getApprovals).mockResolvedValueOnce(approvals([approval()])).mockResolvedValue(approvals([approval({ state: 'approved', decided_at: '2026-10-17T10:01:00Z' })]))
    vi.mocked(decideApproval).mockResolvedValue(approval({ state: 'approved' }))
    render(<ActivityPanel auth={auth} onUnlock={() => undefined} />)
    await userEvent.click(await screen.findByRole('button', { name: 'Approve (record only)' }))
    expect(decideApproval).toHaveBeenCalledWith('AR-0123456789abcdef', true, undefined, auth)
    expect(await screen.findByText('AR-0123456789abcdef approved. Nothing was executed.')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('No pending approval requests.')).toBeInTheDocument())
  })

  it('reports rejection failures without changing state', async () => {
    vi.mocked(getActivity).mockResolvedValue(activity())
    vi.mocked(getApprovals).mockResolvedValue(approvals([approval()]))
    vi.mocked(decideApproval).mockRejectedValue(new Error('The approval request is expired; it can no longer be decided.'))
    render(<ActivityPanel auth={auth} onUnlock={() => undefined} />)
    await userEvent.click(await screen.findByRole('button', { name: 'Reject' }))
    expect(decideApproval).toHaveBeenCalledWith('AR-0123456789abcdef', false, undefined, auth)
    expect(await screen.findByRole('alert')).toHaveTextContent('can no longer be decided')
    expect(screen.getByRole('button', { name: 'Reject' })).toBeEnabled()
  })
})
