export interface ChatRequest { message: string; conversation_id?: string }

export interface ChatMemoryContext {
  outcome: 'success' | 'empty' | 'fallback'
  memory_ids: string[]
}

export type ContextSourceType = 'conversation' | 'memory' | 'document' | 'knowledge' | 'workspace'

export interface ChatContextSource {
  ref: string
  source_type: ContextSourceType
  source_id: string
  label: string | null
  role: 'user' | 'assistant' | null
  timestamp: string | null
  excerpt_chars: number
  truncated: boolean
  cited_in_reply: boolean
}

export interface ChatContextSummary {
  history_status: 'included' | 'empty' | 'not_requested' | 'disabled' | 'unavailable'
  retrieval_status: 'success' | 'empty' | 'fallback' | 'disabled'
  // v4.7: read-only workspace context (knowledge, tasks, reminders, calendar); absent from older servers.
  workspace_status?: 'included' | 'empty' | 'disabled' | 'unavailable'
  sources: ChatContextSource[]
  omitted_count: number
  total_chars: number
  unknown_citation_count: number
}

export interface ToolActivityCall {
  ref: string
  round: number
  proposer: 'rules' | 'model'
  tool_id: string
  purpose: string
  status: string
  error_code: string | null
  executed: boolean
  evidence: string[]
  duration_ms: number
  call_id: string | null
  audit_event_id: string | null
  untrusted_content: boolean
}

export interface ToolVerification {
  status: 'verified' | 'partially_verified' | 'unverified' | 'not_applicable'
  checks: { name: string; outcome: 'pass' | 'fail' | 'not_evaluated'; detail: string }[]
  cited_refs: string[]
  unknown_citation_count: number
  summary: string
}

export interface ToolActivity {
  proposer: 'rules' | 'model'
  rounds_used: number
  calls: ToolActivityCall[]
  stopped_reason: 'no_tools_needed' | 'completed' | 'round_limit' | 'call_limit' | 'deadline' | 'proposal_invalid' | 'provider_failed'
  verification: ToolVerification | null
  limitations: string[]
}

export interface ChatResponse {
  reply: string
  provider: string
  demo: boolean
  partial?: boolean
  conversation_id?: string | null
  memory_context?: ChatMemoryContext | null
  context?: ChatContextSummary
  tool_activity?: ToolActivity
}

export interface ConversationSummary {
  conversation_id: string
  created_at: string
  last_activity_at: string
  preview: string | null
}

export interface ConversationPage {
  items: ConversationSummary[]
  limit: number
  offset: number
  has_more: boolean
}

export interface ConversationHistoryMessage {
  id: string
  conversation_id: string
  sequence_number: number
  role: 'user' | 'assistant'
  content: string
  created_at: string
}

export interface ConversationHistoryPage {
  conversation_id: string
  messages: ConversationHistoryMessage[]
  has_more: boolean
  next_before_sequence: number | null
}

export interface MemoryRecord {
  id: string
  content: string
  category: string | null
  source_reference: string | null
  source_kind?: 'memory' | 'document'
  created_at: string
  updated_at: string
}

export interface MemoryPage {
  items: MemoryRecord[]
  total: number
  limit: number
  offset: number
  has_more: boolean
}

export interface MemoryInput {
  content: string
  category: string | null
  source_reference: string | null
}

export interface ComponentReadiness {
  application?: 'ready' | 'configured' | 'disabled' | 'unavailable'
  provider?: 'ready' | 'configured' | 'disabled' | 'unavailable'
  orchestration?: 'ready' | 'configured' | 'disabled' | 'unavailable'
  authorization?: 'ready' | 'configured' | 'disabled' | 'unavailable'
  persistence?: 'ready' | 'configured' | 'disabled' | 'unavailable'
  repository_intelligence?: 'ready' | 'configured' | 'disabled' | 'unavailable'
  [key: string]: 'ready' | 'configured' | 'disabled' | 'unavailable' | undefined
}

export interface ReadinessResponse {
  status: 'ready' | 'not_ready'
  version: string
  components: ComponentReadiness
}

const configuredBaseUrl = import.meta.env.VITE_API_BASE_URL?.replace(/\/$/, '')
const apiBaseUrl = configuredBaseUrl || '/api'

export interface ApiAuth {
  token?: string
  onUnauthorized?: () => void
  signal?: AbortSignal
}

