import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import CodingPanel from './CodingPanel'
import { analyzeCoding, verifyPatch, type CodingReport, type VerificationReport } from '../services/api'

vi.mock('../services/api', () => ({ analyzeCoding: vi.fn(), verifyPatch: vi.fn() }))
const analyze = vi.mocked(analyzeCoding)
const verify = vi.mocked(verifyPatch)
const report: CodingReport = {
  execution_id: 'coding-1', workflow: 'patch', interpretation: 'Propose addition', analysis_origin: 'static', provider: 'static',
  evidence: [{ id: 'E1', path: 'module.py', sha256: 'a'.repeat(64), excerpt: '<script>untrusted()</script>', excerpt_start_line: 1, truncated: false, symbols: ['total at line 1'], relationships: ['Syntactic call: total'] }],
  facts: ['E1: source inspected'], reported_observation: 'User-reported failure',
  proposal: { summary: 'Static proposal only', hypotheses: [{ explanation: 'Unverified hypothesis', evidence_ids: ['E1'] }], recommendations: ['Review changes'], risks: ['Regression possible'], missing_information: ['Reproduction needed'], changes: [] },
  patch_status: 'proposed', patches: [{ path: 'module.py', operation: 'modify', reason: 'Use addition', sha256_before: 'a'.repeat(64), sha256_after: 'b'.repeat(64), diff: '--- a/module.py\n+++ b/module.py\n@@ -1 +1 @@\n-return 1\n+return 2\n' }],
  existing_tests: ['tests/test_module.py'], verification_recommendations: ['Run focused tests'], limitations: ['No execution'], verification_status: 'not_run', repository_modified: false, execution_enabled: false,
}
const mockVerificationReport: VerificationReport = {
  execution_id: 'exec-test-1',
  plan_id: 'VP-12345678',
  overall_state: 'check_blocked',
  results: [
    {
      check_id: 'CHK-0001',
      name: 'AST syntax validation: module.py',
      scope: 'syntax',
      state: 'check_passed',
      evidence: {
        evidence_id: 'VE-0001',
        check_id: 'CHK-0001',
        check_name: 'AST syntax validation: module.py',
        profile_id: 'python_syntax',
        state: 'check_passed',
        started_at: '2026-10-17T00:00:00Z',
        completed_at: '2026-10-17T00:00:01Z',
        duration_seconds: 0.002,
        output_excerpt: 'Python AST parsed successfully.',
        output_truncated: false,
        verified_files: ['module.py'],
        provenance: 'static_analysis',
        limitations: [],
      },
      summary: 'In-memory AST validation passed.',
    },
    {
      check_id: 'CHK-0002',
      name: 'Targeted unit test: test_module.py',
      scope: 'unit',
      state: 'check_blocked',
      evidence: {
        evidence_id: 'VE-0002',
        check_id: 'CHK-0002',
        check_name: 'Targeted unit test: test_module.py',
        profile_id: 'pytest_targeted',
        state: 'check_blocked',
        started_at: '2026-10-17T00:00:00Z',
        duration_seconds: 0.0,
        output_excerpt: 'Execution disabled by server policy.',
        output_truncated: false,
        verified_files: ['tests/test_module.py'],
        provenance: 'static_analysis',
        limitations: ['Execution disabled'],
      },
      summary: 'Check blocked: verification execution is disabled.',
    },
  ],
  summary: 'Proposed patch passed syntax validation, but live test execution is blocked.',
  facts: ['module.py: verified preimage hash matches disk.'],
  limitations: ['Execution runner is disabled.'],
  execution_enabled: false,
  repository_modified: false,
}
const auth = { token: 'a'.repeat(32) }
beforeEach(() => {
  analyze.mockReset(); analyze.mockResolvedValue(report)
  verify.mockReset(); verify.mockResolvedValue(mockVerificationReport)
})
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

  it('triggers in-memory patch verification and displays verification report', async () => {
    render(<CodingPanel auth={auth} onUnlock={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('Engineering request'), { target: { value: 'Fix bug' } })
    fireEvent.submit(screen.getByRole('form', { name: 'Coding request' }))
    expect(await screen.findByRole('article', { name: 'Coding report' })).toBeInTheDocument()

    const verifyBtn = screen.getByRole('button', { name: 'Validate proposed patches' })
    expect(verifyBtn).toBeInTheDocument()
    await userEvent.click(verifyBtn)

    expect(verify).toHaveBeenCalledWith(
      {
        patches: report.patches,
        reported_observation: report.reported_observation,
      },
      auth,
    )
    expect(await screen.findByRole('region', { name: 'Verification results' })).toBeInTheDocument()
    expect(screen.getAllByText(/check_blocked/i).length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText(/In-memory AST validation passed/i)).toBeInTheDocument()
    expect(screen.getByText(/Python AST parsed successfully/i)).toBeInTheDocument()
  })
})
