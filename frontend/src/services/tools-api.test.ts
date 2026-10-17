import { afterEach, describe, expect, it, vi } from 'vitest'
import { callTool, getCapabilities, getToolAudit } from './api'

afterEach(() => { vi.unstubAllGlobals() })

describe('tool API client', () => {
  it('posts tool calls with the bearer token and server-assigned ids only', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ status: 'succeeded' }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await callTool('calculator', { expression: '1+1' }, { token: 'tok' })
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toContain('/tools/calls')
    expect(JSON.parse(String(init.body))).toEqual({ tool_id: 'calculator', arguments: { expression: '1+1' } })
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer tok')
  })

  it('reads capabilities and bounded audit pages', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ capabilities: [] }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await getCapabilities({ token: 'tok' })
    await getToolAudit(25, { token: 'tok' })
    expect((fetchMock.mock.calls[0] as unknown as [string])[0]).toContain('/capabilities')
    expect((fetchMock.mock.calls[1] as unknown as [string])[0]).toContain('/tools/audit?limit=25')
  })

  it('surfaces sanitized server details for tool route errors', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'The operation is not permitted.' }), { status: 403 })))
    await expect(callTool('git.status', {}, { token: 'tok' })).rejects.toThrow('The operation is not permitted.')
  })
})
