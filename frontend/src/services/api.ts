export interface ChatRequest { message: string; conversation_id?: string }

export interface ChatMemoryContext {
  outcome: 'success' | 'empty' | 'fallback'
  memory_ids: string[]
}

export interface ChatResponse {
  reply: string
  provider: string
  demo: boolean
  partial?: boolean
  conversation_id?: string | null
  memory_context?: ChatMemoryContext | null
}

export interface ConversationSummary {
  conversation_id: string
  created_at: string
  last_activity_at: string
  preview: string | null
}

export interface ConversationPage {
  items: ConversationSummary[]
  limit: number
  offset: number
  has_more: boolean
}

export interface ConversationHistoryMessage {
  id: string
  conversation_id: string
  sequence_number: number
  role: 'user' | 'assistant'
  content: string
  created_at: string
}

export interface ConversationHistoryPage {
  conversation_id: string
  messages: ConversationHistoryMessage[]
  has_more: boolean
  next_before_sequence: number | null
}

export interface MemoryRecord {
  id: string
  content: string
  category: string | null
  source_reference: string | null
  created_at: string
  updated_at: string
}

export interface MemoryPage {
  items: MemoryRecord[]
  total: number
  limit: number
  offset: number
  has_more: boolean
}

export interface MemoryInput {
  content: string
  category: string | null
  source_reference: string | null
}

export interface ComponentReadiness {
  application?: 'ready' | 'configured' | 'disabled' | 'unavailable'
  provider?: 'ready' | 'configured' | 'disabled' | 'unavailable'
  orchestration?: 'ready' | 'configured' | 'disabled' | 'unavailable'
  authorization?: 'ready' | 'configured' | 'disabled' | 'unavailable'
  persistence?: 'ready' | 'configured' | 'disabled' | 'unavailable'
  repository_intelligence?: 'ready' | 'configured' | 'disabled' | 'unavailable'
  [key: string]: 'ready' | 'configured' | 'disabled' | 'unavailable' | undefined
}

export interface ReadinessResponse {
  status: 'ready' | 'not_ready'
  version: string
  components: ComponentReadiness
}

const configuredBaseUrl = import.meta.env.VITE_API_BASE_URL?.replace(/\/$/, '')
const apiBaseUrl = configuredBaseUrl || '/api'

export interface ApiAuth {
  token?: string
  onUnauthorized?: () => void
  signal?: AbortSignal
}

async function request<T>(path: string, init?: RequestInit, allowChatValidation = false, auth: ApiAuth = {}): Promise<T> {
  let response: Response
  let requestUrl: URL
  try {
    requestUrl = new URL(`${apiBaseUrl}${path}`, window.location.origin)
    if (!['http:', 'https:'].includes(requestUrl.protocol)) throw new Error('Unsupported API protocol')
    const configuredUrl = new URL(apiBaseUrl, window.location.origin)
    if (configuredUrl.username || configuredUrl.password) throw new Error('API URLs must not contain credentials')
    const headers = new Headers(init?.headers)
    if (auth.token) {
      const loopbackHosts = new Set(['localhost', '127.0.0.1', '[::1]'])
      if (requestUrl.origin !== configuredUrl.origin
        || (configuredUrl.protocol === 'http:' && !loopbackHosts.has(configuredUrl.hostname))) {
        throw new Error('API origin validation failed')
      }
      headers.set('Authorization', `Bearer ${auth.token}`)
    }
    response = await fetch(configuredBaseUrl ? requestUrl.href : `${apiBaseUrl}${path}`, {
      ...init, headers, ...(auth.signal ? { signal: auth.signal } : {}), ...(auth.token ? { redirect: 'error' as RequestRedirect } : {}),
    })
  } catch {
    throw new Error('Could not reach JARVIS. Check that the backend is running and try again.')
  }
  if (response.status === 401) {
    auth.onUnauthorized?.()
    throw new Error('Workspace locked. Enter your local access token again.')
  }
  if (response.status === 204) return undefined as T
  if (!response.ok) {
    if (response.status === 503) {
      if (path === '/ready' || path.startsWith('/ready')) {
        try {
          const data = await response.clone().json() as ReadinessResponse
          if (data && (data.status === 'not_ready' || data.status === 'ready')) {
            return data as T
          }
        } catch {
          // fall through to error message
        }
        throw new Error('JARVIS services are not ready. Check backend status and try again.')
      }
      if (path.startsWith('/conversations')) {
        throw new Error('Saved conversations are disabled or unavailable. Enable local PostgreSQL persistence to use conversation history.')
      }
      if (path.startsWith('/memories')) {
        throw new Error('Memory storage is disabled or unavailable. Enable local PostgreSQL persistence to manage saved memories.')
      }
      if (path.startsWith('/chat')) {
        throw new Error('Chat service is temporarily unavailable. Check that the provider and local access are configured and try again.')
      }
      if (path.startsWith('/repo')) {
        throw new Error('Repository intelligence service is temporarily unavailable or not configured.')
      }
      throw new Error('That service is temporarily unavailable. Please try again later.')
    }
    if (response.status === 404) {
      if (path.startsWith('/conversations/')) throw new Error('That conversation is no longer available. Refresh the list and try again.')
      throw new Error('That memory is no longer available. Refresh the list and try again.')
    }
    if (response.status === 413) throw new Error('The document exceeds the 16 KiB upload limit.')
    if (response.status === 422) {
      if (allowChatValidation) {
        try {
          const data = await response.clone().json() as { detail?: unknown }
          if (data.detail === 'The request body is invalid.' || data.detail === 'Message exceeds the configured size limit.') throw new Error(data.detail)
        } catch (cause) {
          if (cause instanceof Error && (cause.message === 'The request body is invalid.' || cause.message === 'Message exceeds the configured size limit.')) throw cause
        }
      }
      throw new Error('The request is invalid. Check the fields and try again.')
    }
    throw new Error('JARVIS could not complete that request. Please try again.')
  }
  try {
    return await response.json() as T
  } catch {
    throw new Error('JARVIS returned an unreadable response. Please try again.')
  }
}

