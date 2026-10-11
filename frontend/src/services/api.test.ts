import { afterEach, describe, expect, it, vi } from 'vitest'
import { createMemory, deleteMemory, listMemories, searchMemories, sendChatMessage, updateMemory } from './api'

afterEach(() => vi.unstubAllGlobals())
const page = { items: [], total: 0, limit: 20, offset: 0, has_more: false }

describe('API client', () => {
  it('posts the chat request and returns the response including memory metadata', async () => {
    const body = { reply: 'Echo', provider: 'mock', demo: true, memory_context: { outcome: 'empty', memory_ids: [] } }
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(sendChatMessage('Hello')).resolves.toEqual(body)
    expect(fetchMock).toHaveBeenCalledWith('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message: 'Hello' }) })
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
})
