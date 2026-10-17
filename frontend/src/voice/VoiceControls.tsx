import { useEffect, useRef, useState } from 'react'
import { Mic, MicOff, Square, Volume2, VolumeX } from 'lucide-react'
import type { SpeechProvider, VoiceErrorCode } from './speech'
import './VoiceControls.css'

const LISTEN_LIMIT_MS = 30_000

const ERROR_MESSAGES: Record<VoiceErrorCode, string> = {
  not_allowed: 'Microphone access was not allowed. You can change this in your browser settings, or keep typing.',
  no_speech: 'No speech was detected. Try again, or type your message.',
  audio_capture: 'No microphone was found. You can keep typing.',
  network: 'The browser speech service could not be reached. You can keep typing.',
  aborted: 'Voice input stopped.',
  unsupported: 'Voice input is not supported in this browser. You can keep typing.',
  failed: 'Voice input failed. You can keep typing.',
}

export interface LatestReply { id: string; text: string }

export interface VoiceControlsProps {
  provider: SpeechProvider
  consented: boolean
  onConsent: () => void
  readAloud: boolean
  onReadAloudChange: (enabled: boolean) => void
  onTranscript: (text: string) => void
  latestReply: LatestReply | null
  busy: boolean
  lang?: string
}

export default function VoiceControls({ provider, consented, onConsent, readAloud, onReadAloudChange, onTranscript, latestReply, busy, lang }: VoiceControlsProps) {
  const language = lang ?? (typeof navigator !== 'undefined' && navigator.language ? navigator.language : 'en-US')
  const [disclosureOpen, setDisclosureOpen] = useState(false)
  const [listening, setListening] = useState(false)
  const [speaking, setSpeaking] = useState(false)
  const [interim, setInterim] = useState('')
  const [status, setStatus] = useState('')
  const sessionRef = useRef<{ stop: () => void; abort: () => void } | null>(null)
  const timerRef = useRef<number | undefined>(undefined)
  const pendingRef = useRef<'listen' | 'read' | null>(null)
  const spokenRef = useRef<string | null>(latestReply?.id ?? null)
  const onTranscriptRef = useRef(onTranscript)
  onTranscriptRef.current = onTranscript

  function clearTimer() {
    if (timerRef.current !== undefined) window.clearTimeout(timerRef.current)
    timerRef.current = undefined
  }

  function stopSpeaking() {
    provider.cancelSpeech()
    setSpeaking(false)
  }

  function startListening() {
    if (!provider.recognitionSupported || listening) return
    stopSpeaking() // interrupt read-aloud when the owner starts talking
    setStatus('Listening… Your words will be placed in the message box for you to review before sending.')
    setInterim('')
    setListening(true)
    sessionRef.current = provider.listen(language, {
      onInterim: (text) => setInterim(text),
      onFinal: (text) => {
        setInterim('')
        if (text) {
          onTranscriptRef.current(text)
          setStatus('Transcript added to the message box. Review it, then send.')
        }
      },
      onError: (code) => setStatus(ERROR_MESSAGES[code]),
      onEnd: () => {
        clearTimer()
        sessionRef.current = null
        setListening(false)
        setInterim('')
      },
    })
    timerRef.current = window.setTimeout(() => sessionRef.current?.stop(), LISTEN_LIMIT_MS)
  }

  function stopListening() {
    clearTimer()
    sessionRef.current?.stop()
  }

  function handleMic() {
    if (listening) { stopListening(); return }
    if (!consented) { pendingRef.current = 'listen'; setDisclosureOpen(true); return }
    startListening()
  }

  function toggleReadAloud() {
    if (readAloud) stopSpeaking()
    else spokenRef.current = latestReply?.id ?? null // only replies that arrive after enabling are read
    if (!readAloud && !consented) { pendingRef.current = 'read'; setDisclosureOpen(true); return }
    onReadAloudChange(!readAloud)
  }

  function acceptDisclosure() {
    const next = pendingRef.current
    pendingRef.current = null
    setDisclosureOpen(false)
    onConsent()
    if (next === 'listen') startListening()
    if (next === 'read') onReadAloudChange(true)
  }

  function declineDisclosure() {
    pendingRef.current = null
    setDisclosureOpen(false)
    setStatus('Voice stays off. You can keep typing.')
  }

  // Read new replies aloud when enabled.
  useEffect(() => {
    if (!readAloud || !consented || !latestReply || latestReply.id === spokenRef.current) return
    spokenRef.current = latestReply.id
    setSpeaking(true)
    provider.speak(latestReply.text, language, {
      onEnd: () => setSpeaking(false),
      onError: () => { setSpeaking(false); setStatus('The reply could not be read aloud.') },
    })
  }, [latestReply, readAloud, consented, provider, language])

  // A message being sent stops listening and speaking.
  useEffect(() => {
    if (!busy) return
    clearTimer()
    sessionRef.current?.abort()
    provider.cancelSpeech()
    setSpeaking(false)
  }, [busy, provider])

  // Leaving the chat view or locking the workspace releases the microphone and stops speech.
  useEffect(() => () => {
    clearTimer()
    sessionRef.current?.abort()
    provider.cancelSpeech()
  }, [provider])

  const micLabel = !provider.recognitionSupported
    ? 'Voice input is not supported in this browser'
    : listening ? 'Stop voice input' : 'Start voice input'

  return <>
    {provider.synthesisSupported && <button type="button" className={`voice-button ${readAloud ? 'voice-on' : ''}`} aria-pressed={readAloud} aria-label="Read replies aloud" title="Read replies aloud" onClick={toggleReadAloud}>
      {readAloud ? <Volume2 size={16} /> : <VolumeX size={16} />}
    </button>}
    {speaking && <button type="button" className="voice-button" aria-label="Stop speaking" title="Stop speaking" onClick={stopSpeaking}><Square size={14} /></button>}
    <button type="button" className={`voice-button ${listening ? 'voice-listening' : ''}`} aria-pressed={listening} aria-label={micLabel} title={micLabel}
      onClick={handleMic} disabled={!provider.recognitionSupported || busy}>
      {provider.recognitionSupported ? <Mic size={16} /> : <MicOff size={16} />}
    </button>
    <span className="voice-status" role="status" aria-live="polite">{interim ? `“${interim}”` : status}</span>
    {disclosureOpen && <div className="voice-backdrop">
      <section className="voice-dialog" role="dialog" aria-modal="true" aria-labelledby="voice-title" aria-describedby="voice-body"
        onKeyDown={(event) => { if (event.key === 'Escape') declineDisclosure() }}>
        <h2 id="voice-title">Use voice with JARVIS?</h2>
        <div id="voice-body">
          <p>Voice is optional. Typing always works.</p>
          <ul>
            <li>Speech recognition is done by your browser, not by JARVIS. In Chrome and Edge, your audio is sent to the browser maker’s online speech service (Google or Microsoft) to be transcribed.</li>
            <li>JARVIS never receives or stores audio. The transcript is placed in the message box; nothing is sent until you press send.</li>
            <li>Your browser will ask for microphone permission. You can revoke it at any time in the browser’s site settings.</li>
            <li>Reading replies aloud uses voices installed on this device when available; some browsers use online voices.</li>
          </ul>
        </div>
        <div className="voice-dialog-actions">
          <button type="button" autoFocus onClick={acceptDisclosure}>Enable voice</button>
          <button type="button" onClick={declineDisclosure}>Not now</button>
        </div>
      </section>
    </div>}
  </>
}