async function request<T>(path: string, init?: RequestInit, allowChatValidation = false, auth: ApiAuth = {}): Promise<T> {
  let response: Response
  let requestUrl: URL
  try {
    requestUrl = new URL(`${apiBaseUrl}${path}`, window.location.origin)
    if (!['http:', 'https:'].includes(requestUrl.protocol)) throw new Error('Unsupported API protocol')
    const configuredUrl = new URL(apiBaseUrl, window.location.origin)
    if (configuredUrl.username || configuredUrl.password) throw new Error('API URLs must not contain credentials')
    const headers = new Headers(init?.headers)
    if (auth.token) {
      const loopbackHosts = new Set(['localhost', '127.0.0.1', '[::1]'])
      if (requestUrl.origin !== configuredUrl.origin
        || (configuredUrl.protocol === 'http:' && !loopbackHosts.has(configuredUrl.hostname))) {
        throw new Error('API origin validation failed')
      }
      headers.set('Authorization', `Bearer ${auth.token}`)
    }
    response = await fetch(configuredBaseUrl ? requestUrl.href : `${apiBaseUrl}${path}`, {
      ...init, headers, ...(auth.signal ? { signal: auth.signal } : {}), ...(auth.token ? { redirect: 'error' as RequestRedirect } : {}),
    })
  } catch {
    throw new Error('Could not reach JARVIS. Check that the backend is running and try again.')
  }
  if (response.status === 401) {
    auth.onUnauthorized?.()
    throw new Error('Workspace locked. Enter your local access token again.')
  }
  if (response.status === 204) return undefined as T
  if (!response.ok && (path.startsWith('/browser') || path.startsWith('/tools') || path === '/capabilities'
    || path.startsWith('/approvals') || path.startsWith('/activity') || path.startsWith('/assistant')
    || path.startsWith('/scheduler') || path.startsWith('/workflows') || path.startsWith('/integrations'))) {
    // Browser and tool routes return short, sanitized policy messages that explain blocks.
    let detail = ''
    try {
      const data = await response.clone().json() as { detail?: unknown }
      if (typeof data.detail === 'string') detail = data.detail.slice(0, 300)
    } catch { /* fall through to the generic message */ }
    throw new Error(detail || (path.startsWith('/browser') ? 'The browser request failed.' : 'JARVIS could not complete that request. Check the fields and try again.'))
  }
  if (!response.ok) {
    if (path === '/coding/analyze') {
      const messages: Record<number, string> = {
        403: 'Coding inspection is not permitted by the server policy.',
        413: 'The coding request exceeds the configured request size limit.',
        422: 'The coding request, repository paths or proposed changes are invalid.',
        502: 'The coding proposal could not be validated. No changes were applied.',
        503: 'Coding intelligence is unavailable or not configured.',
        504: 'The coding request timed out. No changes were applied.',
      }
      throw new Error(messages[response.status] ?? 'Coding analysis failed. No changes were applied.')
    }
    if (response.status === 503) {
      if (path === '/ready' || path.startsWith('/ready')) {
        try {
          const data = await response.clone().json() as ReadinessResponse
          if (data && (data.status === 'not_ready' || data.status === 'ready')) {
            return data as T
          }
        } catch {
          // fall through to error message
        }
        throw new Error('JARVIS services are not ready. Check backend status and try again.')
      }
      if (path.startsWith('/conversations')) {
        throw new Error('Saved conversations are disabled or unavailable. Enable local PostgreSQL persistence to use conversation history.')
      }
      if (path.startsWith('/memories')) {
        throw new Error('Memory storage is disabled or unavailable. Enable local PostgreSQL persistence to manage saved memories.')
      }
      if (path.startsWith('/chat')) {
        throw new Error('Chat service is temporarily unavailable. Check that the provider and local access are configured and try again.')
      }
      if (path.startsWith('/repo')) {
        throw new Error('Repository intelligence service is temporarily unavailable or not configured.')
      }
      throw new Error('That service is temporarily unavailable. Please try again later.')
    }
    if (response.status === 404) {
      if (path.startsWith('/conversations/')) throw new Error('That conversation is no longer available. Refresh the list and try again.')
      throw new Error('That memory is no longer available. Refresh the list and try again.')
    }
    if (response.status === 413) throw new Error('The document exceeds the 16 KiB upload limit.')
    if (response.status === 422) {
      if (allowChatValidation) {
        try {
          const data = await response.clone().json() as { detail?: unknown }
          if (data.detail === 'The request body is invalid.' || data.detail === 'Message exceeds the configured size limit.') throw new Error(data.detail)
        } catch (cause) {
          if (cause instanceof Error && (cause.message === 'The request body is invalid.' || cause.message === 'Message exceeds the configured size limit.')) throw cause
        }
      }
      throw new Error('The request is invalid. Check the fields and try again.')
    }
    throw new Error('JARVIS could not complete that request. Please try again.')
  }
  try {
    if (path.startsWith('/browser/') && path.includes('/screenshots/')) return await response.blob() as T
    return await response.json() as T
  } catch {
    throw new Error('JARVIS returned an unreadable response. Please try again.')
  }
}

const json = (body: unknown): RequestInit => ({
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
})

// ----------------------------------------------- notifications (v6.4)

export interface JarvisNotification {
  id: string
  owner_id: string
  category: string
  title: string
  message: string
  priority: 'low' | 'normal' | 'high' | 'urgent'
  read: boolean
  delivered_at: string | null
  created_at: string
}
export interface NotificationPage { items: JarvisNotification[]; total: number }

export function getNotifications(unreadOnly: boolean, limit: number, auth?: ApiAuth): Promise<NotificationPage> {
  const query = new URLSearchParams({ unread_only: String(unreadOnly), limit: String(limit) })
  return request<NotificationPage>(`/scheduler/notifications?${query.toString()}`, undefined, false, auth)
}

export function markNotificationRead(notificationId: string, auth?: ApiAuth): Promise<{ marked: boolean }> {
  return request<{ marked: boolean }>(`/scheduler/notifications/${encodeURIComponent(notificationId)}/read`, { method: 'PATCH' }, false, auth)
}

export type CodingWorkflow = 'auto' | 'explain' | 'locate' | 'trace' | 'diagnose' | 'recommend' | 'patch' | 'review_patch' | 'tests'
export interface CodingChange {
  path: string
  operation: 'modify' | 'create'
  replacements?: { old_text: string; new_text: string }[]
  content?: string
  reason: string
}
export interface CodingRequest {
  request: string
  workflow: CodingWorkflow
  target_paths: string[]
  reported_observation?: string
  changes?: CodingChange[]
  allow_provider_context: boolean
}
export interface CodingReport {
  execution_id: string
  workflow: Exclude<CodingWorkflow, 'auto'>
  interpretation: string
  analysis_origin: 'static' | 'model'
  provider: string
  evidence: { id: string; path: string; sha256: string; excerpt: string; excerpt_start_line: number; truncated: boolean; symbols: string[]; relationships: string[] }[]
  facts: string[]
  reported_observation: string | null
  proposal: { summary: string; hypotheses: { explanation: string; evidence_ids: string[] }[]; recommendations: string[]; risks: string[]; missing_information: string[]; changes: CodingChange[] }
  patch_status: 'not_requested' | 'proposed' | 'unavailable'
  patches: { path: string; operation: 'modify' | 'create'; reason: string; sha256_before: string | null; sha256_after: string; diff: string }[]
  existing_tests: string[]
  verification_recommendations: string[]
  limitations: string[]
  verification_status: 'not_run'
  repository_modified: false
  execution_enabled: false
}

