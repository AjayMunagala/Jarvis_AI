import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import App from './App'
import './styles.css'
import { sendChatMessage } from './services/api'

vi.mock('./services/api', () => ({ sendChatMessage: vi.fn() }))
const sendMock = vi.mocked(sendChatMessage)

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('JARVIS chat', () => {
  it('renders an honest demo-mode welcome and prompt suggestions', () => {
    render(<App />)
    expect(screen.getByRole('heading', { name: /good to have you here, ajay/i })).toBeInTheDocument()
    expect(screen.getByText(/DEMO MODE/)).toBeInTheDocument()
    expect(screen.getByText(/local mock provider/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /explain a concept/i })).toBeInTheDocument()
  })

  it('sends the message and renders the provider response and history', async () => {
    const user = userEvent.setup()
    sendMock.mockResolvedValue({ reply: 'This is the returned mock reply.', provider: 'mock', demo: true })
    render(<App />)

    await user.type(screen.getByRole('textbox', { name: /message jarvis/i }), 'Hello JARVIS{Enter}')

    expect(await screen.findByText('This is the returned mock reply.')).toBeInTheDocument()
    expect(sendMock).toHaveBeenCalledWith('Hello JARVIS')
    expect(screen.getByText('DEMO · MOCK')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Hello JARVIS/ })).toBeInTheDocument()
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
    expect(sendMock).toHaveBeenCalledWith(message)
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
    expect(sendMock).toHaveBeenNthCalledWith(2, 'Retry me')
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
})
