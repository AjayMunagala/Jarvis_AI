import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import App from './App'
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

  it('shows a friendly error and retries the failed message', async () => {
    const user = userEvent.setup()
    sendMock.mockRejectedValueOnce(new Error('Could not reach JARVIS. Check that the backend is running and try again.'))
      .mockResolvedValueOnce({ reply: 'Recovered.', provider: 'mock', demo: true })
    render(<App />)
    await user.type(screen.getByRole('textbox', { name: /message jarvis/i }), 'Retry me{Enter}')

    expect(await screen.findByText(/could not reach jarvis/i)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /try again/i }))
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
})
