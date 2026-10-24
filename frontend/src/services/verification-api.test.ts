import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  createVerificationPlan,
  getVerificationProfiles,
  verifyPatch,
  type VerificationPlan,
  type VerificationProfilesResponse,
  type VerificationReport,
} from './api'

afterEach(() => {
  vi.unstubAllGlobals()
})

const mockPlan: VerificationPlan = {
  plan_id: 'VP-12345678',
  target_paths: ['module.py'],
  checks: [
    {
      check_id: 'CHK-0001',
      name: 'Syntax AST check',
      scope: 'syntax',
      target: 'module.py',
      profile_id: 'python_syntax',
      rationale: 'Validate Python AST',
      expected_files: ['module.py'],
    },
  ],
  rationale: 'Target verification',
  execution_enabled: false,
  created_at: '2026-10-17T00:00:00Z',
}

const mockReport: VerificationReport = {
  execution_id: 'exec-1',
  plan_id: 'VP-12345678',
  overall_state: 'check_blocked',
  results: [],
  summary: 'Plan formulated; execution disabled.',
  facts: [],
  limitations: ['Execution disabled'],
  execution_enabled: false,
  repository_modified: false,
}

const mockProfiles: VerificationProfilesResponse = {
  execution_enabled: false,
  profiles: [
    {
      profile_id: 'pytest_targeted',
      name: 'Targeted Pytest',
      description: 'Run targeted pytest',
      working_directory: '.',
      allowed_target_dirs: ['tests'],
      default_timeout_seconds: 30.0,
    },
  ],
}

describe('Verification API boundary', () => {
  it('creates verification plan using authenticated transport', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(mockPlan), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    const res = await createVerificationPlan({ target_paths: ['module.py'], reason: 'Plan test' }, { token: 'auth-token' })
    expect(res).toEqual(mockPlan)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/verification/plan')
    const init = fetchMock.mock.calls[0][1]
    expect(init.method).toBe('POST')
    expect(new Headers(init.headers).get('authorization')).toBe('Bearer auth-token')
  })

  it('submits in-memory patch verification request', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(mockReport), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    const res = await verifyPatch(
      {
        patches: [
          {
            path: 'module.py',
            operation: 'modify',
            reason: 'Fix bug',
            sha256_before: 'a'.repeat(64),
            sha256_after: 'b'.repeat(64),
            diff: '--- a/module.py\n+++ b/module.py\n@@ -1 +1 @@\n-1\n+2\n',
          },
        ],
      },
      { token: 'auth-token' },
    )
    expect(res).toEqual(mockReport)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/verification/verify-patch')
  })

  it('retrieves supported execution profiles and execution capability status', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(mockProfiles), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    const res = await getVerificationProfiles({ token: 'auth-token' })
    expect(res).toEqual(mockProfiles)
    expect(res.execution_enabled).toBe(false)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/verification/profiles')
  })
})
