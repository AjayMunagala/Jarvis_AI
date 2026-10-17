# Voice and unified interface (v3.0)

v3.0 adds optional voice to chat and a small public interface-configuration endpoint. Decision D5: voice is **browser-native speech first**. The browser's Web Speech API performs recognition and read-aloud. JARVIS adds no server-side audio processing, no new dependency and no new execution surface.

## Privacy and consent

- **Voice is opt-in for each page session.** The first use of the microphone or read-aloud button opens a disclosure. Nothing is enabled until the owner chooses **Enable voice**. **Not now**, or the Escape key, keeps voice off. Consent is held in page memory only, so it is asked for again after a reload.
- **Recognition is done by the browser, not JARVIS.** In Chrome and Edge, recognition audio is sent to the browser maker's online speech service (Google or Microsoft). The disclosure says this before first use.
- **JARVIS never receives or stores audio.** The final transcript is cleaned (control characters removed, whitespace collapsed, at most 4000 characters) and **placed in the message box**. Nothing is sent until the owner presses send (`auto_send: false`).
- **The browser asks for microphone permission.** JARVIS never calls `getUserMedia`. If permission is denied, the composer shows a plain explanation, and typing keeps working.
- Read-aloud prefers a voice installed on the device (`localService`). Some browsers use online voices, which the disclosure notes. Spoken text is capped at 1500 characters, and the rest stays on screen.

## Behavior

| Control | Behavior |
|---|---|
| Microphone (**Start voice input / Stop voice input**) | One utterance (`continuous=false`, interim results shown); stops automatically after 30 s; disabled while a message is being sent; disabled with an explanation in browsers without recognition |
| **Read replies aloud** (toggle) | Reads only replies that arrive after it is turned on; hidden in browsers without speech synthesis |
| **Stop speaking** | Appears while speaking; interrupts at once. Starting the microphone or sending a message also interrupts |

Leaving the chat view, locking the workspace, or a 401 response releases the microphone and cancels speech.

Mapped errors: not allowed, no speech, no microphone, speech service unreachable, aborted, unsupported, and failed. Each one comes with a message offering to keep typing.

## Code

- `frontend/src/voice/speech.ts`: the `SpeechProvider` interface and `createBrowserSpeechProvider()`. It works with `SpeechRecognition` and `webkitSpeechRecognition`, maps errors, and cleans and bounds text. Other providers, such as a local or offline speech engine, can implement the same interface later.
- `frontend/src/voice/VoiceControls.tsx` and `.css`: the controls, the disclosure dialog, and a live status region.
- `frontend/src/App.tsx`: renders voice controls only when `GET /interface/config` reports `voice.enabled`. If that request fails, voice stays hidden. Navigation items show an **OFF** badge when the server has disabled Browser, Capabilities (tools) or Memory (persistence).

## Interface configuration API

`GET /interface/config` is public, like `/ready`. It returns fixed booleans only: no URLs, identifiers, tokens or provider names.

```json
{
  "contract_version": "1",
  "features": { "chat": true, "memory": false, "coding": true, "browser": false, "tools": false,
                "chat_tools": false, "activity": true, "approval_execution": false },
  "voice": { "enabled": true, "mode": "browser_native", "server_receives_audio": false,
             "auto_send": false, "requires_opt_in": true }
}
```

`approval_execution`, `server_receives_audio` and `auto_send` are literal `false`, and `requires_opt_in` is literal `true`. The contract cannot claim otherwise.

Setting: `JARVIS_VOICE_ENABLED` (default `true`, which offers the opt-in controls; `false` hides them).

## Tests

- `frontend/src/voice/speech.test.ts` covers the provider with fake Web Speech objects: unsupported browsers, configuration, interim and final results, error mapping, start failures, local-voice choice and bounds.
- `frontend/src/voice/VoiceControls.test.tsx` covers the disclosure and declining it, the transcript going into the draft and never being sent, errors, unsupported browsers, reading only new replies, interrupting, stopping on send, and releasing on unmount.
- `frontend/src/App.test.tsx` covers voice being hidden when the configuration is unavailable or disabled, typing staying available, and the OFF badges.
- `tests/test_interface.py` covers the endpoint, the absence of sensitive values, the literal contracts, and unchanged execution controls.
- One Playwright scenario stands in a deterministic replacement for the Web Speech API. It checks opt-in, that the transcript fills the composer with **no `/chat` request**, sending, and that the reply is read aloud.

**Not tested:** real microphones, real browser speech services, and real voices. Automated tests cannot exercise these. A manual check in Chrome or Edge is needed to confirm actual recognition quality and permission prompts.
