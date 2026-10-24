import { useEffect, useRef, useState, type FormEvent } from 'react'
import {
  callTool,
  getCapabilities,
  getToolAudit,
  type ApiAuth,
  type CapabilitiesResponse,
  type CapabilityDescriptor,
  type ToolAuditPage,
  type ToolResult,
} from '../services/api'
import './CapabilitiesPanel.css'

const STATUS_LABELS: Record<ToolResult['status'], string> = {
  succeeded: 'Succeeded',
  failed: 'Failed',
  timed_out: 'Timed out',
  unavailable: 'Unavailable',
  denied: 'Denied',
  invalid_input: 'Invalid input',
  partial: 'Partial',
  approval_required: 'Approval required — not executed',
}

function exampleArguments(capability: CapabilityDescriptor): string {
  const properties = capability.input_schema.properties ?? {}
  const required = new Set(capability.input_schema.required ?? [])
  const example: Record<string, unknown> = {}
  for (const [name, schema] of Object.entries(properties)) {
    if (!required.has(name)) continue
    example[name] = schema.type === 'integer' || schema.type === 'number' ? 1 : schema.type === 'array' ? [] : ''
  }
  return JSON.stringify(example, null, 2)
}

export default function CapabilitiesPanel({ auth, onUnlock }: { auth: ApiAuth; onUnlock: () => void }) {
  const [catalog, setCatalog] = useState<CapabilitiesResponse | null>(null)
  const [audit, setAudit] = useState<ToolAuditPage | null>(null)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState('')
  const [argsText, setArgsText] = useState('{}')
  const [result, setResult] = useState<ToolResult | null>(null)
  const [pending, setPending] = useState(false)
  const generation = useRef(0)
  const inFlight = useRef(false)

  async function refresh(version = generation.current) {
    try {
      const [nextCatalog, nextAudit] = await Promise.all([getCapabilities(auth), getToolAudit(25, auth)])
      if (version !== generation.current) return
      setCatalog(nextCatalog); setAudit(nextAudit); setError('')
    } catch (cause) {
      if (version === generation.current) setError(cause instanceof Error ? cause.message : 'Capabilities are unavailable.')
    }
  }

  useEffect(() => {
    generation.current += 1
    inFlight.current = false
    setCatalog(null); setAudit(null); setResult(null); setError(''); setSelected(''); setArgsText('{}'); setPending(false)
    if (auth.token) void refresh(generation.current)
  }, [auth.token]) // eslint-disable-line react-hooks/exhaustive-deps

  function choose(capability: CapabilityDescriptor) {
    setSelected(capability.tool_id); setArgsText(exampleArguments(capability)); setResult(null); setError('')
  }

  async function invoke(event: FormEvent) {
    event.preventDefault()
    if (!auth.token || !selected || inFlight.current) return
    let parsed: unknown
    try {
      parsed = JSON.parse(argsText)
    } catch {
      setError('Arguments must be valid JSON.'); return
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) { setError('Arguments must be a JSON object.'); return }
    inFlight.current = true
    const version = generation.current
    setPending(true); setError(''); setResult(null)
    try {
      const value = await callTool(selected, parsed as Record<string, unknown>, auth)
      if (version === generation.current) { setResult(value); void refresh(version) }
    } catch (cause) {
      if (version === generation.current) setError(cause instanceof Error ? cause.message : 'The tool call failed.')
    } finally {
      if (version === generation.current) { setPending(false); inFlight.current = false }
    }
  }

  const current = catalog?.capabilities.find((item) => item.tool_id === selected) ?? null

  return <section className="capabilities-panel" aria-labelledby="capabilities-heading">
    <p className="capabilities-kicker">READ-ONLY TOOL RUNTIME · DENY BY DEFAULT</p>
    <h1 id="capabilities-heading">Capabilities</h1>
    <p>Registered tools JARVIS can use, with their risk, required permissions and limits. Only low-risk read-only tools can run, and only when you invoke them. JARVIS does not call tools on its own in this version.</p>
    {!auth.token && <div role="status"><p>Unlock the workspace to view capabilities.</p><button type="button" onClick={onUnlock}>Unlock capabilities</button></div>}
    {error && <p role="alert">{error}</p>}
    {catalog && <p className="capabilities-status" role="status" aria-label="Tool runtime status">
      Tool runtime: <strong>{catalog.tools_enabled ? 'enabled' : 'disabled'}</strong> · {catalog.capabilities.length} registered tool(s) · {catalog.note}
    </p>}
    {catalog && <ul className="capabilities-list" aria-label="Registered tools">{catalog.capabilities.map((item) => <li key={item.tool_id}>
      <div className="capabilities-head"><strong>{item.title}</strong> <code>{item.tool_id}@{item.version}</code>
        <span className={`capabilities-risk capabilities-risk-${item.risk}`}>{item.risk} risk</span>
        {item.read_only && <span className="capabilities-tag">read-only</span>}
        <span className={item.available ? 'capabilities-ok' : 'capabilities-off'}>{item.available ? 'available' : 'unavailable'}</span></div>
      <p>{item.description}</p>
      {!item.available && item.availability_reason && <small>{item.availability_reason}</small>}
      <small>Permissions: {item.required_permissions.join(', ') || 'none'} · timeout {item.timeout_seconds}s · max output {Math.round(item.max_output_bytes / 1024)} KiB</small>
      <button type="button" onClick={() => choose(item)} disabled={!item.invocable}>{item.invocable ? 'Use tool' : 'Not invocable'}</button>
    </li>)}</ul>}

    {current && <form className="capabilities-call" onSubmit={invoke} aria-label="Invoke tool">
      <h2>Invoke {current.title}</h2>
      <label htmlFor="tool-arguments">Arguments (JSON, validated by the server against the tool's schema)</label>
      <textarea id="tool-arguments" value={argsText} onChange={(event) => setArgsText(event.target.value)} maxLength={8192} disabled={pending} />
      <details><summary>Input schema</summary><pre>{JSON.stringify(current.input_schema, null, 2)}</pre></details>
      <button type="submit" disabled={pending}>{pending ? 'Running…' : 'Run read-only tool'}</button>
    </form>}

    {result && <article className="capabilities-result" aria-label="Tool result">
      <h2>Result: <span className={`capabilities-status-${result.status}`}>{STATUS_LABELS[result.status]}</span></h2>
      <p className="capabilities-meta">{result.tool_id} · call {result.call_id} · {result.duration_ms.toFixed(1)} ms · side effects: {result.side_effects} · audit {result.audit_event_id}</p>
      {result.error && <p role="alert">{result.error.code}: {result.error.message}</p>}
      {result.untrusted_content && <p className="capabilities-warning">Output contains untrusted external or document content.</p>}
      {result.output && <pre>{JSON.stringify(result.output, null, 2)}</pre>}
      {result.evidence.length > 0 && <><h3>Evidence</h3><ul>{result.evidence.map((item) => <li key={item}><code>{item}</code></li>)}</ul></>}
    </article>}

    {audit && <section aria-label="Tool audit">
      <h2>Recent tool calls</h2>
      <p className="capabilities-meta">{audit.retention} Not durable.</p>
      {audit.events.length ? <ol className="capabilities-audit">{audit.events.map((item) => <li key={item.event_id}>
        {new Date(item.timestamp).toLocaleTimeString()} · {item.tool_id} · {item.decision} · {STATUS_LABELS[item.status]}{item.error_code ? ` (${item.error_code})` : ''} · args: {item.argument_keys.join(', ') || 'none'}
      </li>)}</ol> : <p>No tool calls yet.</p>}
    </section>}
  </section>
}
