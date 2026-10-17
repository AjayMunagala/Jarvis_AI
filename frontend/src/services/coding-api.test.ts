import { afterEach, describe, expect, it, vi } from 'vitest'
import { analyzeCoding } from './api'

afterEach(() => { vi.unstubAllGlobals() })
const body = { request: 'Explain module', workflow: 'explain' as const, target_paths: ['module.py'], allow_provider_context: false }
const report = { execution_id: 'run', interpretation: 'Explain module', provider: 'static', analysis_origin: 'static', facts: [], existing_tests: [], verification_recommendations: [], limitations: [], evidence: [], patches: [], proposal: { summary: 'Static', recommendations: [], risks: [], missing_information: [], hypotheses: [] }, verification_status: 'not_run', repository_modified: false, execution_enabled: false }

describe('Coding API boundary', () => {
  it('uses the existing authenticated JSON transport and validates the advisory contract', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(report), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(analyzeCoding(body, { token: 'test-token' })).resolves.toEqual(report)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/coding/analyze')
    const init = fetchMock.mock.calls[0][1]
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual(body)
    expect(new Headers(init.headers).get('authorization')).toBe('Bearer test-token')
    expect(init.redirect).toBe('error')
  })
  it.each([403, 413, 422, 502, 503, 504])('never exposes server error content for %i', async (status) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"detail":"PRIVATE SOURCE"}', { status })))
    await expect(analyzeCoding(body)).rejects.not.toThrow('PRIVATE SOURCE')
  })
  it('invokes the shared lock handler on 401', async () => {
    const lock = vi.fn()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 401 })))
    await expect(analyzeCoding(body, { token: 'token', onUnauthorized: lock })).rejects.toThrow('Workspace locked')
    expect(lock).toHaveBeenCalledOnce()
  })
  it.each([{ ...report, execution_enabled: true }, { ...report, repository_modified: true }, { ...report, evidence: [{}] }, {}])('rejects invalid or executable reports', async (value) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(value), { status: 200 })))
    await expect(analyzeCoding(body)).rejects.toThrow('unexpected coding report')
  })
})