export function analyzeCoding(body: CodingRequest, auth?: ApiAuth): Promise<CodingReport> {
  return request<CodingReport>('/coding/analyze', json(body), false, auth).then((data) => {
    const strings = (value: unknown): value is string[] => Array.isArray(value) && value.every((item) => typeof item === 'string')
    if (!data || typeof data.execution_id !== 'string' || typeof data.interpretation !== 'string'
      || !['static', 'model'].includes(data.analysis_origin) || typeof data.provider !== 'string'
      || data.verification_status !== 'not_run' || data.repository_modified !== false || data.execution_enabled !== false
      || !strings(data.facts) || !strings(data.existing_tests) || !strings(data.verification_recommendations) || !strings(data.limitations)
      || !data.proposal || typeof data.proposal.summary !== 'string' || !strings(data.proposal.recommendations)
      || !strings(data.proposal.risks) || !strings(data.proposal.missing_information)
      || !Array.isArray(data.proposal.hypotheses) || !data.proposal.hypotheses.every((item) => item && typeof item.explanation === 'string' && strings(item.evidence_ids))
      || !Array.isArray(data.evidence) || !data.evidence.every((item) => item && typeof item.path === 'string' && typeof item.id === 'string'
        && typeof item.sha256 === 'string' && typeof item.excerpt === 'string' && strings(item.symbols) && strings(item.relationships))
      || !Array.isArray(data.patches) || !data.patches.every((item) => item && typeof item.path === 'string' && typeof item.reason === 'string' && typeof item.diff === 'string'
        && typeof item.sha256_after === 'string' && (item.sha256_before === null || typeof item.sha256_before === 'string'))) {
      throw new Error('JARVIS returned an unexpected coding report. No changes were applied.')
    }
    return data
  })
}

export function sendChatMessage(message: string, auth?: ApiAuth, conversationId?: string): Promise<ChatResponse> {
  const body: ChatRequest = conversationId ? { message, conversation_id: conversationId } : { message }
  return request<ChatResponse>('/chat', json(body), true, auth).then((data) => {
    if (!data || typeof data.reply !== 'string' || typeof data.provider !== 'string' || typeof data.demo !== 'boolean') {
      throw new Error('JARVIS returned an unexpected response. Please try again.')
    }
    return data
  })
}

export function listConversations(options: { limit?: number; offset?: number } = {}, auth?: ApiAuth): Promise<ConversationPage> {
  const query = new URLSearchParams()
  query.set('limit', String(options.limit ?? 20))
  query.set('offset', String(options.offset ?? 0))
  return request<ConversationPage>(`/conversations?${query.toString()}`, undefined, false, auth)
}

export function getConversationMessages(
  conversationId: string,
  options: { limit?: number; beforeSequence?: number } = {},
  auth?: ApiAuth,
): Promise<ConversationHistoryPage> {
  const query = new URLSearchParams()
  query.set('limit', String(options.limit ?? 100))
  if (options.beforeSequence !== undefined) query.set('before_sequence', String(options.beforeSequence))
  return request<ConversationHistoryPage>(`/conversations/${encodeURIComponent(conversationId)}/messages?${query.toString()}`, undefined, false, auth)
}

export function listMemories(options: { limit?: number; offset?: number; category?: string } = {}, auth?: ApiAuth): Promise<MemoryPage> {
  const query = new URLSearchParams()
  query.set('limit', String(options.limit ?? 20))
  query.set('offset', String(options.offset ?? 0))
  if (options.category) query.set('category', options.category)
  return request<MemoryPage>(`/memories?${query.toString()}`, undefined, false, auth)
}

export function searchMemories(query: string, options: { limit?: number; offset?: number } = {}, auth?: ApiAuth): Promise<MemoryPage> {
  return request<MemoryPage>('/memories/search', json({ query, limit: options.limit ?? 20, offset: options.offset ?? 0 }), false, auth)
}

export function createMemory(input: MemoryInput, auth?: ApiAuth): Promise<MemoryRecord> {
  return request<MemoryRecord>('/memories', json(input), false, auth)
}

export function ingestDocument(file: File, auth?: ApiAuth): Promise<MemoryRecord> {
  return request<MemoryRecord>('/memories/ingest', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/octet-stream',
      // Keep arbitrary Unicode filenames valid in an HTTP header. The server
      // decodes this value, strips any path, and persists only the basename.
      'X-Document-Filename': encodeURIComponent(file.name),
    },
    body: file,
  }, false, auth)
}

