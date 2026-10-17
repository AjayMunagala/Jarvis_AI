import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import CodingPanel from './CodingPanel'
import { analyzeCoding, type CodingReport } from '../services/api'

vi.mock('../services/api', () => ({ analyzeCoding: vi.fn() }))
const analyze = vi.mocked(analyzeCoding)
const report: CodingReport = {
  execution_id: 'coding-1', workflow: 'patch', interpretation: 'Propose addition', analysis_origin: 'static', provider: 'static',
  evidence: [{ id: 'E1', path: 'module.py', sha256: 'a'.repeat(64), excerpt: '<script>untrusted()</script>', excerpt_start_line: 1, truncated: false, symbols: ['total at line 1'], relationships: ['Syntactic call: total'] }],
  facts: ['E1: source inspected'], reported_observation: 'User-reported failure',
  proposal: { summary: 'Static proposal only', hypotheses: [{ explanation: 'Unverified hypothesis', evidence_ids: ['E1'] }], recommendations: ['Review changes'], risks: ['Regression possible'], missing_information: ['Reproduction needed'], changes: [] },
  patch_status: 'proposed', patches: [{ path: 'module.py', operation: 'modify', reason: 'Use addition', sha256_before: 'a'.repeat(64), sha256_after: 'b'.repeat(64), diff: '--- a/module.py\n+++ b/module.py\n@@ -1 +1 @@\n-return 1\n+return 2\n' }],
  existing_tests: ['tests/test_module.py'], verification_recommendations: ['Run focused tests'], limitations: ['No execution'], verification_status: 'not_run', repository_modified: false, execution_enabled: false,
}
const auth = { token: 'a'.repeat(32) }
beforeEach(() => { analyze.mockReset(); analyze.mockResolvedValue(report) })
afterEach(cleanup)

describe('Coding workspace', () => {
  it('requires unlock and shows an honest empty state', async () => {
    const unlock = vi.fn()
    render(<CodingPanel auth={{}} onUnlock={unlock} />)
    expect(screen.getByRole('button', { name: 'Analyze coding request' })).toBeDisabled()
    expect(screen.getByText(/No coding report yet/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Unlock coding workspace' }))
    expect(unlock).toHaveBeenCalledOnce()
    expect(analyze).not.toHaveBeenCalled()
  })

  it('submits bounded paths, observation and explicit provider consent and renders escaped evidence', async () => {
    render(<CodingPanel auth={auth} onUnlock={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('Engineering request'), { target: { value: 'Propose addition' } })
    fireEvent.change(screen.getByLabelText(/Repository-relative paths/), { target: { value: 'module.py\ntests/test_module.py' } })
    fireEvent.change(screen.getByLabelText(/Reported error/), { target: { value: 'Example error' } })
    await userEvent.click(screen.getByRole('checkbox'))
    await userEvent.click(screen.getByRole('button', { name: 'Analyze coding request' }))
    expect(await screen.findByRole('article', { name: 'Coding report' })).toBeInTheDocument()
    expect(analyze).toHaveBeenCalledWith(expect.objectContaining({ target_paths: ['module.py', 'tests/test_module.py'], reported_observation: 'Example error', allow_provider_context: true }), expect.objectContaining({ token: auth.token, signal: expect.any(AbortSignal) }))
    expect(screen.getByText('Review only — nothing applied')).toBeInTheDocument()
    expect(screen.getByLabelText('Proposed diff for module.py')).toHaveTextContent('return 2')
    expect(screen.getByText('<script>untrusted()</script>')).toBeInTheDocument()
    expect(document.querySelector('script')).toBeNull()
    expect(screen.getByText('Unverified hypothesis (references: E1)')).toBeInTheDocument()
    expect(screen.getByText(/Verification: not_run/)).toBeInTheDocument()
  })

  it('shows loading, prevents duplicate submits and keeps provider consent disabled by default', async () => {
    let resolve!: (value: CodingReport) => void
    analyze.mockReturnValue(new Promise((done) => { resolve = done }))
    render(<CodingPanel auth={auth} onUnlock={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('Engineering request'), { target: { value: 'Explain total' } })
    fireEvent.submit(screen.getByRole('form', { name: 'Coding request' }))
    fireEvent.submit(screen.getByRole('form', { name: 'Coding request' }))
    expect(screen.getByRole('button', { name: 'Inspecting…' })).toBeDisabled()
    expect(analyze).toHaveBeenCalledOnce()
    expect(analyze.mock.calls[0][0].allow_provider_context).toBe(false)
    await act(async () => resolve(report))
  })

  it('validates structured edits before sending and exposes errors accessibly', async () => {
    render(<CodingPanel auth={auth} onUnlock={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('Engineering request'), { target: { value: 'Patch' } })
    fireEvent.change(screen.getByLabelText('Proposed changes JSON'), { target: { value: '{bad json' } })
    fireEvent.submit(screen.getByRole('form', { name: 'Coding request' }))
    expect(screen.getByRole('alert')).toHaveTextContent('JSON array')
    expect(analyze).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('Proposed changes JSON'), { target: { value: '[]' } })
    analyze.mockRejectedValueOnce(new Error('The coding proposal could not be validated.'))
    fireEvent.submit(screen.getByRole('form', { name: 'Coding request' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('could not be validated')
  })

  it('clears sensitive report and input on credential change and ignores late responses', async () => {
    const view = render(<CodingPanel auth={auth} onUnlock={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('Engineering request'), { target: { value: 'Private request' } })
    fireEvent.submit(screen.getByRole('form', { name: 'Coding request' }))
    await screen.findByRole('article', { name: 'Coding report' })
    view.rerender(<CodingPanel auth={{}} onUnlock={vi.fn()} />)
    expect(screen.queryByRole('article', { name: 'Coding report' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('Engineering request')).toHaveValue('')
    let resolve!: (value: CodingReport) => void
    analyze.mockReturnValue(new Promise((done) => { resolve = done }))
    view.rerender(<CodingPanel auth={auth} onUnlock={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('Engineering request'), { target: { value: 'Another request' } })
    fireEvent.submit(screen.getByRole('form', { name: 'Coding request' }))
    const signal = analyze.mock.calls[analyze.mock.calls.length - 1]?.[1]?.signal
    view.rerender(<CodingPanel auth={{}} onUnlock={vi.fn()} />)
    expect(signal?.aborted).toBe(true)
    await act(async () => resolve(report))
    expect(screen.queryByRole('article', { name: 'Coding report' })).not.toBeInTheDocument()
  })

  it('does not revive a report after unmount', async () => {
    let resolve!: (value: CodingReport) => void
    analyze.mockReturnValue(new Promise((done) => { resolve = done }))
    const view = render(<CodingPanel auth={auth} onUnlock={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('Engineering request'), { target: { value: 'Explain' } })
    fireEvent.submit(screen.getByRole('form', { name: 'Coding request' }))
    const signal = analyze.mock.calls[0][1]?.signal
    view.unmount()
    expect(signal?.aborted).toBe(true)
    await act(async () => resolve(report))
  })
})
