import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import App from './App'
import './styles.css'
import { getMemory, listMemories, sendChatMessage } from './services/api'

vi.mock('./services/api', () => ({
  sendChatMessage: vi.fn(), listMemories: vi.fn(), searchMemories: vi.fn(),
  createMemory: vi.fn(), updateMemory: vi.fn(), deleteMemory: vi.fn(), getMemory: vi.fn(),
}))
const sendMock = vi.mocked(sendChatMessage)

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
})
