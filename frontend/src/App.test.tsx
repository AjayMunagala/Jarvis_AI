import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import App from './App'
import './styles.css'
import { getConversationMessages, getInterfaceConfig, getMemory, getNotifications, getReadiness, listConversations, listMemories, sendChatMessage, type ConversationPage, type ReadinessResponse } from './services/api'

vi.mock('./services/api', () => ({
  sendChatMessage: vi.fn(), listConversations: vi.fn(), getConversationMessages: vi.fn(), listMemories: vi.fn(), searchMemories: vi.fn(),
  createMemory: vi.fn(), updateMemory: vi.fn(), deleteMemory: vi.fn(), getMemory: vi.fn(), getReadiness: vi.fn(), analyzeCoding: vi.fn(), getInterfaceConfig: vi.fn(),
  getNotifications: vi.fn(), markNotificationRead: vi.fn(),
}))
const sendMock = vi.mocked(sendChatMessage)
const listConversationsMock = vi.mocked(listConversations)
const historyMock = vi.mocked(getConversationMessages)
const getReadinessMock = vi.mocked(getReadiness)

const emptyConversationPage = { items: [], limit: 20, offset: 0, has_more: false }

async function unlockWithToken(token = 'a'.repeat(32)) {
  vi.mocked(listMemories).mockImplementation(async (_options, auth) => {
    auth?.onUnauthorized?.()
    throw new Error('Workspace locked. Enter your local access token again.')
  })
  await userEvent.click(screen.getByRole('button', { name: 'Memory' }))
  await userEvent.type(await screen.findByLabelText('Local access token'), token)
  await userEvent.click(screen.getByRole('button', { name: 'Unlock' }))
  await waitFor(() => expect(listConversationsMock).toHaveBeenCalled())
  await userEvent.click(screen.getByRole('button', { name: /chat/i }))
}