export function updateMemory(id: string, input: Partial<MemoryInput>, auth?: ApiAuth): Promise<MemoryRecord> {
  return request<MemoryRecord>(`/memories/${encodeURIComponent(id)}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input),
  }, false, auth)
}

export function deleteMemory(id: string, auth?: ApiAuth): Promise<void> {
  return request<void>(`/memories/${encodeURIComponent(id)}`, { method: 'DELETE' }, false, auth)
}

export function getMemory(id: string, auth?: ApiAuth): Promise<MemoryRecord> {
  return request<MemoryRecord>(`/memories/${encodeURIComponent(id)}`, undefined, false, auth)
}

export function getReadiness(auth?: ApiAuth): Promise<ReadinessResponse> {
  return request<ReadinessResponse>('/ready', undefined, false, auth)
}

export function getRepoInventory(auth?: ApiAuth): Promise<unknown> {
  return request<unknown>('/repo/inventory', undefined, false, auth)
}

export type VerificationState =
  | 'not_checked'
  | 'check_requested'
  | 'check_running'
  | 'check_passed'
  | 'check_failed'
  | 'check_timed_out'
  | 'check_blocked'
  | 'check_result_unavailable'
  | 'inconclusive'

export type CheckScope = 'syntax' | 'unit' | 'integration' | 'typecheck' | 'build' | 'regression'

export interface CheckPlanItem {
  check_id: string
  name: string
  scope: CheckScope
  target: string
  profile_id: string
  rationale: string
  expected_files: string[]
}

export interface VerificationPlan {
  plan_id: string
  target_paths: string[]
  checks: CheckPlanItem[]
  rationale: string
  execution_enabled: boolean
  created_at: string
}

export interface VerificationEvidence {
  evidence_id: string
  check_id: string
  check_name: string
  profile_id: string
  state: VerificationState
  command_executed?: string[] | null
  exit_code?: number | null
  started_at: string
  completed_at?: string | null
  duration_seconds: number
  output_excerpt: string
  output_truncated: boolean
  redacted_error_details?: string | null
  verified_files: string[]
  provenance: 'direct_execution' | 'static_analysis' | 'reported_external'
  limitations: string[]
}

export interface CheckResult {
  check_id: string
  name: string
  scope: CheckScope
  state: VerificationState
  evidence: VerificationEvidence
  summary: string
  diagnostics?: string[]
}

export interface VerificationReport {
  execution_id: string
  plan_id: string
  overall_state: VerificationState
  results: CheckResult[]
  summary: string
  facts: string[]
  limitations: string[]
  execution_enabled: boolean
  repository_modified: false
}

export interface ProfileDefinition {
  profile_id: string
  name: string
  description: string
  working_directory: '.' | 'frontend'
  allowed_target_dirs: string[]
  default_timeout_seconds: number
}

export interface VerificationProfilesResponse {
  execution_enabled: boolean
  profiles: ProfileDefinition[]
}

export interface VerificationPlanRequest {
  target_paths?: string[]
  reason?: string
}

export interface VerifyPatchRequest {
  patches: { path: string; operation: 'modify' | 'create'; reason: string; sha256_before: string | null; sha256_after: string; diff: string }[]
  reported_observation?: string | null
}

export function createVerificationPlan(body: VerificationPlanRequest, auth?: ApiAuth): Promise<VerificationPlan> {
  return request<VerificationPlan>('/verification/plan', json(body), false, auth)
}

export function verifyPatch(body: VerifyPatchRequest, auth?: ApiAuth): Promise<VerificationReport> {
  return request<VerificationReport>('/verification/verify-patch', json(body), false, auth)
}

export function getVerificationProfiles(auth?: ApiAuth): Promise<VerificationProfilesResponse> {
  return request<VerificationProfilesResponse>('/verification/profiles', undefined, false, auth)
}

// ------------------------------------------------------------ browser (v2.6)

export type BrowserActionName = 'navigate' | 'back' | 'reload' | 'observe' | 'click' | 'type' | 'select' | 'scroll' | 'screenshot'
export type BrowserActionOutcome = 'performed' | 'blocked' | 'approval_required' | 'failed' | 'unsupported' | 'timed_out'
export interface BrowserCapabilities { javascript: boolean; interaction: boolean; screenshots: boolean; console: boolean; network_diagnostics: boolean }
export interface BrowserLimits { max_navigations: number; max_actions: number; navigation_timeout_seconds: number; session_ttl_seconds: number; max_text_chars: number; max_elements: number }
export interface BrowserElement {
  element_id: string
  kind: 'link' | 'button' | 'submit' | 'text_input' | 'textarea' | 'select' | 'checkbox' | 'radio' | 'other'
  tag: string
  label: string
  text: string
  href: string | null
  input_type: string | null
  name: string | null
  form_id: string | null
  required: boolean
  disabled: boolean
  sensitive: boolean
  options: { value: string; label: string; selected: boolean }[]
}
export interface BrowserObservation {
  observation_id: string
  session_id: string | null
  sequence: number
  url: string
  title: string
  retrieved_at: string
  http_status: number | null
  content_sha256: string
  text: string
  text_truncated: boolean
  headings: string[]
  elements: BrowserElement[]
  elements_truncated: boolean
  forms: { form_id: string; action: string | null; method: 'get' | 'post'; label: string; element_ids: string[] }[]
  console_errors: { level: 'error' | 'warning'; message: string }[]
  network_failures: { url: string; method: string; status: number | null; error: string; resource_type: string }[]
  provider: 'mock' | 'http' | 'playwright'
  javascript_executed: boolean
  redaction_count: number
  limitations: string[]
  untrusted: true
}
export interface BrowserActionRecord {
  action_id: string
  sequence: number
  action: BrowserActionName
  classification: 'observe' | 'navigate' | 'interact' | 'consequential' | 'blocked'
  outcome: BrowserActionOutcome
  reason: string
  element_id: string | null
  element_label: string
  started_at: string
  duration_ms: number
  url_before: string | null
  url_after: string | null
  transition: 'none' | 'same_page' | 'navigated' | 'unexpected_navigation'
  resulting_observation_id: string | null
  screenshot_id: string | null
}
export interface BrowserSession {
  session_id: string
  state: 'active' | 'closed' | 'expired' | 'failed'
  provider: 'mock' | 'http' | 'playwright'
  capabilities: BrowserCapabilities
  created_at: string
  expires_at: string
  current_url: string | null
  navigations_used: number
  actions_used: number
  limits: BrowserLimits
  last_observation: BrowserObservation | null
  actions: BrowserActionRecord[]
  screenshots: { screenshot_id: string; observation_id: string | null; captured_at: string; mime_type: 'image/png'; byte_size: number; sha256: string }[]
  pending_approvals: BrowserActionRecord[]
  failure_reason: string | null
  execution_of_consequential_actions: 'disabled'
}
export interface BrowserStatus {
  enabled: boolean
  provider: 'mock' | 'http' | 'playwright'
  provider_available: boolean
  provider_detail: string
  capabilities: BrowserCapabilities
  limits: BrowserLimits
  private_network_allowed: boolean
  allowed_domains: string[]
  consequential_actions: 'approval_required'
}
export interface BrowserActionInput {
  action: BrowserActionName
  element_id?: string
  observation_id?: string
  text?: string
  option?: string
  direction?: 'up' | 'down'
  url?: string
}
export interface BrowserActionResponse { record: BrowserActionRecord; session: BrowserSession }
export interface ResearchSource {
  source_id: string
  requested_url: string
  final_url: string | null
  title: string
  status: 'retrieved' | 'failed' | 'blocked' | 'timed_out'
  failure_reason: string | null
  retrieved_at: string
  http_status: number | null
  content_sha256: string | null
  excerpt: string
  excerpt_truncated: boolean
  matched_terms: string[]
  javascript_executed: boolean
  provider: string
}
export interface ResearchReport {
  research_id: string
  question: string
  started_at: string
  completed_at: string
  sources: ResearchSource[]
  observations: { source_id: string; quote: string; origin: 'page_text' }[]
  interpretation: { text: string; origin: 'model'; provider: string; demo: boolean; cited_source_ids: string[]; unknown_citation_count: number; verified: false } | null
  limitations: string[]
  content_untrusted: true
}

export function getBrowserStatus(auth?: ApiAuth): Promise<BrowserStatus> {
  return request<BrowserStatus>('/browser/status', undefined, false, auth)
}

export function createBrowserSession(url: string | undefined, auth?: ApiAuth): Promise<BrowserSession> {
  return request<BrowserSession>('/browser/sessions', json(url ? { url } : {}), false, auth)
}

export function getBrowserSession(sessionId: string, auth?: ApiAuth): Promise<BrowserSession> {
  return request<BrowserSession>(`/browser/sessions/${encodeURIComponent(sessionId)}`, undefined, false, auth)
}

export function closeBrowserSession(sessionId: string, auth?: ApiAuth): Promise<BrowserSession> {
  return request<BrowserSession>(`/browser/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' }, false, auth)
}

export function browserAction(sessionId: string, input: BrowserActionInput, auth?: ApiAuth): Promise<BrowserActionResponse> {
  return request<BrowserActionResponse>(`/browser/sessions/${encodeURIComponent(sessionId)}/actions`, json(input), false, auth)
}

export function getBrowserScreenshot(sessionId: string, screenshotId: string, auth?: ApiAuth): Promise<Blob> {
  return request<Blob>(`/browser/sessions/${encodeURIComponent(sessionId)}/screenshots/${encodeURIComponent(screenshotId)}`, undefined, false, auth)
}

export function runBrowserResearch(body: { question: string; urls: string[]; include_model_interpretation: boolean }, auth?: ApiAuth): Promise<ResearchReport> {
  return request<ResearchReport>('/browser/research', json(body), false, auth)
}

// ------------------------------------------------- capabilities and tools (v2.7)

export type ToolRisk = 'low' | 'medium' | 'high' | 'critical'
export type ToolStatus = 'succeeded' | 'failed' | 'timed_out' | 'unavailable' | 'denied' | 'invalid_input' | 'partial' | 'approval_required'
export interface CapabilityDescriptor {
  tool_id: string
  version: string
  title: string
  description: string
  risk: ToolRisk
  read_only: boolean
  side_effects: 'none'
  required_permissions: string[]
  input_schema: { properties?: Record<string, { type?: string; description?: string; default?: unknown }>; required?: string[] } & Record<string, unknown>
  output_schema: Record<string, unknown>
  timeout_seconds: number
  max_output_bytes: number
  available: boolean
  availability_reason: string
  invocable: boolean
  autonomous: false
}
export interface CapabilitiesResponse { contract_version: string; tools_enabled: boolean; capabilities: CapabilityDescriptor[]; note: string }
export interface ToolResult {
  contract_version: string
  call_id: string
  tool_id: string
  tool_version: string | null
  status: ToolStatus
  output: Record<string, unknown> | null
  error: { code: string; message: string } | null
  evidence: string[]
  started_at: string
  completed_at: string
  duration_ms: number
  side_effects: 'none'
  retryable: boolean
  output_bytes: number
  audit_event_id: string
  untrusted_content: boolean
}
export interface ToolAuditEvent {
  event_id: string
  timestamp: string
  call_id: string
  correlation_id: string
  tool_id: string
  actor: string
  decision: 'allowed' | 'denied' | 'approval_required' | 'invalid_input' | 'unavailable' | 'disabled'
  status: ToolStatus
  error_code: string | null
  argument_keys: string[]
  argument_sha256: string
  duration_ms: number
  evidence_count: number
  durable: false
}
export interface ToolAuditPage { durable: false; retention: string; events: ToolAuditEvent[] }

export function getCapabilities(auth?: ApiAuth): Promise<CapabilitiesResponse> {
  return request<CapabilitiesResponse>('/capabilities', undefined, false, auth)
}

export function callTool(toolId: string, args: Record<string, unknown>, auth?: ApiAuth): Promise<ToolResult> {
  return request<ToolResult>('/tools/calls', json({ tool_id: toolId, arguments: args }), false, auth)
}

export function getToolAudit(limit: number, auth?: ApiAuth): Promise<ToolAuditPage> {
  return request<ToolAuditPage>(`/tools/audit?limit=${encodeURIComponent(String(limit))}`, undefined, false, auth)
}

// ----------------------------------------------- activity and approvals (v2.9)

export interface AuditRecord {
  event_id: string
  occurred_at: string
  owner_id: string
  category: 'tool' | 'browser' | 'approval' | 'intelligence' | 'orchestration' | 'assistant' | 'workflow'
    | 'integration'
    | 'knowledge' | 'research' | 'scheduler' | 'health' | 'finance' | 'improvement'
  event_type: string
  decision: string
  status: string
  subject: string
  correlation_id: string | null
  details: Record<string, string | number | boolean | null>
  durable: boolean
}
export interface ActivityPage { durable_storage: boolean; storage_note: string; events: AuditRecord[] }
export type ApprovalState = 'pending' | 'approved' | 'rejected' | 'expired'
export interface ApprovalRequest {
  approval_id: string
  owner_id: string
  // v4.3: autonomous, improvement and quarantine requests are enforced; their approved action runs once.
  category: 'tool' | 'browser' | 'autonomous' | 'improvement' | 'quarantine' | 'workflow' | 'integration'
  action: string
  target: string
  reason: string
  risk: 'low' | 'medium' | 'high' | 'critical' | 'consequential'
  expected_side_effects: string
  scope_sha256: string
  correlation_id: string | null
  created_at: string
  expires_at: string
  state: ApprovalState
  decided_at: string | null
  decision_note: string | null
  execution: 'not_available' | 'single_use'
  consumed_at: string | null
  durable: boolean
}
export interface ApprovalPage { durable_storage: boolean; execution_note: string; approvals: ApprovalRequest[] }

export function getActivity(limit: number, auth?: ApiAuth, category?: AuditRecord['category']): Promise<ActivityPage> {
  const query = new URLSearchParams({ limit: String(limit) })
  if (category) query.set('category', category)
  return request<ActivityPage>(`/activity?${query.toString()}`, undefined, false, auth)
}

export function getApprovals(limit: number, auth?: ApiAuth, state?: ApprovalState): Promise<ApprovalPage> {
  const query = new URLSearchParams({ limit: String(limit) })
  if (state) query.set('state', state)
  return request<ApprovalPage>(`/approvals?${query.toString()}`, undefined, false, auth)
}

export function decideApproval(approvalId: string, approve: boolean, note: string | undefined, auth?: ApiAuth): Promise<ApprovalRequest> {
  return request<ApprovalRequest>(`/approvals/${encodeURIComponent(approvalId)}/${approve ? 'approve' : 'reject'}`, json(note ? { note } : {}), false, auth)
}

// ----------------------------------------------- approval-gated responses (v6.7)

/** HTTP 202 body of an approval-gated route: nothing ran; the owner must approve ``approval`` first. */
export interface ApprovalRequiredResponse { status: 'approval_required'; detail: string; approval: ApprovalRequest }

export function isApprovalRequired(value: unknown): value is ApprovalRequiredResponse {
  if (!value || typeof value !== 'object') return false
  const data = value as { status?: unknown; approval?: unknown }
  return data.status === 'approval_required' && !!data.approval && typeof data.approval === 'object'
    && typeof (data.approval as { approval_id?: unknown }).approval_id === 'string'
}

export const APPROVAL_ID_PATTERN = /^AR-[a-f0-9]{16}$/

// ----------------------------------------------- workflows (v3.2, UI v6.7)

export type WorkflowStatus = 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'
export type WorkflowStepStatus = 'pending' | 'running' | 'completed' | 'failed' | 'skipped'
export type WorkflowStepType = 'tool' | 'assistant' | 'transform' | 'noop'
export interface WorkflowStepDefinition {
  step_id: string
  name: string
  step_type: WorkflowStepType
  action: string
  parameters: Record<string, unknown>
  depends_on: string[]
}
export interface WorkflowDefinition {
  id: string
  owner_id: string
  name: string
  description: string | null
  dag_spec: { steps: WorkflowStepDefinition[] }
  created_at: string
  updated_at: string
}
export interface WorkflowDefinitionPage { items: WorkflowDefinition[]; total: number }
export interface WorkflowDefinitionCreate {
  name: string
  description?: string | null
  dag_spec: { steps: (Pick<WorkflowStepDefinition, 'step_id' | 'name'> & Partial<WorkflowStepDefinition>)[] }
}
export interface WorkflowRun {
  id: string
  workflow_id: string | null
  owner_id: string
  status: WorkflowStatus
  inputs: Record<string, unknown>
  outputs: Record<string, unknown>
  error_message: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
}
export interface WorkflowStepRun {
  id: string
  run_id: string
  step_id: string
  step_name: string
  status: WorkflowStepStatus
  inputs: Record<string, unknown>
  outputs: Record<string, unknown>
  error_message: string | null
  recovery_metadata: Record<string, unknown>
  started_at: string | null
  completed_at: string | null
}
export interface WorkflowRunPage { items: WorkflowRun[]; total: number }
export interface WorkflowRunDetail { run: WorkflowRun; steps: WorkflowStepRun[] }
export interface WorkflowRunTrigger { inputs?: Record<string, unknown>; approval_id?: string; wait?: boolean }

export function listWorkflows(limit: number, offset: number, auth?: ApiAuth): Promise<WorkflowDefinitionPage> {
  const query = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  return request<WorkflowDefinitionPage>(`/workflows/definitions?${query.toString()}`, undefined, false, auth)
}

export function createWorkflow(body: WorkflowDefinitionCreate, auth?: ApiAuth): Promise<WorkflowDefinition> {
  return request<WorkflowDefinition>('/workflows/definitions', json(body), false, auth)
}

export function deleteWorkflow(workflowId: string, auth?: ApiAuth): Promise<void> {
  return request<void>(`/workflows/definitions/${encodeURIComponent(workflowId)}`, { method: 'DELETE' }, false, auth)
}

/** 201 with the run, or 202 when the workflow has write steps and needs an approved request (nothing ran). */
export function runWorkflow(workflowId: string, body: WorkflowRunTrigger, auth?: ApiAuth): Promise<WorkflowRunDetail | ApprovalRequiredResponse> {
  return request<WorkflowRunDetail | ApprovalRequiredResponse>(`/workflows/definitions/${encodeURIComponent(workflowId)}/run`, json(body), false, auth)
}

export function listWorkflowRuns(
  options: { workflowId?: string; status?: WorkflowStatus; limit?: number; offset?: number } = {}, auth?: ApiAuth,
): Promise<WorkflowRunPage> {
  const query = new URLSearchParams({ limit: String(options.limit ?? 50), offset: String(options.offset ?? 0) })
  if (options.workflowId) query.set('workflow_id', options.workflowId)
  if (options.status) query.set('status', options.status)
  return request<WorkflowRunPage>(`/workflows/runs?${query.toString()}`, undefined, false, auth)
}

export function getWorkflowRun(runId: string, auth?: ApiAuth): Promise<WorkflowRunDetail> {
  return request<WorkflowRunDetail>(`/workflows/runs/${encodeURIComponent(runId)}`, undefined, false, auth)
}

export function cancelWorkflowRun(runId: string, auth?: ApiAuth): Promise<WorkflowRunDetail> {
  return request<WorkflowRunDetail>(`/workflows/runs/${encodeURIComponent(runId)}/cancel`, { method: 'POST' }, false, auth)
}

// ----------------------------------------------- scheduler jobs (v3.5, UI v6.7)

export type ScheduleType = 'cron' | 'interval' | 'once'
export type JobLastStatus = 'idle' | 'running' | 'success' | 'failure'
export interface ScheduledJob {
  id: string
  owner_id: string
  name: string
  schedule_type: ScheduleType
  schedule_expr: string
  action_type: string
  payload: Record<string, unknown>
  enabled: boolean
  next_run_at: string | null
  last_run_at: string | null
  last_status: JobLastStatus
  created_at: string
  updated_at: string
}
export interface ScheduledJobPage { items: ScheduledJob[]; total: number }
export interface JobCreate {
  name: string
  schedule_type: ScheduleType
  schedule_expr: string
  action_type: string
  payload?: Record<string, unknown>
  enabled?: boolean
}
export type JobUpdate = Partial<JobCreate>
export interface JobRunResult {
  job_id: string
  status: 'success' | 'failure'
  started_at: string
  completed_at: string
  output: Record<string, unknown>
  error: string | null
}

export function listJobs(limit: number, offset: number, auth?: ApiAuth): Promise<ScheduledJobPage> {
  const query = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  return request<ScheduledJobPage>(`/scheduler/jobs?${query.toString()}`, undefined, false, auth)
}

export function createJob(body: JobCreate, auth?: ApiAuth): Promise<ScheduledJob> {
  return request<ScheduledJob>('/scheduler/jobs', json(body), false, auth)
}

export function updateJob(jobId: string, body: JobUpdate, auth?: ApiAuth): Promise<ScheduledJob> {
  return request<ScheduledJob>(`/scheduler/jobs/${encodeURIComponent(jobId)}`, { ...json(body), method: 'PATCH' }, false, auth)
}

export function deleteJob(jobId: string, auth?: ApiAuth): Promise<void> {
  return request<void>(`/scheduler/jobs/${encodeURIComponent(jobId)}`, { method: 'DELETE' }, false, auth)
}

export function runJob(jobId: string, auth?: ApiAuth): Promise<JobRunResult> {
  return request<JobRunResult>(`/scheduler/jobs/${encodeURIComponent(jobId)}/run`, { method: 'POST' }, false, auth)
}

// ----------------------------------------------- integrations (v4.8, UI v6.7)

export type EnrollmentState = 'pending' | 'active' | 'revoked'
export interface AdapterAction { name: string; read_only: boolean; description: string }
export interface AdapterDescriptor {
  adapter_id: string
  name: string
  mode: 'mock'
  requires_secret: boolean
  actions: AdapterAction[]
  limitations: string[]
}
export interface Enrollment {
  enrollment_id: string
  owner_id: string
  adapter_id: string
  display_name: string
  /** A reference such as env:JARVIS_INTEGRATION_LAMP; never the secret value. */
  secret_ref: string | null
  state: EnrollmentState
  created_at: string
  activated_at: string | null
  revoked_at: string | null
  last_action_at: string | null
  durable: boolean
}
export interface EnrollmentView { enrollment: Enrollment; secret_status: 'not_required' | 'present' | 'missing' }
export interface EnrollmentPage { durable_storage: boolean; enrollments: EnrollmentView[] }
export interface EnrollmentCreate { adapter_id: string; display_name: string; secret_ref?: string | null }
export interface IntegrationActionRequest { action: string; arguments: Record<string, unknown>; approval_id?: string }
export interface IntegrationActionResult {
  enrollment_id: string
  action: string
  read_only: boolean
  mode: 'mock'
  output: Record<string, unknown>
  executed_at: string
  approval_id: string | null
}

export const SECRET_REF_PATTERN = /^env:JARVIS_INTEGRATION_[A-Z0-9_]{1,64}$/

export function listAdapters(auth?: ApiAuth): Promise<AdapterDescriptor[]> {
  return request<AdapterDescriptor[]>('/integrations/adapters', undefined, false, auth)
}

export function listEnrollments(limit: number, auth?: ApiAuth): Promise<EnrollmentPage> {
  return request<EnrollmentPage>(`/integrations/enrollments?limit=${encodeURIComponent(String(limit))}`, undefined, false, auth)
}

export function createEnrollment(body: EnrollmentCreate, auth?: ApiAuth): Promise<EnrollmentView> {
  return request<EnrollmentView>('/integrations/enrollments', json(body), false, auth)
}

/** Activation always needs an approved request: the first call returns 202, the repeat carries approval_id. */
export function activateEnrollment(enrollmentId: string, approvalId: string | undefined, auth?: ApiAuth): Promise<EnrollmentView | ApprovalRequiredResponse> {
  const query = approvalId ? `?${new URLSearchParams({ approval_id: approvalId }).toString()}` : ''
  return request<EnrollmentView | ApprovalRequiredResponse>(`/integrations/enrollments/${encodeURIComponent(enrollmentId)}/activate${query}`, { method: 'POST' }, false, auth)
}

export function revokeEnrollment(enrollmentId: string, auth?: ApiAuth): Promise<EnrollmentView> {
  return request<EnrollmentView>(`/integrations/enrollments/${encodeURIComponent(enrollmentId)}/revoke`, { method: 'POST' }, false, auth)
}

/** Read-only actions run at once; state-changing ones return 202 until repeated with an approved approval_id. */
export function runIntegrationAction(enrollmentId: string, body: IntegrationActionRequest, auth?: ApiAuth): Promise<IntegrationActionResult | ApprovalRequiredResponse> {
  return request<IntegrationActionResult | ApprovalRequiredResponse>(`/integrations/enrollments/${encodeURIComponent(enrollmentId)}/actions`, json(body), false, auth)
}

// ------------------------------------------------- unified interface (v3.0)

export interface InterfaceConfig {
  contract_version: '1'
  features: { chat: boolean; memory: boolean; coding: boolean; browser: boolean; tools: boolean; chat_tools: boolean; activity: boolean; approval_execution: false }
  voice: { enabled: boolean; mode: 'browser_native'; server_receives_audio: false; auto_send: false; requires_opt_in: true }
}

export function getInterfaceConfig(): Promise<InterfaceConfig> {
  return request<InterfaceConfig>('/interface/config')
}

// ------------------------------------------------- assistant (v3.1)

export type TaskStatus = 'pending' | 'in_progress' | 'completed' | 'cancelled'
export type TaskPriority = 'low' | 'medium' | 'high' | 'urgent'
export type ReminderStatus = 'pending' | 'triggered' | 'acknowledged' | 'dismissed'

export interface TaskItem {
  id: string
  owner_id: string
  title: string
  description: string | null
  status: TaskStatus
  priority: TaskPriority
  due_date: string | null
  tags: string[]
  created_at: string
  updated_at: string
}

export interface TaskPage {
  items: TaskItem[]
  total: number
}

export interface NoteItem {
  id: string
  owner_id: string
  title: string
  content: string
  tags: string[]
  created_at: string
  updated_at: string
}

export interface NotePage {
  items: NoteItem[]
  total: number
}

export interface ReminderItem {
  id: string
  owner_id: string
  task_id: string | null
  title: string
  remind_at: string
  status: ReminderStatus
  created_at: string
  updated_at: string
}

export interface ReminderPage {
  items: ReminderItem[]
  total: number
}

export interface CalendarEventItem {
  id: string
  owner_id: string
  title: string
  description: string | null
  start_time: string
  end_time: string
  location: string | null
  created_at: string
  updated_at: string
}

export interface CalendarEventPage {
  items: CalendarEventItem[]
  total: number
}

export function listTasks(
  auth?: ApiAuth,
  status?: TaskStatus,
  priority?: TaskPriority,
  tag?: string,
  limit = 50,
  offset = 0,
): Promise<TaskPage> {
  const query = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  if (status) query.set('status', status)
  if (priority) query.set('priority', priority)
  if (tag) query.set('tag', tag)
  return request<TaskPage>(`/assistant/tasks?${query.toString()}`, undefined, false, auth)
}

export function createTask(
  body: { title: string; description?: string; priority?: TaskPriority; due_date?: string; tags?: string[] },
  auth?: ApiAuth,
): Promise<TaskItem> {
  return request<TaskItem>('/assistant/tasks', json(body), false, auth)
}

export function updateTask(
  taskId: string,
  body: { title?: string; description?: string; status?: TaskStatus; priority?: TaskPriority; due_date?: string; tags?: string[] },
  auth?: ApiAuth,
): Promise<TaskItem> {
  return request<TaskItem>(`/assistant/tasks/${encodeURIComponent(taskId)}`, { ...json(body), method: 'PATCH' }, false, auth)
}

export function deleteTask(taskId: string, auth?: ApiAuth): Promise<void> {
  return request<void>(`/assistant/tasks/${encodeURIComponent(taskId)}`, { method: 'DELETE' }, false, auth)
}

export function listNotes(
  auth?: ApiAuth,
  queryParam?: string,
  tag?: string,
  limit = 50,
  offset = 0,
): Promise<NotePage> {
  const query = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  if (queryParam) query.set('query', queryParam)
  if (tag) query.set('tag', tag)
  return request<NotePage>(`/assistant/notes?${query.toString()}`, undefined, false, auth)
}

export function createNote(
  body: { title: string; content: string; tags?: string[] },
  auth?: ApiAuth,
): Promise<NoteItem> {
  return request<NoteItem>('/assistant/notes', json(body), false, auth)
}

export function updateNote(
  noteId: string,
  body: { title?: string; content?: string; tags?: string[] },
  auth?: ApiAuth,
): Promise<NoteItem> {
  return request<NoteItem>(`/assistant/notes/${encodeURIComponent(noteId)}`, { ...json(body), method: 'PATCH' }, false, auth)
}

export function deleteNote(noteId: string, auth?: ApiAuth): Promise<void> {
  return request<void>(`/assistant/notes/${encodeURIComponent(noteId)}`, { method: 'DELETE' }, false, auth)
}

export function listReminders(
  auth?: ApiAuth,
  status?: ReminderStatus,
  limit = 50,
  offset = 0,
): Promise<ReminderPage> {
  const query = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  if (status) query.set('status', status)
  return request<ReminderPage>(`/assistant/reminders?${query.toString()}`, undefined, false, auth)
}

export function createReminder(
  body: { title: string; remind_at: string; task_id?: string },
  auth?: ApiAuth,
): Promise<ReminderItem> {
  return request<ReminderItem>('/assistant/reminders', json(body), false, auth)
}

export function updateReminder(
  reminderId: string,
  body: { title?: string; remind_at?: string; status?: ReminderStatus },
  auth?: ApiAuth,
): Promise<ReminderItem> {
  return request<ReminderItem>(`/assistant/reminders/${encodeURIComponent(reminderId)}`, { ...json(body), method: 'PATCH' }, false, auth)
}

export function deleteReminder(reminderId: string, auth?: ApiAuth): Promise<void> {
  return request<void>(`/assistant/reminders/${encodeURIComponent(reminderId)}`, { method: 'DELETE' }, false, auth)
}

export function listCalendarEvents(
  startRange: string,
  endRange: string,
  auth?: ApiAuth,
  limit = 100,
): Promise<CalendarEventPage> {
  const query = new URLSearchParams({ start_range: startRange, end_range: endRange, limit: String(limit) })
  return request<CalendarEventPage>(`/assistant/calendar/events?${query.toString()}`, undefined, false, auth)
}

export function createCalendarEvent(
  body: { title: string; start_time: string; end_time: string; description?: string; location?: string },
  auth?: ApiAuth,
): Promise<CalendarEventItem> {
  return request<CalendarEventItem>('/assistant/calendar/events', json(body), false, auth)
}

export function deleteCalendarEvent(eventId: string, auth?: ApiAuth): Promise<void> {
  return request<void>(`/assistant/calendar/events/${encodeURIComponent(eventId)}`, { method: 'DELETE' }, false, auth)
}

