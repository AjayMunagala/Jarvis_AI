import { afterEach, describe, expect, it } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import VoiceControls, { type LatestReply } from './VoiceControls'
import type { ListenHandlers, SpeakHandlers, SpeechProvider } from './speech'

class MockSpeechProvider implements SpeechProvider {
  name = 'mock'
  listeners: ListenHandlers[] = []
  spoken: string[] = []
  speakHandlers: SpeakHandlers[] = []
  stopped = 0
  aborted = 0
  cancelled = 0
  constructor(public recognitionSupported = true, public synthesisSupported = true) {}
  listen(_lang: string, handlers: ListenHandlers) {
    this.listeners.push(handlers)
    return { stop: () => { this.stopped += 1; handlers.onEnd() }, abort: () => { this.aborted += 1; handlers.onEnd() } }
  }
  speak(text: string, _lang: string, handlers: SpeakHandlers) { this.spoken.push(text); this.speakHandlers.push(handlers) }
  cancelSpeech() { this.cancelled += 1 }
}

function Harness({ provider, reply = null, busy = false, initialConsent = false }: { provider: SpeechProvider; reply?: LatestReply | null; busy?: boolean; initialConsent?: boolean }) {
  const [consented, setConsented] = useState(initialConsent)
  const [readAloud, setReadAloud] = useState(false)
  const [draft, setDraft] = useState('')
  return <div className="composer">
    <p data-testid="draft">{draft}</p>
    <VoiceControls provider={provider} consented={consented} onConsent={() => setConsented(true)} readAloud={readAloud}
      onReadAloudChange={setReadAloud} onTranscript={(text) => setDraft((current) => `${current} ${text}`.trim())}
      latestReply={reply} busy={busy} lang="en-US" />
  </div>
}

afterEach(() => cleanup())

describe('VoiceControls', () => {
  it('shows the privacy disclosure before any microphone use and respects "Not now"', async () => {
    const provider = new MockSpeechProvider()
    render(<Harness provider={provider} />)
    await userEvent.click(screen.getByRole('button', { name: 'Start voice input' }))
    const dialog = screen.getByRole('dialog', { name: 'Use voice with JARVIS?' })
    expect(dialog).toHaveTextContent('audio is sent to the browser maker’s online speech service')
    expect(dialog).toHaveTextContent('JARVIS never receives or stores audio')
    expect(dialog).toHaveTextContent('nothing is sent until you press send')
    expect(provider.listeners).toHaveLength(0)
    await userEvent.click(screen.getByRole('button', { name: 'Not now' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(provider.listeners).toHaveLength(0)
    expect(screen.getByRole('status')).toHaveTextContent('Voice stays off. You can keep typing.')
  })

  it('after opt-in, places the transcript in the draft without sending it', async () => {
    const provider = new MockSpeechProvider()
    render(<Harness provider={provider} />)
    await userEvent.click(screen.getByRole('button', { name: 'Start voice input' }))
    await userEvent.click(screen.getByRole('button', { name: 'Enable voice' }))
    expect(provider.listeners).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'Stop voice input' })).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(screen.getByRole('button', { name: 'Stop voice input' }))
    expect(provider.stopped).toBe(1)
    await userEvent.click(screen.getByRole('button', { name: 'Start voice input' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    const events = provider.listeners[1]
    act(() => events.onInterim('what is'))
    expect(await screen.findByText('“what is”')).toBeInTheDocument()
    act(() => { events.onFinal('what is six times seven'); events.onEnd() })
    expect(screen.getByTestId('draft')).toHaveTextContent('what is six times seven')
    expect(screen.getByRole('status')).toHaveTextContent('Review it, then send.')
  })

  it('explains microphone errors and keeps typing available', async () => {
    const provider = new MockSpeechProvider()
    render(<Harness provider={provider} initialConsent />)
    await userEvent.click(screen.getByRole('button', { name: 'Start voice input' }))
    act(() => { provider.listeners[0].onError('not_allowed'); provider.listeners[0].onEnd() })
    expect(await screen.findByText(/Microphone access was not allowed/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Start voice input' })).toBeEnabled()
  })

  it('disables voice input in unsupported browsers and hides read-aloud without synthesis', () => {
    render(<Harness provider={new MockSpeechProvider(false, false)} />)
    expect(screen.getByRole('button', { name: 'Voice input is not supported in this browser' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Read replies aloud' })).toBeNull()
  })

  it('reads only new replies aloud, can be interrupted, and stops when a message is sent', async () => {
    const provider = new MockSpeechProvider()
    const { rerender } = render(<Harness provider={provider} reply={{ id: 'old', text: 'Earlier reply' }} />)
    await userEvent.click(screen.getByRole('button', { name: 'Read replies aloud' }))
    await userEvent.click(screen.getByRole('button', { name: 'Enable voice' }))
    expect(screen.getByRole('button', { name: 'Read replies aloud' })).toHaveAttribute('aria-pressed', 'true')
    expect(provider.spoken).toEqual([])
    rerender(<Harness provider={provider} reply={{ id: 'new', text: 'Fresh reply' }} />)
    expect(provider.spoken).toEqual(['Fresh reply'])
    await userEvent.click(screen.getByRole('button', { name: 'Stop speaking' }))
    expect(provider.cancelled).toBeGreaterThan(0)
    const before = provider.cancelled
    rerender(<Harness provider={provider} reply={{ id: 'new', text: 'Fresh reply' }} busy />)
    expect(provider.cancelled).toBeGreaterThan(before)
    expect(provider.spoken).toEqual(['Fresh reply'])
  })

  it('releases the microphone and speech on unmount', async () => {
    const provider = new MockSpeechProvider()
    const { unmount } = render(<Harness provider={provider} initialConsent />)
    await userEvent.click(screen.getByRole('button', { name: 'Start voice input' }))
    unmount()
    expect(provider.aborted).toBe(1)
    expect(provider.cancelled).toBeGreaterThan(0)
  })
})
