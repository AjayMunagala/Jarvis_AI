import { useEffect, useRef, useState, type FormEvent } from 'react'
import {
  browserAction,
  closeBrowserSession,
  createBrowserSession,
  getBrowserScreenshot,
  getBrowserStatus,
  runBrowserResearch,
  type ApiAuth,
  type BrowserActionInput,
  type BrowserActionRecord,
  type BrowserElement,
  type BrowserSession,
  type BrowserStatus,
  type ResearchReport,
} from '../services/api'
import './BrowserPanel.css'

const OUTCOME_LABELS: Record<BrowserActionRecord['outcome'], string> = {
  performed: 'Performed',
  blocked: 'Blocked',
  approval_required: 'Approval required — not performed',
  failed: 'Failed',
  unsupported: 'Unsupported by provider',
  timed_out: 'Timed out',
}

function Outcome({ record }: { record: BrowserActionRecord }) {
  return <span className={`browser-outcome browser-outcome-${record.outcome}`}>{OUTCOME_LABELS[record.outcome]}</span>
}

export default function BrowserPanel({ auth, onUnlock }: { auth: ApiAuth; onUnlock: () => void }) {
  const [status, setStatus] = useState<BrowserStatus | null>(null)
  const [statusError, setStatusError] = useState('')
  const [tab, setTab] = useState<'session' | 'research'>('session')
  const [session, setSession] = useState<BrowserSession | null>(null)
  const [url, setUrl] = useState('')
  const [fieldText, setFieldText] = useState<Record<string, string>>({})
  const [fieldOption, setFieldOption] = useState<Record<string, string>>({})
  const [lastRecord, setLastRecord] = useState<BrowserActionRecord | null>(null)
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const [screenshotUrl, setScreenshotUrl] = useState<string | null>(null)
  const [question, setQuestion] = useState('')
  const [researchUrls, setResearchUrls] = useState('')
  const [interpret, setInterpret] = useState(false)
  const [research, setResearch] = useState<ReportState>({ report: null, error: '', pending: false })
  const generation = useRef(0)
  const inFlight = useRef(false)
  const controller = useRef<AbortController | null>(null)

  useEffect(() => () => { generation.current += 1; controller.current?.abort() }, [])
  useEffect(() => () => { if (screenshotUrl) URL.revokeObjectURL(screenshotUrl) }, [screenshotUrl])
  useEffect(() => {
    generation.current += 1
    controller.current?.abort()
    inFlight.current = false
    setSession(null); setLastRecord(null); setError(''); setPending(false); setStatus(null); setStatusError('')
    setResearch({ report: null, error: '', pending: false }); setScreenshotUrl(null)
    if (!auth.token) return
    const version = generation.current
    getBrowserStatus(auth).then((value) => { if (version === generation.current) setStatus(value) })
      .catch((cause: unknown) => { if (version === generation.current) setStatusError(cause instanceof Error ? cause.message : 'Browser status is unavailable.') })
  }, [auth.token]) // eslint-disable-line react-hooks/exhaustive-deps

  async function run<T>(operation: (signalAuth: ApiAuth) => Promise<T>, apply: (value: T) => void) {
    if (!auth.token || inFlight.current) return
    inFlight.current = true
    const version = ++generation.current
    controller.current?.abort()
    controller.current = new AbortController()
    setPending(true); setError('')
    try {
      const value = await operation({ ...auth, signal: controller.current.signal })
      if (version === generation.current) apply(value)
    } catch (cause) {
      if (version === generation.current) setError(cause instanceof Error ? cause.message : 'The browser request failed.')
    } finally {
      if (version === generation.current) { setPending(false); inFlight.current = false }
    }
  }

  function open(event: FormEvent) {
    event.preventDefault()
    const target = url.trim()
    if (!target) return
    if (session && session.state === 'active') {
      void act({ action: 'navigate', url: target })
    } else {
      void run((signalAuth) => createBrowserSession(target, signalAuth), (value) => { setSession(value); setLastRecord(value.actions[value.actions.length - 1] ?? null) })
    }
  }

  async function act(input: BrowserActionInput) {
    if (!session) return
    await run((signalAuth) => browserAction(session.session_id, input, signalAuth), async (value) => {
      setSession(value.session); setLastRecord(value.record)
      if (value.record.screenshot_id) {
        try {
          const blob = await getBrowserScreenshot(value.session.session_id, value.record.screenshot_id, auth)
          setScreenshotUrl(URL.createObjectURL(blob))
        } catch { setError('The screenshot could not be loaded.') }
      }
    })
  }

  function elementAction(element: BrowserElement, action: 'click' | 'type' | 'select') {
    const observationId = session?.last_observation?.observation_id
    if (!observationId) return
    const input: BrowserActionInput = { action, element_id: element.element_id, observation_id: observationId }
    if (action === 'type') input.text = fieldText[element.element_id] ?? ''
    if (action === 'select') input.option = fieldOption[element.element_id] ?? element.options.find((item) => item.selected)?.value ?? element.options[0]?.value
    void act(input)
  }

  function closeSession() {
    if (!session) return
    void run((signalAuth) => closeBrowserSession(session.session_id, signalAuth), (value) => { setSession(value); setScreenshotUrl(null) })
  }

  function submitResearch(event: FormEvent) {
    event.preventDefault()
    const urls = researchUrls.split('\n').map((item) => item.trim()).filter(Boolean)
    if (!question.trim() || !urls.length) { setResearch({ report: null, error: 'Enter a question and at least one URL.', pending: false }); return }
    if (urls.length > 5) { setResearch({ report: null, error: 'Use at most five URLs.', pending: false }); return }
    setResearch({ report: null, error: '', pending: true })
    void runBrowserResearch({ question: question.trim(), urls, include_model_interpretation: interpret }, auth)
      .then((report) => setResearch({ report, error: '', pending: false }))
      .catch((cause: unknown) => setResearch({ report: null, error: cause instanceof Error ? cause.message : 'Research failed.', pending: false }))
  }

  const observation = session?.last_observation ?? null
  const active = session?.state === 'active'
  const interaction = Boolean(session?.capabilities.interaction ?? status?.capabilities.interaction)
  const links = observation?.elements.filter((item) => item.kind === 'link') ?? []
  const fields = observation?.elements.filter((item) => ['text_input', 'textarea', 'select', 'checkbox', 'radio', 'other'].includes(item.kind)) ?? []
  const buttons = observation?.elements.filter((item) => item.kind === 'button' || item.kind === 'submit') ?? []
  const disabled = pending || !active

  return <section className="browser-panel" aria-labelledby="browser-heading">
    <p className="browser-kicker">CONTROLLED · PAGE CONTENT IS UNTRUSTED</p>
    <h1 id="browser-heading">Browser</h1>
    <p>JARVIS observes pages and performs only bounded, authorized actions. Page text is evidence, never instructions. Submissions, payments, deletions and other consequential actions are never performed in this version.</p>
    {!auth.token && <div role="status"><p>Unlock the workspace to use the browser.</p><button type="button" onClick={onUnlock}>Unlock browser workspace</button></div>}
    {statusError && <p role="alert">{statusError}</p>}
    {status && <p className="browser-status" role="status" aria-label="Browser provider status">
      Provider: <strong>{status.provider}</strong> · {status.enabled ? 'enabled' : 'disabled'} · {status.provider_available ? 'available' : 'unavailable'} · JavaScript {status.capabilities.javascript ? 'executed' : 'not executed'} · interaction {status.capabilities.interaction ? 'supported' : 'link navigation only'}
      {!status.enabled && <span> — set JARVIS_BROWSER_ENABLED=true to use the browser.</span>}
    </p>}
    <div className="browser-tabs" role="tablist">
      <button type="button" role="tab" aria-selected={tab === 'session'} onClick={() => setTab('session')}>Session</button>
      <button type="button" role="tab" aria-selected={tab === 'research'} onClick={() => setTab('research')}>Research</button>
    </div>

    {tab === 'session' && <div role="tabpanel">
      <form className="browser-url" onSubmit={open}>
        <label htmlFor="browser-url">Address</label>
        <div><input id="browser-url" value={url} onChange={(event) => setUrl(event.target.value)} maxLength={2000} placeholder="https://example.com" disabled={pending || !auth.token || status?.enabled === false} />
          <button type="submit" disabled={pending || !auth.token || !url.trim() || status?.enabled === false}>{active ? 'Go' : 'Open session'}</button></div>
      </form>
      {session && <div className="browser-controls" aria-label="Session controls">
        <button type="button" onClick={() => void act({ action: 'back' })} disabled={disabled}>Back</button>
        <button type="button" onClick={() => void act({ action: 'reload' })} disabled={disabled}>Reload</button>
        <button type="button" onClick={() => void act({ action: 'observe' })} disabled={disabled}>Observe</button>
        {session.capabilities.screenshots && <button type="button" onClick={() => void act({ action: 'screenshot' })} disabled={disabled}>Screenshot</button>}
        {interaction && <><button type="button" onClick={() => void act({ action: 'scroll', direction: 'up' })} disabled={disabled}>Scroll up</button>
          <button type="button" onClick={() => void act({ action: 'scroll', direction: 'down' })} disabled={disabled}>Scroll down</button></>}
        <button type="button" className="browser-danger" onClick={closeSession} disabled={pending || !active}>Close session</button>
      </div>}
      {pending && <p role="status">Working…</p>}
      {error && <p role="alert">{error}</p>}
      {session && <p className="browser-meta" aria-label="Session state">
        Session {session.state} · navigations {session.navigations_used}/{session.limits.max_navigations} · actions {session.actions_used}/{session.limits.max_actions} · expires {new Date(session.expires_at).toLocaleTimeString()}
        {session.failure_reason ? ` · ${session.failure_reason}` : ''}
      </p>}
      {session && session.pending_approvals.length > 0 && <div className="browser-approval" role="alert">
        <strong>{session.pending_approvals.length} action(s) require owner approval and were NOT performed.</strong>
        <ul>{session.pending_approvals.map((item) => <li key={item.action_id}>{item.action} “{item.element_label || item.element_id}” — {item.reason}</li>)}</ul>
        <p>Executing consequential actions is disabled in this version.</p>
      </div>}
      {lastRecord && <p className="browser-last" aria-label="Last action">Last action: {lastRecord.action}{lastRecord.element_label ? ` “${lastRecord.element_label}”` : ''} — <Outcome record={lastRecord} /> {lastRecord.reason}{lastRecord.transition === 'unexpected_navigation' ? ' (unexpected navigation)' : ''}</p>}

      {observation && <article className="browser-observation" aria-label="Page observation">
        <h2>{observation.title || '(untitled page)'}</h2>
        <p className="browser-meta">{observation.url} · HTTP {observation.http_status ?? 'unknown'} · observed {new Date(observation.retrieved_at).toLocaleTimeString()} · {observation.javascript_executed ? 'JavaScript executed' : 'JavaScript not executed'}{observation.redaction_count ? ` · ${observation.redaction_count} value(s) redacted` : ''}</p>
        {observation.limitations.length > 0 && <ul className="browser-limitations">{observation.limitations.map((item) => <li key={item}>{item}</li>)}</ul>}
        {observation.headings.length > 0 && <section><h3>Headings</h3><ul>{observation.headings.map((item, index) => <li key={index}>{item}</li>)}</ul></section>}
        <details><summary>Visible text (untrusted){observation.text_truncated ? ' — truncated' : ''}</summary><pre>{observation.text || '(no visible text)'}</pre></details>
        <section><h3>Links ({links.length})</h3>{links.length ? <ul className="browser-elements">{links.map((item) => <li key={item.element_id}>
          <span>{item.label || item.href}</span> <small>{item.href}</small>
          <button type="button" onClick={() => elementAction(item, 'click')} disabled={disabled}>Follow</button></li>)}</ul> : <p>No links found.</p>}</section>
        <section><h3>Form fields ({fields.length})</h3>{fields.length ? <ul className="browser-elements">{fields.map((item) => <li key={item.element_id}>
          <span>{item.label || item.name || item.element_id}</span> <small>{item.input_type ?? item.kind}{item.required ? ' · required' : ''}</small>
          {item.sensitive ? <em className="browser-sensitive">Sensitive field — JARVIS will not type here</em>
            : item.kind === 'select' ? <><select aria-label={`Option for ${item.label || item.element_id}`} value={fieldOption[item.element_id] ?? item.options.find((option) => option.selected)?.value ?? ''} onChange={(event) => setFieldOption({ ...fieldOption, [item.element_id]: event.target.value })} disabled={disabled || !interaction}>
              {item.options.map((option) => <option key={option.value} value={option.value}>{option.label || option.value}</option>)}</select>
              <button type="button" onClick={() => elementAction(item, 'select')} disabled={disabled || !interaction}>Select</button></>
            : item.kind === 'text_input' || item.kind === 'textarea' ? <><input aria-label={`Text for ${item.label || item.element_id}`} value={fieldText[item.element_id] ?? ''} onChange={(event) => setFieldText({ ...fieldText, [item.element_id]: event.target.value })} maxLength={1000} disabled={disabled || !interaction} />
              <button type="button" onClick={() => elementAction(item, 'type')} disabled={disabled || !interaction || !(fieldText[item.element_id] ?? '').length}>Type</button></>
            : item.kind === 'checkbox' || item.kind === 'radio' ? <button type="button" onClick={() => elementAction(item, 'click')} disabled={disabled || !interaction}>Toggle</button>
            : <em>Not supported</em>}
        </li>)}</ul> : <p>No form fields found.</p>}</section>
        <section><h3>Buttons ({buttons.length})</h3>{buttons.length ? <ul className="browser-elements">{buttons.map((item) => <li key={item.element_id}>
          <span>{item.label || item.element_id}</span> <small>{item.kind === 'submit' ? 'submits a form — requires approval' : 'button'}</small>
          <button type="button" onClick={() => elementAction(item, 'click')} disabled={disabled || item.disabled}>{item.kind === 'submit' ? 'Request' : 'Click'}</button></li>)}</ul> : <p>No buttons found.</p>}</section>
        <section aria-label="Diagnostics"><h3>Diagnostics</h3>
          {observation.console_errors.length ? <ul>{observation.console_errors.map((item, index) => <li key={index}>Console {item.level}: {item.message}</li>)}</ul> : <p>No console errors captured{session?.capabilities.console ? '' : ' (not supported by this provider)'}.</p>}
          {observation.network_failures.length ? <ul>{observation.network_failures.map((item, index) => <li key={index}>{item.method} {item.url} — {item.status ?? item.error}</li>)}</ul> : <p>No network failures captured.</p>}
        </section>
      </article>}
      {screenshotUrl && <figure className="browser-screenshot"><img src={screenshotUrl} alt="Latest browser screenshot" /><figcaption>Screenshot of the current viewport.</figcaption></figure>}
      {session && session.actions.length > 0 && <section aria-label="Action log"><h3>Action log</h3><ol className="browser-log">{session.actions.map((item) => <li key={item.action_id}>
        #{item.sequence} {item.action}{item.element_label ? ` “${item.element_label}”` : ''} · {item.classification} · <Outcome record={item} /> — {item.reason}</li>)}</ol></section>}
    </div>}

    {tab === 'research' && <div role="tabpanel">
      <form className="browser-research" onSubmit={submitResearch}>
        <label htmlFor="research-question">Question</label>
        <textarea id="research-question" value={question} onChange={(event) => setQuestion(event.target.value)} maxLength={500} disabled={research.pending || !auth.token} />
        <label htmlFor="research-urls">Source URLs (one per line, at most five). JARVIS reads only these pages; it does not search the web.</label>
        <textarea id="research-urls" value={researchUrls} onChange={(event) => setResearchUrls(event.target.value)} maxLength={10500} disabled={research.pending || !auth.token} />
        <label className="browser-check"><input type="checkbox" checked={interpret} onChange={(event) => setInterpret(event.target.checked)} disabled={research.pending || !auth.token} /> Add a model interpretation (labelled, not verified)</label>
        <button type="submit" disabled={research.pending || !auth.token || status?.enabled === false}>{research.pending ? 'Researching…' : 'Research'}</button>
      </form>
      {research.error && <p role="alert">{research.error}</p>}
      {research.report && <article aria-label="Research report">
        <h2>Sources</h2>
        <ul className="browser-sources">{research.report.sources.map((source) => <li key={source.source_id}>
          <strong>[{source.source_id}] {source.title || source.requested_url}</strong> <span className={`browser-source-${source.status}`}>{source.status}</span>
          <div><small>{source.final_url ?? source.requested_url} · retrieved {new Date(source.retrieved_at).toLocaleString()}{source.http_status ? ` · HTTP ${source.http_status}` : ''}</small></div>
          {source.failure_reason && <p>{source.failure_reason}</p>}
          {source.excerpt && <blockquote>{source.excerpt}</blockquote>}
        </li>)}</ul>
        <h2>Observations (verbatim from pages)</h2>
        {research.report.observations.length ? <ul>{research.report.observations.map((item, index) => <li key={index}>[{item.source_id}] “{item.quote}”</li>)}</ul> : <p>No sentence in the retrieved pages matched the question terms.</p>}
        {research.report.interpretation && <section className="browser-interpretation" aria-label="Model interpretation">
          <h2>Model interpretation — not verified</h2>
          <p>{research.report.interpretation.text}</p>
          <small>Provider {research.report.interpretation.provider}{research.report.interpretation.demo ? ' (demo)' : ''} · cited {research.report.interpretation.cited_source_ids.join(', ') || 'no sources'}{research.report.interpretation.unknown_citation_count ? ` · ${research.report.interpretation.unknown_citation_count} citation(s) to unknown sources` : ''}</small>
        </section>}
        <h3>Limitations</h3><ul>{research.report.limitations.map((item) => <li key={item}>{item}</li>)}</ul>
      </article>}
    </div>}
  </section>
}

type ReportState = { report: ResearchReport | null; error: string; pending: boolean }