beforeEach(() => {
  listConversationsMock.mockResolvedValue(emptyConversationPage)
  historyMock.mockResolvedValue({ conversation_id: 'conversation-1', messages: [], has_more: false, next_before_sequence: null })
  getReadinessMock.mockResolvedValue({
    status: 'ready',
    version: '2.0.0',
    components: { application: 'ready', provider: 'ready', orchestration: 'ready', authorization: 'ready', persistence: 'disabled', repository_intelligence: 'ready' },
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('JARVIS chat', () => {
  it('renders a provider-neutral welcome and prompt suggestions before the first response', () => {
    render(<App />)
    expect(screen.getByRole('heading', { name: /good to have you here, ajay/i })).toBeInTheDocument()
    expect(screen.getByText(/PROVIDER MODE/)).toBeInTheDocument()
    expect(screen.getByText(/backend selects the response provider/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /explain a concept/i })).toBeInTheDocument()
  })

  it('keeps anonymous stateless mock chat available without requesting persisted conversations', async () => {
    sendMock.mockResolvedValue({ reply: 'Offline mock reply.', provider: 'mock', demo: true, conversation_id: null })
    render(<App />)
    await userEvent.type(screen.getByRole('textbox', { name: /message jarvis/i }), 'Hello offline{Enter}')
    expect(await screen.findByText('Offline mock reply.')).toBeInTheDocument()
    expect(sendMock).toHaveBeenCalledWith('Hello offline', expect.objectContaining({ token: undefined }))
    expect(listConversationsMock).not.toHaveBeenCalled()
  })

  it('shows conversation-list loading and renders persisted previews and activity dates', async () => {
    let resolveList!: (page: ConversationPage) => void
    listConversationsMock.mockReturnValue(new Promise((resolve) => { resolveList = resolve }))
    render(<App />)
    await unlockWithToken()
    expect(screen.getByText(/loading saved conversations/i)).toBeInTheDocument()
    await act(async () => resolveList({
      items: [{ conversation_id: 'saved-1', created_at: '2026-10-15T10:00:00Z', last_activity_at: '2026-10-15T11:00:00Z', preview: 'Saved first question' }],
      limit: 20, offset: 0, has_more: false,
    }))
    expect(await screen.findByTitle('Saved first question')).toBeInTheDocument()
    expect(screen.getByTitle('Saved first question')).toHaveTextContent('Saved first question')
  })

  it('shows the empty conversation-list state after a successful empty response', async () => {
    render(<App />)
    await unlockWithToken()
    expect(await screen.findByText('No saved conversations yet.')).toBeInTheDocument()
  })

  it('allows re-authentication from the conversation sidebar after a page reload', async () => {
    render(<App />)
    await userEvent.click(screen.getByRole('button', { name: /unlock to load saved conversations/i }))
    await userEvent.type(await screen.findByLabelText('Local access token'), 'r'.repeat(32))
    await userEvent.click(screen.getByRole('button', { name: 'Unlock' }))
    await waitFor(() => expect(listConversationsMock).toHaveBeenCalledWith(
      { limit: 20, offset: 0 }, expect.objectContaining({ token: 'r'.repeat(32) }),
    ))
    expect(await screen.findByText('No saved conversations yet.')).toBeInTheDocument()
  })

  it('shows a safe API error and allows retrying the conversation list', async () => {
    listConversationsMock.mockRejectedValueOnce(new Error('Saved conversations are disabled or unavailable. Enable local PostgreSQL persistence to use conversation history.'))
      .mockResolvedValueOnce(emptyConversationPage)
    render(<App />)
    await unlockWithToken()
    expect(await screen.findByRole('alert')).toHaveTextContent(/saved conversations are disabled or unavailable/i)
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('No saved conversations yet.')).toBeInTheDocument()
  })

  it('loads a saved conversation history and sends follow-up messages to its backend ID', async () => {
    listConversationsMock.mockResolvedValue({
      items: [{ conversation_id: 'saved-42', created_at: '2026-10-15T10:00:00Z', last_activity_at: '2026-10-15T11:00:00Z', preview: 'Original question' }],
      limit: 20, offset: 0, has_more: false,
    })
    historyMock.mockResolvedValue({
      conversation_id: 'saved-42', messages: [
        { id: 'm1', conversation_id: 'saved-42', sequence_number: 1, role: 'user', content: 'Original question', created_at: '2026-10-15T10:00:00Z' },
        { id: 'm2', conversation_id: 'saved-42', sequence_number: 2, role: 'assistant', content: 'Earlier answer', created_at: '2026-10-15T10:01:00Z' },
      ], has_more: false, next_before_sequence: null,
    })
    sendMock.mockResolvedValue({ reply: 'Follow-up answer', provider: 'mock', demo: true, conversation_id: 'saved-42' })
    render(<App />)
    await unlockWithToken()
    await userEvent.click(await screen.findByTitle('Original question'))
    expect(await screen.findByText('Earlier answer')).toBeInTheDocument()
    await userEvent.type(screen.getByRole('textbox', { name: /message jarvis/i }), 'Continue{Enter}')
    expect(await screen.findByText('Follow-up answer')).toBeInTheDocument()
    expect(sendMock).toHaveBeenCalledWith('Continue', expect.objectContaining({ token: 'a'.repeat(32) }), 'saved-42')
  })

  it('loads older history pages in original message order', async () => {
    listConversationsMock.mockResolvedValue({ items: [{ conversation_id: 'saved-1', created_at: '2026-10-15T10:00:00Z', last_activity_at: '2026-10-15T11:00:00Z', preview: 'Question' }], limit: 20, offset: 0, has_more: false })
    historyMock.mockResolvedValueOnce({ conversation_id: 'saved-1', messages: [{ id: 'm2', conversation_id: 'saved-1', sequence_number: 2, role: 'assistant', content: 'Newest answer', created_at: '2026-10-15T10:01:00Z' }], has_more: true, next_before_sequence: 2 })
      .mockResolvedValueOnce({ conversation_id: 'saved-1', messages: [{ id: 'm1', conversation_id: 'saved-1', sequence_number: 1, role: 'user', content: 'Oldest question', created_at: '2026-10-15T10:00:00Z' }], has_more: false, next_before_sequence: null })
    render(<App />)
    await unlockWithToken()
    await userEvent.click(await screen.findByTitle('Question'))
    await screen.findByText('Newest answer')
    await userEvent.click(screen.getByRole('button', { name: 'Load older messages' }))
    expect(await screen.findByText('Oldest question')).toBeInTheDocument()
    const displayed = [...document.querySelectorAll('.message-bubble')].map((node) => node.textContent)
    expect(displayed).toEqual(['Oldest question', 'Newest answer'])
  })

  it('shows a safe error when persisted history is missing or unavailable', async () => {
    listConversationsMock.mockResolvedValue({ items: [{ conversation_id: 'missing-1', created_at: '2026-10-15T10:00:00Z', last_activity_at: '2026-10-15T10:00:00Z', preview: 'Old preview' }], limit: 20, offset: 0, has_more: false })
    historyMock.mockRejectedValue(new Error('That conversation is no longer available. Refresh the list and try again.'))
    render(<App />)
    await unlockWithToken()
    await userEvent.click(await screen.findByTitle('Old preview'))
    expect(await screen.findByRole('alert')).toHaveTextContent(/conversation is no longer available/i)
    expect(screen.queryByText(/internal|sql|password/i)).not.toBeInTheDocument()
  })

  it('clears selected conversation content and token on lock', async () => {
    listConversationsMock.mockResolvedValue({ items: [{ conversation_id: 'saved-1', created_at: '2026-10-15T10:00:00Z', last_activity_at: '2026-10-15T11:00:00Z', preview: 'Sensitive preview' }], limit: 20, offset: 0, has_more: false })
    historyMock.mockResolvedValue({ conversation_id: 'saved-1', messages: [{ id: 'm1', conversation_id: 'saved-1', sequence_number: 1, role: 'user', content: 'Sensitive message body', created_at: '2026-10-15T10:00:00Z' }], has_more: false, next_before_sequence: null })
    render(<App />)
    await unlockWithToken()
    await userEvent.click(await screen.findByTitle('Sensitive preview'))
    expect(await screen.findByText('Sensitive message body')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Lock workspace' }))
    expect(screen.queryByText('Sensitive message body')).not.toBeInTheDocument()
    expect(screen.queryByTitle('Sensitive preview')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Local access token')).toHaveValue('')
    expect(listConversationsMock.mock.calls[0]?.[1]?.signal?.aborted).toBe(true)
    expect(listConversationsMock).toHaveBeenCalledTimes(1)
  })

  it('clears protected conversation data when history returns 401', async () => {
    listConversationsMock.mockResolvedValue({ items: [{ conversation_id: 'saved-1', created_at: '2026-10-15T10:00:00Z', last_activity_at: '2026-10-15T11:00:00Z', preview: 'Protected preview' }], limit: 20, offset: 0, has_more: false })
    historyMock.mockImplementation(async (_id, _options, auth) => {
      auth?.onUnauthorized?.()
      throw new Error('Workspace locked. Enter your local access token again.')
    })
    render(<App />)
    await unlockWithToken()
    await userEvent.click(await screen.findByTitle('Protected preview'))
    expect(await screen.findByLabelText('Local access token')).toHaveValue('')
    expect(screen.queryByTitle('Protected preview')).not.toBeInTheDocument()
  })

  it('requests additional conversation-list pages using the returned pagination metadata', async () => {
    listConversationsMock.mockResolvedValueOnce({ items: [{ conversation_id: 'c1', created_at: '2026-10-15T10:00:00Z', last_activity_at: '2026-10-15T10:00:00Z', preview: 'First page' }], limit: 20, offset: 0, has_more: true })
      .mockResolvedValueOnce({ items: [{ conversation_id: 'c2', created_at: '2026-10-14T10:00:00Z', last_activity_at: '2026-10-14T10:00:00Z', preview: 'Second page' }], limit: 20, offset: 20, has_more: false })
    render(<App />)
    await unlockWithToken()
    expect(await screen.findByTitle('First page')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Load more' }))
    expect(await screen.findByTitle('Second page')).toBeInTheDocument()
    expect(listConversationsMock.mock.calls[listConversationsMock.mock.calls.length - 1]?.[0]).toEqual({ limit: 20, offset: 20 })
  })

  it('sends the message and renders the provider response and history', async () => {
    const user = userEvent.setup()
    sendMock.mockResolvedValue({ reply: 'This is the returned mock reply.', provider: 'mock', demo: true })
    render(<App />)

    await user.type(screen.getByRole('textbox', { name: /message jarvis/i }), 'Hello JARVIS{Enter}')

    expect(await screen.findByText('This is the returned mock reply.')).toBeInTheDocument()
    expect(sendMock).toHaveBeenCalledWith('Hello JARVIS', expect.objectContaining({ token: undefined, onUnauthorized: expect.any(Function) }))
    expect(screen.getAllByText('DEMO · MOCK')).toHaveLength(2)
    expect(screen.getByRole('button', { name: /Hello JARVIS/ })).toBeInTheDocument()
  })

  it('labels real-provider responses as model output, not demo output', async () => {
    sendMock.mockResolvedValue({ reply: 'Model response.', provider: 'openai', demo: false })
    render(<App />)
    await userEvent.type(screen.getByRole('textbox', { name: /message jarvis/i }), 'Hello{Enter}')
    expect(await screen.findByText('Model response.')).toBeInTheDocument()
    expect(screen.getAllByText('MODEL · OPENAI')).toHaveLength(2)
    expect(screen.queryByText('DEMO · OPENAI')).not.toBeInTheDocument()
  })

  it('marks synthesized partial expert responses clearly', async () => {
    sendMock.mockResolvedValue({ reply: 'Some useful results.', provider: 'mock', demo: true, partial: true })
    render(<App />)
    await userEvent.type(screen.getByRole('textbox', { name: /message jarvis/i }), 'Analyze this{Enter}')
    expect(await screen.findByText('Some useful results.')).toBeInTheDocument()
    expect(screen.getByText('PARTIAL RESULT')).toBeInTheDocument()
  })

  it('shows a loading state and prevents duplicate submissions while pending', async () => {
    const user = userEvent.setup()
    let resolveRequest!: (value: { reply: string; provider: string; demo: boolean }) => void
    sendMock.mockReturnValue(new Promise((resolve) => { resolveRequest = resolve }))
    render(<App />)
    const input = screen.getByRole('textbox', { name: /message jarvis/i })
    await user.type(input, 'Please wait')
    await user.keyboard('{Enter}')

    expect(screen.getByRole('status', { name: /jarvis is responding/i })).toBeInTheDocument()
    expect(input).toBeDisabled()
    expect(sendMock).toHaveBeenCalledTimes(1)
    resolveRequest({ reply: 'Done.', provider: 'mock', demo: true })
    expect(await screen.findByText('Done.')).toBeInTheDocument()
  })

  it('rejects a 4,005-character Enter submission and any direct form submission', () => {
    render(<App />)
    const input = screen.getByRole('textbox', { name: /message jarvis/i })
    const oversizedMessage = 'x'.repeat(4005)
    fireEvent.change(input, { target: { value: oversizedMessage } })
    expect(input).toHaveValue(oversizedMessage)

    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter' })
    fireEvent.submit(input.closest('form')!)

    expect(sendMock).not.toHaveBeenCalled()
    expect(screen.getByRole('status')).toHaveTextContent(/limited to 4,000 characters/i)
  })

  it('accepts exactly 4,000 characters', async () => {
    const message = 'x'.repeat(4000)
    sendMock.mockResolvedValue({ reply: 'Boundary accepted.', provider: 'mock', demo: true })
    render(<App />)
    const input = screen.getByRole('textbox', { name: /message jarvis/i })
    fireEvent.change(input, { target: { value: message } })
    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter' })

    expect(sendMock).toHaveBeenCalledTimes(1)
    expect(sendMock).toHaveBeenCalledWith(message, expect.objectContaining({ token: undefined, onUnauthorized: expect.any(Function) }))
    expect(await screen.findByText('Boundary accepted.')).toBeInTheDocument()
  })

  it('synchronously guards Enter and form submissions against duplicate requests', async () => {
    let resolveRequest!: (value: { reply: string; provider: string; demo: boolean }) => void
    sendMock.mockReturnValue(new Promise((resolve) => { resolveRequest = resolve }))
    render(<App />)
    const input = screen.getByRole('textbox', { name: /message jarvis/i })
    fireEvent.change(input, { target: { value: 'Only once' } })

    act(() => {
      fireEvent.keyDown(input, { key: 'Enter', code: 'Enter' })
      fireEvent.keyDown(input, { key: 'Enter', code: 'Enter' })
      fireEvent.submit(input.closest('form')!)
    })

    expect(sendMock).toHaveBeenCalledTimes(1)
    await act(async () => resolveRequest({ reply: 'Single response.', provider: 'mock', demo: true }))
    expect(await screen.findByText('Single response.')).toBeInTheDocument()
  })

  it('shows a friendly error and retries the failed message', async () => {
    const user = userEvent.setup()
    let resolveRetry!: (value: { reply: string; provider: string; demo: boolean }) => void
    sendMock.mockRejectedValueOnce(new Error('Could not reach JARVIS. Check that the backend is running and try again.'))
      .mockReturnValueOnce(new Promise((resolve) => { resolveRetry = resolve }))
    render(<App />)
    await user.type(screen.getByRole('textbox', { name: /message jarvis/i }), 'Retry me{Enter}')

    expect(await screen.findByText(/could not reach jarvis/i)).toBeInTheDocument()
    const retryButton = screen.getByRole('button', { name: /try again/i })
    act(() => {
      fireEvent.click(retryButton)
      fireEvent.click(retryButton)
    })
    expect(sendMock).toHaveBeenCalledTimes(2)
    await act(async () => resolveRetry({ reply: 'Recovered.', provider: 'mock', demo: true }))
    expect(await screen.findByText('Recovered.')).toBeInTheDocument()
    expect(sendMock).toHaveBeenNthCalledWith(2, 'Retry me', expect.objectContaining({ token: undefined, onUnauthorized: expect.any(Function) }))
  })

  it('keeps the credential in memory, locks it explicitly, and requires re-entry after reload', async () => {
    sendMock.mockResolvedValue({ reply: 'Authenticated response.', provider: 'mock', demo: true })
    vi.mocked(listMemories).mockImplementation((_options, auth) => {
      if (!auth?.token) {
        auth?.onUnauthorized?.()
        return Promise.reject(new Error('Workspace locked. Enter your local access token again.'))
      }
      return Promise.resolve({ items: [], total: 0, limit: 20, offset: 0, has_more: false })
    })
    const { unmount } = render(<App />)
    await userEvent.click(screen.getByRole('button', { name: 'Memory' }))
    await waitFor(() => expect(vi.mocked(listMemories)).toHaveBeenCalled())
    // The protected API rejects the initial empty credential and opens the token prompt.
    await userEvent.type(screen.getByLabelText('Local access token'), 'a'.repeat(32))
    await userEvent.click(screen.getByRole('button', { name: 'Unlock' }))
    await userEvent.click(screen.getByRole('button', { name: /chat/i }))
    await userEvent.type(screen.getByRole('textbox', { name: /message jarvis/i }), 'Hello{Enter}')
    await screen.findByText('Authenticated response.')
    expect(sendMock).toHaveBeenCalledWith('Hello', expect.objectContaining({ token: 'a'.repeat(32) }))
    expect(window.localStorage.length).toBe(0)
    expect(window.sessionStorage.length).toBe(0)
    await userEvent.click(screen.getByRole('button', { name: 'Lock workspace' }))
    expect(screen.getByLabelText('Local access token')).toHaveValue('')
    unmount()

    render(<App />)
    expect(screen.queryByRole('button', { name: 'Lock workspace' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Local access token')).not.toBeInTheDocument()
    expect(window.localStorage.length).toBe(0)
    expect(window.sessionStorage.length).toBe(0)
  })

  it('clears the entered credential and shows unlock UI after chat receives 401', async () => {
    sendMock.mockImplementation(async (_message, auth) => {
      auth?.onUnauthorized?.()
      throw new Error('Workspace locked. Enter your local access token again.')
    })
    render(<App />)
    await userEvent.click(screen.getByRole('button', { name: 'Memory' }))
    await userEvent.type(screen.getByLabelText('Local access token'), 'b'.repeat(32))
    await userEvent.click(screen.getByRole('button', { name: 'Unlock' }))
    await userEvent.click(screen.getByRole('button', { name: /chat/i }))
    await userEvent.type(screen.getByRole('textbox', { name: /message jarvis/i }), 'hello{Enter}')
    expect(await screen.findByLabelText('Local access token')).toHaveValue('')
    expect(window.localStorage.length).toBe(0)
    expect(window.sessionStorage.length).toBe(0)
  })

  it('uses Shift+Enter for a newline without sending', async () => {
    const user = userEvent.setup()
    render(<App />)
    const input = screen.getByRole('textbox', { name: /message jarvis/i })
    await user.type(input, 'first{Shift>}{Enter}{/Shift}second')
    expect(sendMock).not.toHaveBeenCalled()
    expect(input).toHaveValue('first\nsecond')
  })

  it('starts a new conversation with Ctrl+K and Cmd+K and removes the listener on unmount', () => {
    const addListener = vi.spyOn(window, 'addEventListener')
    const removeListener = vi.spyOn(window, 'removeEventListener')
    const { unmount } = render(<App />)
    const input = screen.getByRole('textbox', { name: /message jarvis/i })
    fireEvent.change(input, { target: { value: 'unsent draft' } })

    expect(fireEvent.keyDown(window, { key: 'k', ctrlKey: true })).toBe(false)
    expect(input).toHaveValue('')
    expect(input).toHaveFocus()
    expect(fireEvent.keyDown(window, { key: 'k', metaKey: true })).toBe(false)
    expect(screen.getByText(/Ctrl K|⌘ K/)).toBeInTheDocument()

    const keydownListener = addListener.mock.calls.find(([type]) => type === 'keydown')?.[1]
    unmount()
    expect(keydownListener).toBeDefined()
    expect(removeListener).toHaveBeenCalledWith('keydown', keydownListener)
  })

  it('keeps long sidebar titles truncatable while short titles remain readable', async () => {
    const user = userEvent.setup()
    const title = 'L'.repeat(44)
    sendMock.mockResolvedValue({ reply: 'Title created.', provider: 'mock', demo: true })
    const { container } = render(<App />)
    const input = screen.getByRole('textbox', { name: /message jarvis/i })
    await user.type(input, 'Short title{Enter}')
    await screen.findByText('Title created.')
    await user.click(screen.getByRole('button', { name: /new conversation/i }))
    await user.type(input, `${title}{Enter}`)
    await screen.findAllByText('Title created.')

    const renderedTitles = [...container.querySelectorAll('.history-title')]
    expect(renderedTitles.map((item) => item.textContent)).toEqual([title, 'Short title'])
    for (const renderedTitle of renderedTitles) {
      expect(['0', '0px']).toContain(getComputedStyle(renderedTitle).minWidth)
      expect(getComputedStyle(renderedTitle).textOverflow).toBe('ellipsis')
      expect(getComputedStyle(renderedTitle).overflow).toBe('hidden')
    }
  })

  it('links the retrieval outcome and IDs to the current memory manager', async () => {
    vi.mocked(listMemories).mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0, has_more: false })
    vi.mocked(getMemory).mockRejectedValue(new Error('That memory is no longer available. Refresh the list and try again.'))
    sendMock.mockResolvedValue({
      reply: 'Here is a response.', provider: 'mock', demo: true,
      memory_context: { outcome: 'success', memory_ids: ['memory-123'] },
    })
    render(<App />)
    await userEvent.type(screen.getByRole('textbox', { name: /message jarvis/i }), 'Use my saved context{Enter}')
    await userEvent.click(await screen.findByRole('button', { name: /Memory context · success · 1 record/ }))
    expect(screen.getByRole('heading', { name: 'Memory' })).toBeInTheDocument()
    expect(screen.getByText(/not a historical snapshot/i)).toBeInTheDocument()
  })

  it('renders dynamic readiness indicator: ready, loading, not-ready, and recovery', async () => {
    // 1. Loading state before resolution
    let resolveReadiness!: (val: ReadinessResponse) => void
    getReadinessMock.mockReturnValueOnce(new Promise((resolve) => { resolveReadiness = resolve }))
    const { unmount } = render(<App />)
    expect(screen.getByText('Checking…')).toBeInTheDocument()
    expect(screen.getByLabelText(/System status: checking/i)).toBeInTheDocument()

    // Resolve as ready
    await act(async () => {
      resolveReadiness({
        status: 'ready',
        version: '2.0.0',
        components: { application: 'ready', provider: 'ready' },
      })
    })
    expect(await screen.findByText('System ready')).toBeInTheDocument()
    unmount()

    // 2. Not ready state
    getReadinessMock.mockResolvedValueOnce({
      status: 'not_ready',
      version: '2.0.0',
      components: { application: 'ready', provider: 'unavailable' },
    })
    const { unmount: unmountNotReady } = render(<App />)
    expect(await screen.findByText('System not ready')).toBeInTheDocument()
    expect(screen.getByLabelText(/System status: not ready/i)).toBeInTheDocument()
    unmountNotReady()

    // 3. Offline / unreachable state and recovery
    getReadinessMock.mockRejectedValueOnce(new Error('Network error'))
    const { unmount: unmountOffline } = render(<App />)
    expect(await screen.findByText('System offline')).toBeInTheDocument()
    expect(screen.getByLabelText(/System status: offline/i)).toBeInTheDocument()

    // Clicking the status badge triggers a retry
    getReadinessMock.mockResolvedValueOnce({
      status: 'ready',
      version: '2.0.0',
      components: { application: 'ready' },
    })
    await userEvent.click(screen.getByLabelText(/System status: offline/i))
    expect(await screen.findByText('System ready')).toBeInTheDocument()
    unmountOffline()
  })

  it('keeps component failures authoritative after successful chat and refreshes readiness', async () => {
    const unavailable: ReadinessResponse = {
      status: 'not_ready', version: '2.1.0',
      components: { application: 'ready', repository_intelligence: 'unavailable' },
    }
    getReadinessMock.mockResolvedValue(unavailable)
    sendMock.mockResolvedValue({ reply: 'Chat still works.', provider: 'mock', demo: true })
    render(<App />)
    expect(await screen.findByText('System not ready')).toBeInTheDocument()
    await userEvent.type(screen.getByRole('textbox', { name: /message jarvis/i }), 'Hello{Enter}')
    expect(await screen.findByText('Chat still works.')).toBeInTheDocument()
    await waitFor(() => expect(getReadinessMock).toHaveBeenCalledTimes(2))
    expect(screen.getByText('System not ready')).toBeInTheDocument()
    expect(screen.queryByText('System ready')).not.toBeInTheDocument()

    getReadinessMock.mockResolvedValue({ ...unavailable, status: 'ready', components: { application: 'ready', repository_intelligence: 'ready' } })
    await userEvent.click(screen.getByLabelText('System status: not ready'))
    expect(await screen.findByText('System ready')).toBeInTheDocument()
    expect(getReadinessMock).toHaveBeenCalledTimes(3)
  })

  it('does not promote readiness after successful conversation listing or a failed refresh', async () => {
    getReadinessMock.mockResolvedValue({
      status: 'not_ready', version: '2.1.0', components: { repository_intelligence: 'unavailable' },
    })
    render(<App />)
    expect(await screen.findByText('System not ready')).toBeInTheDocument()
    await unlockWithToken()
    expect(await screen.findByText('No saved conversations yet.')).toBeInTheDocument()
    await waitFor(() => expect(getReadinessMock).toHaveBeenCalledTimes(2))
    expect(screen.getByText('System not ready')).toBeInTheDocument()

    getReadinessMock.mockRejectedValue(new Error('Network unavailable'))
    await userEvent.click(screen.getByLabelText('System status: not ready'))
    expect(await screen.findByText('System offline')).toBeInTheDocument()
    expect(screen.queryByText('System ready')).not.toBeInTheDocument()
  })

  it('ignores an older ready response after a newer readiness check fails', async () => {
    let resolveInitial!: (value: ReadinessResponse) => void
    getReadinessMock.mockReturnValueOnce(new Promise((resolve) => { resolveInitial = resolve }))
      .mockResolvedValueOnce({ status: 'not_ready', version: '2.1.0', components: { authorization: 'unavailable' } })
    sendMock.mockResolvedValue({ reply: 'Successful chat.', provider: 'mock', demo: true })
    render(<App />)
    expect(screen.getByText('Checking…')).toBeInTheDocument()
    await userEvent.type(screen.getByRole('textbox', { name: /message jarvis/i }), 'Hello{Enter}')
    expect(await screen.findByText('System not ready')).toBeInTheDocument()
    await act(async () => resolveInitial({ status: 'ready', version: '2.1.0', components: { application: 'ready' } }))
    expect(screen.getByText('System not ready')).toBeInTheDocument()
    expect(screen.queryByText('System ready')).not.toBeInTheDocument()
  })

  it('lists the context sources supplied to the model with cited markers', async () => {
    sendMock.mockResolvedValue({
      reply: 'Use backoff [M1].', provider: 'mock', demo: true,
      context: {
        history_status: 'unavailable', retrieval_status: 'success', omitted_count: 1, total_chars: 80, unknown_citation_count: 0,
        sources: [
          { ref: 'M1', source_type: 'memory', source_id: 'm-1', label: 'decisions', role: null, timestamp: null, excerpt_chars: 40, truncated: false, cited_in_reply: true },
          { ref: 'D1', source_type: 'document', source_id: 'd-1', label: 'runbook.md', role: null, timestamp: null, excerpt_chars: 40, truncated: true, cited_in_reply: false },
        ],
      },
    })
    render(<App />)
    await userEvent.type(screen.getByRole('textbox', { name: /message jarvis/i }), 'How do retries work?{Enter}')
    const sources = await screen.findByLabelText('Context supplied to the model')
    expect(sources).toHaveTextContent('M1 memory · decisions (cited), D1 document · runbook.md')
    expect(sources).toHaveTextContent('history unavailable')
    expect(sources).toHaveTextContent('1 omitted for size')
  })

  it('shows tool calls with honest statuses and the independent verification result', async () => {
    sendMock.mockResolvedValue({
      reply: 'The result is 42 [T1].', provider: 'scripted', demo: false,
      tool_activity: {
        proposer: 'model', rounds_used: 1, stopped_reason: 'completed', limitations: [],
        calls: [
          { ref: 'T1', round: 1, proposer: 'model', tool_id: 'calculator', purpose: 'compute', status: 'succeeded', error_code: null, executed: true, evidence: ['computed:calculator'], duration_ms: 2, call_id: 'TC-0123456789abcdef', audit_event_id: 'AE-0123456789abcdef', untrusted_content: false },
          { ref: 'T2', round: 1, proposer: 'model', tool_id: 'shell.exec', purpose: 'x', status: 'denied', error_code: 'not_offered', executed: false, evidence: [], duration_ms: 0, call_id: null, audit_event_id: null, untrusted_content: false },
        ],
        verification: { status: 'partially_verified', checks: [], cited_refs: ['T1'], unknown_citation_count: 0, summary: 'Some tool results are cited correctly; others are uncited or failed.' },
      },
    })
    render(<App />)
    await userEvent.type(screen.getByRole('textbox', { name: /message jarvis/i }), 'compute 6 times 7{Enter}')
    const tools = await screen.findByLabelText('Tools used for this answer')
    expect(tools).toHaveTextContent('model-proposed, validated by JARVIS')
    expect(tools).toHaveTextContent('T1 calculator · succeeded, T2 shell.exec · denied (not_offered)')
    expect(tools).toHaveTextContent('Verification: partially verified')
  })
})

describe('JARVIS unified interface and voice (v3.0)', () => {
  const config = (voice: boolean, features: Partial<{ browser: boolean; tools: boolean; memory: boolean }> = {}) => ({
    contract_version: '1' as const,
    features: { chat: true, memory: false, coding: true, browser: false, tools: false, chat_tools: false, activity: true, approval_execution: false as const, ...features },
    voice: { enabled: voice, mode: 'browser_native' as const, server_receives_audio: false as const, auto_send: false as const, requires_opt_in: true as const },
  })

  it('hides voice controls when the interface configuration is unavailable', async () => {
    vi.mocked(getInterfaceConfig).mockRejectedValue(new Error('offline'))
    render(<App />)
    await waitFor(() => expect(getInterfaceConfig).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: /voice input/i })).toBeNull()
    expect(screen.getByRole('textbox', { name: /message jarvis/i })).toBeEnabled()
  })

  it('offers opt-in voice controls when enabled and keeps typing available', async () => {
    vi.mocked(getInterfaceConfig).mockResolvedValue(config(true))
    render(<App />)
    const mic = await screen.findByRole('button', { name: /voice input/i })
    // jsdom has no Web Speech API, so the control explains it is unsupported.
    expect(mic).toHaveAccessibleName('Voice input is not supported in this browser')
    expect(mic).toBeDisabled()
    await userEvent.type(screen.getByRole('textbox', { name: /message jarvis/i }), 'typed still works')
    expect(screen.getByRole('button', { name: 'Send message' })).toBeEnabled()
    expect(sendMock).not.toHaveBeenCalled()
  })

  it('does not offer voice when the server disables it and marks disabled views', async () => {
    vi.mocked(getInterfaceConfig).mockResolvedValue(config(false, { browser: false, tools: true, memory: false }))
    render(<App />)
    await waitFor(() => expect(document.querySelectorAll('.nav-off')).toHaveLength(2))
    expect(screen.queryByRole('button', { name: /voice input/i })).toBeNull()
    expect(screen.getByRole('button', { name: 'Browser' })).toBeInTheDocument()
  })

  it('opens the Assistant and Notifications views, which are no longer marked as coming soon', async () => {
    render(<App />)
    expect(screen.getByRole('button', { name: 'Assistant' })).not.toHaveTextContent('SOON')
    expect(screen.getByRole('button', { name: 'Notifications' })).not.toHaveTextContent('SOON')
    await userEvent.click(screen.getByRole('button', { name: 'Assistant' }))
    expect(await screen.findByText('Unlock the workspace to view and manage personal assistant items.')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Notifications' }))
    expect(await screen.findByText('Unlock the workspace to view notifications.')).toBeInTheDocument()
    expect(getNotifications).not.toHaveBeenCalled()
  })

  it('polls unread notifications after unlock and shows only new ones on the desktop', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const shown = vi.fn()
    vi.stubGlobal('Notification', Object.assign(function (this: unknown, title: string) { shown(title) }, { permission: 'granted', requestPermission: vi.fn() }))
    const item = (id: string, title: string) => ({ id, owner_id: 'o', category: 'reminder', title, message: 'm', priority: 'normal' as const,
      read: false, delivered_at: null, created_at: '2026-10-24T07:00:00Z' })
    vi.mocked(getNotifications)
      .mockResolvedValueOnce({ items: [item('a', 'Already there')], total: 1 })
      .mockResolvedValue({ items: [item('b', 'Call mom'), item('a', 'Already there')], total: 2 })
    try {
      render(<App />)
      await unlockWithToken()
      await waitFor(() => expect(screen.getByLabelText('1 unread notifications')).toBeInTheDocument())
      expect(getNotifications).toHaveBeenCalledWith(true, 50, expect.objectContaining({ token: 'a'.repeat(32) }))
      expect(shown).not.toHaveBeenCalled()  // what was already unread at unlock is not re-announced
      await act(async () => { vi.advanceTimersByTime(30_000) })
      await waitFor(() => expect(screen.getByLabelText('2 unread notifications')).toBeInTheDocument())
      expect(shown).toHaveBeenCalledTimes(1)
      expect(shown).toHaveBeenCalledWith('Call mom')
    } finally {
      vi.useRealTimers()
      vi.unstubAllGlobals()
    }
  })
})
