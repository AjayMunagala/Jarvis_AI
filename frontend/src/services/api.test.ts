import { afterEach, describe, expect, it, vi } from 'vitest'
import { sendChatMessage } from './api'

afterEach(() => vi.unstubAllGlobals())

describe('chat API client', () => {
  it('posts the backend request schema and returns the actual response', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ reply: 'Echo', provider: 'mock', demo: true }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(sendChatMessage('Hello')).resolves.toEqual({ reply: 'Echo', provider: 'mock', demo: true })
    expect(fetchMock).toHaveBeenCalledWith('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: 'Hello' }),
    })
  })

  it('surfaces a sanitized backend validation error detail', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: 'invalid_request', detail: 'The request body is invalid.' }), {
      status: 422,
      headers: { 'Content-Type': 'application/json' },
    })))
    await expect(sendChatMessage('invalid')).rejects.toThrow('The request body is invalid.')
  })

  it('converts network errors into a friendly connection message', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('socket details')))
    await expect(sendChatMessage('hello')).rejects.toThrow('Could not reach JARVIS. Check that the backend is running and try again.')
  })

  it('rejects malformed success payloads', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('null', {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })))
    await expect(sendChatMessage('hello')).rejects.toThrow('JARVIS returned an unexpected response.')
  })
})
