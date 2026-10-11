export interface ChatRequest {
  message: string
}

export interface ChatResponse {
  reply: string
  provider: string
  demo: boolean
  partial?: boolean
}

const configuredBaseUrl = import.meta.env.VITE_API_BASE_URL?.replace(/\/$/, '')
const apiBaseUrl = configuredBaseUrl || '/api'

export async function sendChatMessage(message: string): Promise<ChatResponse> {
  let response: Response
  try {
    response = await fetch(`${apiBaseUrl}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message } satisfies ChatRequest),
    })
  } catch {
    throw new Error('Could not reach JARVIS. Check that the backend is running and try again.')
  }

  let data: unknown
  try {
    data = await response.json() as unknown
  } catch {
    throw new Error('JARVIS returned an unreadable response. Please try again.')
  }

  if (!response.ok) {
    const detail = typeof data === 'object' && data !== null && 'detail' in data && typeof data.detail === 'string'
      ? data.detail
      : 'Please try again.'
    throw new Error(detail)
  }
  if (typeof data !== 'object' || data === null || !('reply' in data)
    || typeof data.reply !== 'string' || !('provider' in data) || typeof data.provider !== 'string'
    || !('demo' in data) || typeof data.demo !== 'boolean'
    || ('partial' in data && typeof data.partial !== 'boolean')) {
    throw new Error('JARVIS returned an unexpected response. Please try again.')
  }
  return data as ChatResponse
}
