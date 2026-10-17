import { afterEach, describe, expect, it, vi } from 'vitest'
import { browserAction, createBrowserSession, getBrowserScreenshot } from './api'

afterEach(() => { vi.unstubAllGlobals() })

describe('browser API client', () => {
  it('sends authenticated JSON for session creation and actions', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ session_id: 'BS-1' }), { status: 201 }))
    vi.stubGlobal('fetch', fetchMock)
    await createBrowserSession('https://site.example/', { token: 'tok' })
    await browserAction('BS-1', { action: 'observe' }, { token: 'tok' })
    const [firstUrl, firstInit] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(firstUrl).toContain('/browser/sessions')
    expect(JSON.parse(String(firstInit.body))).toEqual({ url: 'https://site.example/' })
    expect(new Headers(firstInit.headers).get('Authorization')).toBe('Bearer tok')
    expect((fetchMock.mock.calls[1] as unknown as [string])[0]).toContain('/browser/sessions/BS-1/actions')
  })

  it('surfaces the sanitized server detail for browser errors', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ error: 'http_error', detail: 'Only http and https URLs are allowed.' }), { status: 422 })))
    await expect(createBrowserSession('file:///etc/passwd', { token: 'tok' })).rejects.toThrow('Only http and https URLs are allowed.')
  })

  it('falls back to a generic message when no detail is available', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('not json', { status: 503 })))
    await expect(browserAction('BS-1', { action: 'reload' }, { token: 'tok' })).rejects.toThrow('The browser request failed.')
  })

  it('reports locked workspace on 401 and calls the unauthorized handler', async () => {
    const onUnauthorized = vi.fn()
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 401 })))
    await expect(browserAction('BS-1', { action: 'reload' }, { token: 'tok', onUnauthorized })).rejects.toThrow('Workspace locked')
    expect(onUnauthorized).toHaveBeenCalled()
  })

  it('returns screenshots as blobs', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(new Blob(['png'], { type: 'image/png' }), { status: 200, headers: { 'Content-Type': 'image/png' } })))
    const blob = await getBrowserScreenshot('BS-1', 'SHOT-aaaaaaaaaaaa', { token: 'tok' })
    expect(blob.type).toBe('image/png')
  })
})
