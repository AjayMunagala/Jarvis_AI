import { useEffect, useRef, useState, type FormEvent } from 'react'
import { analyzeCoding, type ApiAuth, type CodingChange, type CodingReport, type CodingWorkflow } from '../services/api'
import './CodingPanel.css'

const workflows: CodingWorkflow[] = ['auto', 'explain', 'locate', 'trace', 'diagnose', 'recommend', 'patch', 'review_patch', 'tests']

function Items({ title, values }: { title: string; values: string[] }) {
  return <section><h3>{title}</h3>{values.length ? <ul>{values.map((value, index) => <li key={index}>{value}</li>)}</ul> : <p>None identified in this bounded inspection.</p>}</section>
}

export default function CodingPanel({ auth, onUnlock }: { auth: ApiAuth; onUnlock: () => void }) {
  const [request, setRequest] = useState('')
  const [workflow, setWorkflow] = useState<CodingWorkflow>('auto')
  const [paths, setPaths] = useState('')
  const [observation, setObservation] = useState('')
  const [changes, setChanges] = useState('')
  const [consent, setConsent] = useState(false)
  const [report, setReport] = useState<CodingReport | null>(null)
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const controller = useRef<AbortController | null>(null)
  const generation = useRef(0)
  const inFlight = useRef(false)

  useEffect(() => () => { generation.current += 1; controller.current?.abort() }, [])
  useEffect(() => {
    generation.current += 1
    controller.current?.abort()
    inFlight.current = false
    setPending(false); setReport(null); setRequest(''); setPaths(''); setObservation(''); setChanges(''); setConsent(false); setError('')
  }, [auth.token])

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!auth.token || !request.trim() || inFlight.current) return
    let edits: CodingChange[] | undefined
    try {
      if (changes.trim()) {
        const parsed: unknown = JSON.parse(changes)
        if (!Array.isArray(parsed) || parsed.length > 5) throw new Error()
        edits = parsed as CodingChange[] // Closed server contracts validate each field and edit.
      }
    } catch { setError('Proposed changes must be a JSON array with at most five file changes.'); return }
    const targetPaths = paths.split('\n').map((path) => path.trim()).filter(Boolean)
    if (targetPaths.length > 8) { setError('Use at most eight repository-relative paths.'); return }
    inFlight.current = true
    const version = ++generation.current
    controller.current?.abort()
    controller.current = new AbortController()
    setPending(true); setError(''); setReport(null)
    try {
      const result = await analyzeCoding({ request, workflow, target_paths: targetPaths,
        ...(observation ? { reported_observation: observation } : {}), ...(edits ? { changes: edits } : {}), allow_provider_context: consent },
      { ...auth, signal: controller.current.signal })
      if (version === generation.current) setReport(result)
    } catch (cause) {
      if (version === generation.current) setError(cause instanceof Error ? cause.message : 'Coding analysis failed. Please try again.')
    } finally {
      if (version === generation.current) { setPending(false); inFlight.current = false }
    }
  }

  return <section className="coding-panel" aria-labelledby="coding-title">
    <h1 id="coding-title">Coding intelligence</h1>
    <p>Inspect repository evidence and propose reviewable patches. No code, tests or patches are executed or applied.</p>
    {!auth.token && <div role="status"><p>Unlock the workspace to inspect repository source.</p><button type="button" onClick={onUnlock}>Unlock coding workspace</button></div>}
    <form onSubmit={(event) => void submit(event)} aria-label="Coding request">
      <label htmlFor="coding-request">Engineering request</label>
      <textarea id="coding-request" value={request} onChange={(event) => setRequest(event.target.value)} maxLength={4000} required disabled={pending || !auth.token} />
      <label htmlFor="coding-workflow">Workflow</label>
      <select id="coding-workflow" value={workflow} onChange={(event) => setWorkflow(event.target.value as CodingWorkflow)} disabled={pending}>{workflows.map((value) => <option key={value} value={value}>{value.replace('_', ' ')}</option>)}</select>
      <label htmlFor="coding-paths">Repository-relative paths (optional, one per line, up to eight)</label>
      <textarea id="coding-paths" value={paths} onChange={(event) => setPaths(event.target.value)} maxLength={1928} disabled={pending || !auth.token} />
      <label htmlFor="coding-observation">Reported error or observation (unverified)</label>
      <textarea id="coding-observation" value={observation} onChange={(event) => setObservation(event.target.value)} maxLength={8000} disabled={pending || !auth.token} />
      <details><summary>Optional exact changes for offline patch generation</summary>
        <p>Use patch or review patch. Modifications require a unique old_text and replacement new_text. New files must also be listed in the paths above. Nothing is applied.</p>
        <pre>{'[{"path":"module.py","operation":"modify","reason":"Explain the change","replacements":[{"old_text":"return 1","new_text":"return 2"}]}]'}</pre>
        <label htmlFor="coding-changes">Proposed changes JSON</label><textarea id="coding-changes" value={changes} onChange={(event) => setChanges(event.target.value)} maxLength={24000} disabled={pending || !auth.token} />
      </details>
      <label className="coding-consent"><input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} disabled={pending || !auth.token} />Allow this request and bounded redacted repository context to be sent to the configured model provider. Redaction may miss secrets; leave unchecked for local static inspection.</label>
      <button type="submit" disabled={pending || !auth.token || !request.trim()}>{pending ? 'Inspecting…' : 'Analyze coding request'}</button>
    </form>
    {pending && <p role="status" aria-live="polite">Collecting bounded evidence and validating the proposal…</p>}
    {error && <p role="alert">{error}</p>}
    {!report && !pending && !error && <p>No coding report yet. Choose a request and optional repository paths.</p>}
    {report && <article aria-label="Coding report">
      <h2>Review only — nothing applied</h2><p role="status">Verification: {report.verification_status}. Analysis: {report.analysis_origin} · {report.provider}.</p>
      <h3>Request interpretation</h3><p>{report.interpretation}</p>
      <Items title="Repository facts" values={report.facts} />
      <section><h3>Repository evidence (untrusted reference data)</h3>{report.evidence.length ? report.evidence.map((item) => <details key={item.id}><summary>{item.id} · {item.path} · line {item.excerpt_start_line}{item.truncated ? ' · excerpt truncated' : ''}</summary><p>SHA-256: <code>{item.sha256}</code></p><pre>{item.excerpt}</pre><Items title="Declared symbols" values={item.symbols} /><Items title="Syntactic relationships" values={item.relationships} /></details>) : <p>No readable repository evidence was found.</p>}</section>
      {report.reported_observation && <section><h3>User-reported observation — not independently verified</h3><pre>{report.reported_observation}</pre></section>}
      <section><h3>Advisory findings — unverified</h3><p>{report.proposal.summary}</p>{report.proposal.hypotheses.map((item, index) => <p key={index}>{item.explanation} {item.evidence_ids.length ? `(references: ${item.evidence_ids.join(', ')})` : '(no source reference)'}</p>)}</section>
      <Items title="Recommended changes" values={report.proposal.recommendations} />
      <section><h3>Proposed patches · {report.patch_status}</h3>{report.patches.map((patch) => <details key={patch.path} open><summary>{patch.operation} · {patch.path}</summary><p>{patch.reason}</p><p>Before: <code>{patch.sha256_before ?? 'new file'}</code><br />After: <code>{patch.sha256_after}</code></p><pre aria-label={`Proposed diff for ${patch.path}`}>{patch.diff}</pre></details>)}</section>
      <Items title="Regression and security risks" values={report.proposal.risks} />
      <Items title="Existing tests identified — not run" values={report.existing_tests} />
      <Items title="Verification recommendations" values={report.verification_recommendations} />
      <Items title="Missing information" values={report.proposal.missing_information} />
      <Items title="Limitations and uncertainty" values={report.limitations} />
    </article>}
  </section>
}
