import { describe, expect, it, vi } from 'vitest'
import { cleanTranscript, createBrowserSpeechProvider, MAX_SPOKEN_LENGTH, spokenText, type SpeechWindow } from './speech'

class FakeRecognition {
  static instances: FakeRecognition[] = []
  lang = ''
  continuous = true
  interimResults = false
  maxAlternatives = 5
  onresult: ((event: unknown) => void) | null = null
  onerror: ((event: { error?: string }) => void) | null = null
  onend: (() => void) | null = null
  start = vi.fn()
  stop = vi.fn(() => this.onend?.())
  abort = vi.fn(() => this.onend?.())
  constructor() { FakeRecognition.instances.push(this) }
  emit(results: Array<[string, boolean]>) {
    this.onresult?.({ resultIndex: 0, results: results.map(([transcript, isFinal]) => ({ isFinal, 0: { transcript } })) })
  }
}

function handlers() {
  return { onInterim: vi.fn(), onFinal: vi.fn(), onError: vi.fn(), onEnd: vi.fn() }
}

describe('browser speech provider', () => {
  it('reports unsupported recognition and synthesis without throwing', () => {
    const provider = createBrowserSpeechProvider({})
    expect(provider.recognitionSupported).toBe(false)
    expect(provider.synthesisSupported).toBe(false)
    const events = handlers()
    provider.listen('en-US', events).stop()
    expect(events.onError).toHaveBeenCalledWith('unsupported')
    expect(events.onEnd).toHaveBeenCalledTimes(1)
    const speak = { onEnd: vi.fn(), onError: vi.fn() }
    provider.speak('hello', 'en-US', speak)
    expect(speak.onError).toHaveBeenCalled()
    expect(() => provider.cancelSpeech()).not.toThrow()
  })

  it('configures single-utterance recognition and separates interim from final text', () => {
    FakeRecognition.instances = []
    const provider = createBrowserSpeechProvider({ webkitSpeechRecognition: FakeRecognition } as unknown as SpeechWindow)
    const events = handlers()
    const session = provider.listen('en-GB', events)
    const recognition = FakeRecognition.instances[0]
    expect(recognition).toMatchObject({ lang: 'en-GB', continuous: false, interimResults: true, maxAlternatives: 1 })
    expect(recognition.start).toHaveBeenCalled()
    recognition.emit([['what is', false]])
    expect(events.onInterim).toHaveBeenCalledWith('what is')
    recognition.emit([['what is  six\ttimes seven\u0007', true]])
    expect(events.onFinal).toHaveBeenCalledWith('what is six times seven')
    session.stop()
    session.stop()
    expect(events.onEnd).toHaveBeenCalledTimes(1)
  })

  it.each([
    ['not-allowed', 'not_allowed'], ['service-not-allowed', 'not_allowed'], ['no-speech', 'no_speech'],
    ['audio-capture', 'audio_capture'], ['network', 'network'], ['aborted', 'aborted'], ['bad-grammar', 'failed'],
  ])('maps recognition error %s to %s', (error, code) => {
    FakeRecognition.instances = []
    const provider = createBrowserSpeechProvider({ SpeechRecognition: FakeRecognition } as unknown as SpeechWindow)
    const events = handlers()
    provider.listen('en-US', events)
    FakeRecognition.instances[0].onerror?.({ error })
    expect(events.onError).toHaveBeenCalledWith(code)
  })

  it('reports a start failure and ends the session', () => {
    class Throwing extends FakeRecognition { start = vi.fn(() => { throw new Error('busy') }) }
    const provider = createBrowserSpeechProvider({ SpeechRecognition: Throwing } as unknown as SpeechWindow)
    const events = handlers()
    provider.listen('en-US', events)
    expect(events.onError).toHaveBeenCalledWith('failed')
    expect(events.onEnd).toHaveBeenCalledTimes(1)
  })

  it('speaks bounded text with a local voice and cancels earlier speech', () => {
    const spoken: Array<{ text: string; voice: unknown; lang: string }> = []
    class Utterance { lang = ''; voice: unknown = null; onend = null; onerror = null; constructor(public text: string) {} }
    const local = { lang: 'en-US', localService: true }
    const synthesis = {
      speak: vi.fn((item: Utterance) => spoken.push({ text: item.text, voice: item.voice, lang: item.lang })),
      cancel: vi.fn(),
      getVoices: () => [{ lang: 'en-US', localService: false }, local],
    }
    const provider = createBrowserSpeechProvider({ speechSynthesis: synthesis, SpeechSynthesisUtterance: Utterance } as unknown as SpeechWindow)
    expect(provider.synthesisSupported).toBe(true)
    provider.speak('x'.repeat(MAX_SPOKEN_LENGTH + 50), 'en-US', { onEnd: vi.fn(), onError: vi.fn() })
    expect(synthesis.cancel).toHaveBeenCalled()
    expect(spoken[0].voice).toBe(local)
    expect(spoken[0].text).toMatch(/The rest of the reply is on screen\.$/)
    expect(spoken[0].text.length).toBeLessThan(MAX_SPOKEN_LENGTH + 50)
  })

  it('cleans transcripts and spoken text', () => {
    expect(cleanTranscript('  a\n\nb\u0000 c ')).toBe('a b c')
    expect(cleanTranscript('y'.repeat(5000))).toHaveLength(4000)
    expect(spokenText(' short  reply ')).toBe('short reply')
  })
})
