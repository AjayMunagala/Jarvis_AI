// Speech providers for optional voice input and read-aloud (v3.0, decision D5).
//
// The browser performs recognition and synthesis. JARVIS never receives audio:
// only the text transcript the owner reviews and sends reaches the backend.
// In Chromium-based browsers, recognition audio is processed by the browser
// vendor's online speech service; the privacy disclosure says so before use.

export type VoiceErrorCode = 'not_allowed' | 'no_speech' | 'audio_capture' | 'network' | 'aborted' | 'unsupported' | 'failed'

export interface ListenHandlers {
  onInterim: (text: string) => void
  onFinal: (text: string) => void
  onError: (code: VoiceErrorCode) => void
  onEnd: () => void
}

export interface ListenSession {
  stop: () => void
  abort: () => void
}

export interface SpeakHandlers {
  onEnd: () => void
  onError: () => void
}

export interface SpeechProvider {
  readonly name: string
  readonly recognitionSupported: boolean
  readonly synthesisSupported: boolean
  listen: (lang: string, handlers: ListenHandlers) => ListenSession
  speak: (text: string, lang: string, handlers: SpeakHandlers) => void
  cancelSpeech: () => void
}

export const MAX_TRANSCRIPT_LENGTH = 4000
export const MAX_SPOKEN_LENGTH = 1500

export function cleanTranscript(text: string): string {
  // Strip control characters and collapse whitespace; the result is plain text for the composer.
  return text.replace(/[\u0000-\u001f\u007f]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, MAX_TRANSCRIPT_LENGTH)
}

export function spokenText(text: string): string {
  const clean = text.replace(/\s+/g, ' ').trim()
  return clean.length > MAX_SPOKEN_LENGTH ? `${clean.slice(0, MAX_SPOKEN_LENGTH)}… The rest of the reply is on screen.` : clean
}

function errorCode(error: string | undefined): VoiceErrorCode {
  switch (error) {
    case 'not-allowed':
    case 'service-not-allowed':
      return 'not_allowed'
    case 'no-speech':
      return 'no_speech'
    case 'audio-capture':
      return 'audio_capture'
    case 'network':
      return 'network'
    case 'aborted':
      return 'aborted'
    default:
      return 'failed'
  }
}

// Minimal structural types: the Web Speech API is not in every TypeScript DOM lib.
interface RecognitionResultLike { isFinal: boolean; 0: { transcript: string } }
interface RecognitionEventLike { resultIndex: number; results: ArrayLike<RecognitionResultLike> }
interface RecognitionLike {
  lang: string
  continuous: boolean
  interimResults: boolean
  maxAlternatives: number
  onresult: ((event: RecognitionEventLike) => void) | null
  onerror: ((event: { error?: string }) => void) | null
  onend: (() => void) | null
  start: () => void
  stop: () => void
  abort: () => void
}
type RecognitionConstructor = new () => RecognitionLike
interface VoiceLike { lang: string; localService: boolean }
interface UtteranceLike { lang: string; voice: unknown; onend: (() => void) | null; onerror: (() => void) | null }
interface SynthesisLike { speak: (utterance: UtteranceLike) => void; cancel: () => void; getVoices: () => VoiceLike[] }
type UtteranceConstructor = new (text: string) => UtteranceLike

export interface SpeechWindow {
  SpeechRecognition?: RecognitionConstructor
  webkitSpeechRecognition?: RecognitionConstructor
  speechSynthesis?: SynthesisLike
  SpeechSynthesisUtterance?: UtteranceConstructor
}

export function createBrowserSpeechProvider(target: SpeechWindow = window as unknown as SpeechWindow): SpeechProvider {
  const Recognition = target.SpeechRecognition ?? target.webkitSpeechRecognition
  const synthesis = target.speechSynthesis
  const Utterance = target.SpeechSynthesisUtterance

  return {
    name: 'browser',
    recognitionSupported: typeof Recognition === 'function',
    synthesisSupported: Boolean(synthesis && typeof Utterance === 'function'),

    listen(lang, handlers) {
      if (typeof Recognition !== 'function') {
        handlers.onError('unsupported')
        handlers.onEnd()
        return { stop() {}, abort() {} }
      }
      let recognition: RecognitionLike
      try {
        recognition = new Recognition()
      } catch {
        handlers.onError('unsupported')
        handlers.onEnd()
        return { stop() {}, abort() {} }
      }
      let ended = false
      recognition.lang = lang
      recognition.continuous = false
      recognition.interimResults = true
      recognition.maxAlternatives = 1
      recognition.onresult = (event) => {
        let interim = ''
        let final = ''
        for (let index = event.resultIndex; index < event.results.length; index += 1) {
          const result = event.results[index]
          if (result.isFinal) final += result[0].transcript
          else interim += result[0].transcript
        }
        if (final.trim()) handlers.onFinal(cleanTranscript(final))
        else if (interim.trim()) handlers.onInterim(cleanTranscript(interim))
      }
      recognition.onerror = (event) => handlers.onError(errorCode(event.error))
      recognition.onend = () => {
        if (ended) return
        ended = true
        handlers.onEnd()
      }
      try {
        // The browser asks for microphone permission here; JARVIS never requests it directly.
        recognition.start()
      } catch {
        handlers.onError('failed')
        ended = true
        handlers.onEnd()
      }
      return {
        stop: () => { try { recognition.stop() } catch { /* already stopped */ } },
        abort: () => { try { recognition.abort() } catch { /* already stopped */ } },
      }
    },

    speak(text, lang, handlers) {
      if (!synthesis || typeof Utterance !== 'function') {
        handlers.onError()
        return
      }
      try {
        synthesis.cancel()
        const utterance = new Utterance(spokenText(text))
        utterance.lang = lang
        // Prefer a voice installed on this device so the reply text stays local.
        const prefix = lang.split('-')[0]
        const local = synthesis.getVoices().find((voice) => voice.localService && voice.lang.startsWith(prefix))
        if (local) utterance.voice = local
        utterance.onend = handlers.onEnd
        utterance.onerror = handlers.onError
        synthesis.speak(utterance)
      } catch {
        handlers.onError()
      }
    },

    cancelSpeech() {
      try { synthesis?.cancel() } catch { /* nothing to cancel */ }
    },
  }
}