const json = (body: unknown): RequestInit => ({
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
})

export function sendChatMessage(message: string, auth?: ApiAuth, conversationId?: string): Promise<ChatResponse> {
  const body: ChatRequest = conversationId ? { message, conversation_id: conversationId } : { message }
  return request<ChatResponse>('/chat', json(body), true, auth).then((data) => {
    if (!data || typeof data.reply !== 'string' || typeof data.provider !== 'string' || typeof data.demo !== 'boolean') {
      throw new Error('JARVIS returned an unexpected response. Please try again.')
    }
    return data
  })
}

export function listConversations(options: { limit?: number; offset?: number } = {}, auth?: ApiAuth): Promise<ConversationPage> {
  const query = new URLSearchParams()
  query.set('limit', String(options.limit ?? 20))
  query.set('offset', String(options.offset ?? 0))
  return request<ConversationPage>(`/conversations?${query.toString()}`, undefined, false, auth)
}

export function getConversationMessages(
  conversationId: string,
  options: { limit?: number; beforeSequence?: number } = {},
  auth?: ApiAuth,
): Promise<ConversationHistoryPage> {
  const query = new URLSearchParams()
  query.set('limit', String(options.limit ?? 100))
  if (options.beforeSequence !== undefined) query.set('before_sequence', String(options.beforeSequence))
  return request<ConversationHistoryPage>(`/conversations/${encodeURIComponent(conversationId)}/messages?${query.toString()}`, undefined, false, auth)
}

export function listMemories(options: { limit?: number; offset?: number; category?: string } = {}, auth?: ApiAuth): Promise<MemoryPage> {
  const query = new URLSearchParams()
  query.set('limit', String(options.limit ?? 20))
  query.set('offset', String(options.offset ?? 0))
  if (options.category) query.set('category', options.category)
  return request<MemoryPage>(`/memories?${query.toString()}`, undefined, false, auth)
}

export function searchMemories(query: string, options: { limit?: number; offset?: number } = {}, auth?: ApiAuth): Promise<MemoryPage> {
  return request<MemoryPage>('/memories/search', json({ query, limit: options.limit ?? 20, offset: options.offset ?? 0 }), false, auth)
}

export function createMemory(input: MemoryInput, auth?: ApiAuth): Promise<MemoryRecord> {
  return request<MemoryRecord>('/memories', json(input), false, auth)
}

export function ingestDocument(file: File, auth?: ApiAuth): Promise<MemoryRecord> {
  return request<MemoryRecord>('/memories/ingest', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/octet-stream',
      // Keep arbitrary Unicode filenames valid in an HTTP header. The server
      // decodes this value, strips any path, and persists only the basename.
      'X-Document-Filename': encodeURIComponent(file.name),
    },
    body: file,
  }, false, auth)
}

export function updateMemory(id: string, input: Partial<MemoryInput>, auth?: ApiAuth): Promise<MemoryRecord> {
  return request<MemoryRecord>(`/memories/${encodeURIComponent(id)}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input),
  }, false, auth)
}

export function deleteMemory(id: string, auth?: ApiAuth): Promise<void> {
  return request<void>(`/memories/${encodeURIComponent(id)}`, { method: 'DELETE' }, false, auth)
}

export function getMemory(id: string, auth?: ApiAuth): Promise<MemoryRecord> {
  return request<MemoryRecord>(`/memories/${encodeURIComponent(id)}`, undefined, false, auth)
}

export function getReadiness(auth?: ApiAuth): Promise<ReadinessResponse> {
  return request<ReadinessResponse>('/ready', undefined, false, auth)
}

export function getRepoInventory(auth?: ApiAuth): Promise<unknown> {
  return request<unknown>('/repo/inventory', undefined, false, auth)
}
