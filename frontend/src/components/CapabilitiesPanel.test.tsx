import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import CapabilitiesPanel from './CapabilitiesPanel'
import { callTool, getCapabilities, getToolAudit, type CapabilitiesResponse, type CapabilityDescriptor, type ToolResult } from '../services/api'

vi.mock('../services/api', () => ({ callTool: vi.fn(), getCapabilities: vi.fn(), getToolAudit: vi.fn() }))

const auth = { token: 'test-token' }
const capability = (overrides: Partial<CapabilityDescriptor>): CapabilityDescriptor => ({
  tool_id: 'calculator', version: '1', title: 'Calculator', description: 'Evaluates bounded arithmetic.', risk: 'low', read_only: true,
  side_effects: 'none', required_permissions: ['tools.invoke', 'chat.advisory'],
  input_schema: { type: 'object', properties: { expression: { type: 'string' } }, required: ['expression'] }, output_schema: {},
  timeout_seconds: 2, max_output_bytes: 32768, available: true, availability_reason: '', invocable: true, autonomous: false, ...overrides,
})
const catalog: CapabilitiesResponse = {
  contract_version: '1', tools_enabled: true, note: 'Read-only tools only.',
  capabilities: [capability({}), capability({ tool_id: 'memory.search', title: 'Memory search', available: false, invocable: false, availability_reason: 'Memory storage requires JARVIS_PERSISTENCE_ENABLED=true.' })],
}
const result = (overrides: Partial<ToolResult>): ToolResult => ({
  contract_version: '1', call_id: 'TC-0123456789abcdef', tool_id: 'calculator', tool_version: '1', status: 'succeeded',
  output: { expression: '6*7', result: 42, result_type: 'integer' }, error: null, evidence: ['computed:calculator'],
  started_at: '2026-10-17T10:00:00Z', completed_at: '2026-10-17T10:00:00Z', duration_ms: 1.5, side_effects: 'none', retryable: false,
  output_bytes: 40, audit_event_id: 'AE-0123456789abcdef', untrusted_content: false, ...overrides,
})

afterEach(() => { cleanup(); vi.clearAllMocks() })

async function renderPanel(response = catalog) {
  vi.mocked(getCapabilities).mockResolvedValue(response)
  vi.mocked(getToolAudit).mockResolvedValue({ durable: false, retention: 'In-memory only, last 500 events; cleared on restart.', events: [] })
  render(<CapabilitiesPanel auth={auth} onUnlock={() => undefined} />)
  await screen.findByLabelText('Registered tools')
}

describe('CapabilitiesPanel', () => {
  it('requires unlocking and makes no request without a token', () => {
    render(<CapabilitiesPanel auth={{}} onUnlock={() => undefined} />)
    expect(screen.getByText('Unlock the workspace to view capabilities.')).toBeInTheDocument()
    expect(getCapabilities).not.toHaveBeenCalled()
  })

  it('lists tools with risk, permissions, limits and availability reasons', async () => {
    await renderPanel()
    expect(screen.getByLabelText('Tool runtime status')).toHaveTextContent('enabled')
    expect(screen.getAllByText('low risk')).toHaveLength(2)
    expect(screen.getAllByText(/Permissions: tools.invoke, chat.advisory · timeout 2s · max output 32 KiB/)).toHaveLength(2)
    expect(screen.getByText('Memory storage requires JARVIS_PERSISTENCE_ENABLED=true.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Not invocable' })).toBeDisabled()
    expect(screen.getByText(/Not durable/)).toBeInTheDocument()
  })

  it('shows a disabled runtime with no invocable tools', async () => {
    await renderPanel({ ...catalog, tools_enabled: false, capabilities: catalog.capabilities.map((item) => ({ ...item, invocable: false })) })
    expect(screen.getByLabelText('Tool runtime status')).toHaveTextContent('disabled')
    expect(screen.queryByRole('button', { name: 'Use tool' })).toBeNull()
  })

  it('invokes a tool with JSON arguments and shows output, evidence and audit id', async () => {
    await renderPanel()
    vi.mocked(callTool).mockResolvedValue(result({}))
    await userEvent.click(screen.getByRole('button', { name: 'Use tool' }))
    const args = screen.getByLabelText(/Arguments \(JSON/)
    fireEvent.change(args, { target: { value: '{"expression": "6*7"}' } })
    await userEvent.click(screen.getByRole('button', { name: 'Run read-only tool' }))
    expect(callTool).toHaveBeenCalledWith('calculator', { expression: '6*7' }, auth)
    expect(await screen.findByLabelText('Tool result')).toHaveTextContent('Succeeded')
    expect(screen.getByText('computed:calculator')).toBeInTheDocument()
    expect(screen.getByText(/AE-0123456789abcdef/)).toBeInTheDocument()
  })

  it('rejects invalid JSON locally', async () => {
    await renderPanel()
    await userEvent.click(screen.getByRole('button', { name: 'Use tool' }))
    fireEvent.change(screen.getByLabelText(/Arguments \(JSON/), { target: { value: '[1, 2' } })
    await userEvent.click(screen.getByRole('button', { name: 'Run read-only tool' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Arguments must be valid JSON.')
    fireEvent.change(screen.getByLabelText(/Arguments \(JSON/), { target: { value: '[1, 2]' } })
    await userEvent.click(screen.getByRole('button', { name: 'Run read-only tool' }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Arguments must be a JSON object.'))
    expect(callTool).not.toHaveBeenCalled()
  })

  it('shows refusals and untrusted-content warnings honestly', async () => {
    await renderPanel()
    vi.mocked(callTool).mockResolvedValue(result({ status: 'denied', output: null, evidence: [], error: { code: 'permission_denied', message: 'Missing permission: repo.read.' } }))
    await userEvent.click(screen.getByRole('button', { name: 'Use tool' }))
    await userEvent.click(screen.getByRole('button', { name: 'Run read-only tool' }))
    expect(await screen.findByLabelText('Tool result')).toHaveTextContent('Denied')
    expect(screen.getByRole('alert')).toHaveTextContent('permission_denied: Missing permission: repo.read.')
    vi.mocked(callTool).mockResolvedValue(result({ untrusted_content: true }))
    await userEvent.click(screen.getByRole('button', { name: 'Run read-only tool' }))
    expect(await screen.findByText('Output contains untrusted external or document content.')).toBeInTheDocument()
  })
})
