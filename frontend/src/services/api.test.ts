import { afterEach, describe, expect, it, vi } from 'vitest'
import { createMemory, deleteMemory, listMemories, searchMemories, sendChatMessage, updateMemory } from './api'

afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); vi.resetModules() })
const page = { items: [], total: 0, limit: 20, offset: 0, has_more: false }

describe('API client', () => {
  it('posts the chat request and returns the response including memory metadata', async () => {
    const body = { reply: 'Echo', provider: 'mock', demo: true, memory_context: { outcome: 'empty', memory_ids: [] } }
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(sendChatMessage('Hello')).resolves.toEqual(body)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/chat')
    expect(init).toMatchObject({ method: 'POST', body: JSON.stringify({ message: 'Hello' }) })
    expect(new Headers(init?.headers).get('content-type')).toBe('application/json')
  })
  it('returns the safe validation detail for chat', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: 'invalid_request', detail: 'The request body is invalid.' }), { status: 422 })))
    await expect(sendChatMessage('invalid')).rejects.toThrow('The request body is invalid.')
  })
  it('lists using exact category query parameters', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(page), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await listMemories({ limit: 10, offset: 20, category: 'project' })
    expect(fetchMock.mock.calls[0][0]).toBe('/api/memories?limit=10&offset=20&category=project')
  })
  it('uses lexical search request schema', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(page), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await searchMemories('roadmap')
    expect(fetchMock).toHaveBeenCalledWith('/api/memories/search', expect.objectContaining({ method: 'POST', body: JSON.stringify({ query: 'roadmap', limit: 20, offset: 0 }) }))
  })
  it('supports explicit create, patch and delete methods', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response('{"id":"m"}', { status: 201 }))
      .mockResolvedValueOnce(new Response('{"id":"m"}', { status: 200 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchMock)
    const input = { content: 'remembered', category: null, source_reference: null }
    await createMemory(input); await updateMemory('m', input); await deleteMemory('m')
    expect(fetchMock.mock.calls.map(([url, init]) => [url, init?.method ?? 'POST'])).toEqual([
      ['/api/memories', 'POST'], ['/api/memories/m', 'PATCH'], ['/api/memories/m', 'DELETE'],
    ])
  })
  it('turns disabled persistence into a clear safe error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: 'persistence_unavailable', detail: 'internal detail' }), { status: 503 })))
    await expect(listMemories()).rejects.toThrow(/Memory storage is disabled or unavailable/)
    await expect(listMemories()).rejects.not.toThrow(/internal detail/)
  })
  it('sanitizes network and malformed-response failures', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('private socket')))
    await expect(listMemories()).rejects.toThrow(/Could not reach JARVIS/)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('null', { status: 200 })))
    await expect(sendChatMessage('hi')).rejects.toThrow('JARVIS returned an unexpected response.')
  })

  it('sends an in-memory bearer token only to the configured API origin', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ reply: 'ok', provider: 'mock', demo: true }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await sendChatMessage('hello', { token: 'never-log-this-token' })
    const [url, init] = fetchMock.mock.calls[0]
    expect(new URL(String(url), window.location.origin).origin).toBe(window.location.origin)
    expect(new Headers(init?.headers).get('authorization')).toBe('Bearer never-log-this-token')
  })

  it('clears the workspace through the unauthorized callback and sanitizes 401 errors', async () => {
    const onUnauthorized = vi.fn()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'secret server detail' }), { status: 401 })))
    await expect(listMemories({}, { token: 'private-token-value', onUnauthorized }))
      .rejects.toThrow('Workspace locked. Enter your local access token again.')
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
    await expect(listMemories({}, { token: 'private-token-value' })).rejects.not.toThrow(/private-token-value|secret server detail/)
  })

  it('does not forward a bearer token to an untrusted plain-HTTP API origin', async () => {
    vi.stubEnv('VITE_API_BASE_URL', 'http://api.example.test')
    vi.resetModules()
    const { sendChatMessage: isolatedSend } = await import('./api')
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    await expect(isolatedSend('hello', { token: 'private-token-at-least-32-characters-long' }))
      .rejects.toThrow(/Could not reach JARVIS/)
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
