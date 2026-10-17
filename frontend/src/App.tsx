import { useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import {
  Activity, ArrowDown, ArrowUp, AudioLines, Check, ChevronDown, CircleHelp,
  Clock3, Command, Cpu, FileText, Folder, Menu, MessageSquare, Plus, Search,
  RefreshCw, Settings2, ShieldCheck, Sparkles, X,
} from 'lucide-react'
import { getConversationMessages, getReadiness, listConversations, sendChatMessage, type ApiAuth, type ChatMemoryContext } from './services/api'
import MemoryManager from './components/MemoryManager'

const MAX_MESSAGE_LENGTH = 4000

type Message = {
  id: string
  role: 'user' | 'assistant'
  content: string
  error?: boolean
  retryText?: string
  provider?: string
  demo?: boolean
  partial?: boolean
  memoryContext?: ChatMemoryContext | null
}

type Conversation = {
  id: string
  title: string
  updatedAt: number
  messages: Message[]
  persisted: boolean
}

const suggestions = [
  { icon: Command, title: 'Explain a concept', text: 'Explain a technical concept in simple terms' },
  { icon: Cpu, title: 'Debug some code', text: 'Help me debug a piece of code' },
  { icon: Search, title: 'Research a topic', text: 'Help me research a topic' },
  { icon: FileText, title: 'Summarize a document', text: 'Help me summarize a document' },
]

const navItems = [
  { label: 'Chat', icon: MessageSquare, active: true },
  { label: 'Capabilities', icon: Sparkles },
  { label: 'Memory', icon: Folder },
  { label: 'Activity', icon: Activity },
]

function createId() {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`
}

function formatTime(timestamp: number) {
  const date = new Date(timestamp)
  const today = new Date()
  const isToday = date.toDateString() === today.toDateString()
  return isToday ? 'Today' : date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

function SystemStatus() {
  type Status = 'checking' | 'ready' | 'degraded' | 'not_ready' | 'unavailable' | 'timeout'
  const [status, setStatus] = useState<Status>('checking')
  const controllerRef = useRef<AbortController | null>(null)
  const timerRef = useRef<number | null>(null)
  const mountedRef = useRef(false)

  const refresh = useCallback(async () => {
    controllerRef.current?.abort()
    if (timerRef.current !== null) window.clearTimeout(timerRef.current)
    const controller = new AbortController()
    controllerRef.current = controller
    setStatus('checking')
    timerRef.current = window.setTimeout(() => controller.abort(), 4000)
    try {
      const readiness = await getReadiness({ signal: controller.signal })
      if (mountedRef.current && controllerRef.current === controller) setStatus(readiness.status)
    } catch {
      if (mountedRef.current && controllerRef.current === controller) {
        setStatus(controller.signal.aborted ? 'timeout' : 'unavailable')
      }
    } finally {
      if (controllerRef.current === controller) {
        if (timerRef.current !== null) window.clearTimeout(timerRef.current)
        timerRef.current = null
        controllerRef.current = null
      }
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    void refresh()
    return () => {
      mountedRef.current = false
      controllerRef.current?.abort()
      if (timerRef.current !== null) window.clearTimeout(timerRef.current)
      controllerRef.current = null
      timerRef.current = null
    }
  }, [refresh])

  const labels: Record<Status, string> = {
    checking: 'Checking system',
    ready: 'System ready',
    degraded: 'System degraded',
    not_ready: 'System not ready',
    unavailable: 'Status unavailable',
    timeout: 'Status check timed out',
  }

  return <div className={`system-status system-status-${status}`} data-status={status}>
    <span className="system-status-message" role="status" aria-live="polite">
      <span className="status-ring" aria-hidden="true">{status === 'ready' ? <Check size={11} /> : <span />}</span>
      <span>{labels[status]}</span>
    </span>
    <button type="button" className="system-status-refresh" aria-label="Refresh system status" onClick={() => void refresh()} disabled={status === 'checking'}>
      <RefreshCw size={12} />
    </button>
  </div>
}

export default function App() {
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [pending, setPending] = useState(false)
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false)
  const [notice, setNotice] = useState('')
  const [activeView, setActiveView] = useState<'Chat' | 'Memory'>('Chat')
  const [inspectMemoryIds, setInspectMemoryIds] = useState<string[]>([])
  const [providerInfo, setProviderInfo] = useState<{ provider: string; demo: boolean } | null>(null)
  const [credential, setCredential] = useState('')
  const [credentialDraft, setCredentialDraft] = useState('')
  const [unlockOpen, setUnlockOpen] = useState(false)
  const [conversationListLoading, setConversationListLoading] = useState(false)
  const [conversationListError, setConversationListError] = useState('')
  const [conversationListHasMore, setConversationListHasMore] = useState(false)
  const [conversationListOffset, setConversationListOffset] = useState(0)
  const [historyLoading, setHistoryLoading] = useState(false)
  const [historyError, setHistoryError] = useState('')
  const [historyHasMore, setHistoryHasMore] = useState(false)
  const [historyBeforeSequence, setHistoryBeforeSequence] = useState<number | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const requestInFlightRef = useRef(false)
  const activeIdRef = useRef<string | null>(null)
  const sessionVersionRef = useRef(0)
  const sessionControllerRef = useRef(new AbortController())

  const setActiveConversationId = useCallback((id: string | null) => {
    activeIdRef.current = id
    setActiveId(id)
  }, [])

  const handleUnauthorized = useCallback(() => {
    sessionVersionRef.current += 1
    sessionControllerRef.current.abort()
    sessionControllerRef.current = new AbortController()
    requestInFlightRef.current = false
    activeIdRef.current = null
    setCredential('')
    setCredentialDraft('')
    setDraft('')
    setConversations([])
    setActiveId(null)
    setInspectMemoryIds([])
    setActiveView('Chat')
    setProviderInfo(null)
    setMobileMenuOpen(false)
    setNotice('')
    setConversationListLoading(false)
    setConversationListError('')
    setConversationListHasMore(false)
    setConversationListOffset(0)
    setHistoryError('')
    setHistoryLoading(false)
    setHistoryHasMore(false)
    setHistoryBeforeSequence(null)
    setPending(false)
    setUnlockOpen(true)
  }, [])

  const currentAuth = useCallback((): ApiAuth => ({
    token: credential || undefined,
    onUnauthorized: handleUnauthorized,
    signal: sessionControllerRef.current.signal,
  }), [credential, handleUnauthorized])

  function lockWorkspace() {
    handleUnauthorized()
  }

  const shortcutLabel = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/i.test(navigator.platform)
    ? '⌘ K'
    : 'Ctrl K'

  const activeConversation = conversations.find((conversation) => conversation.id === activeId) ?? null
  const messages = activeConversation?.messages ?? []

  useEffect(() => {
    if (messages.length) bottomRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'end' })
  }, [messages, pending])

  const newConversation = useCallback(() => {
    setActiveConversationId(null)
    setDraft('')
    setMobileMenuOpen(false)
    setNotice('')
    setHistoryLoading(false)
    setHistoryError('')
    setHistoryHasMore(false)
    setHistoryBeforeSequence(null)
    setActiveView('Chat')
    inputRef.current?.focus()
  }, [setActiveConversationId])

  const loadConversationPage = useCallback(async (offset: number, append = false) => {
    if (!credential) return
    const version = sessionVersionRef.current
    setConversationListLoading(true)
    setConversationListError('')
    try {
      const page = await listConversations({ limit: 20, offset }, currentAuth())
      if (version !== sessionVersionRef.current) return
      setConversations((current) => {
        const local = current.filter((item) => !item.persisted)
        const existing = new Map(current.filter((item) => item.persisted).map((item) => [item.id, item]))
        const returned = page.items.map((item) => {
          const cached = existing.get(item.conversation_id)
          return {
            id: item.conversation_id,
            title: item.preview ?? 'Empty conversation',
            updatedAt: Date.parse(item.last_activity_at),
            messages: cached?.messages ?? [],
            persisted: true,
          }
        })
        if (!append) return [...local, ...returned]
        const merged = new Map([...current.filter((item) => item.persisted), ...returned].map((item) => [item.id, item]))
        return [...local, ...merged.values()]
      })
      setConversationListOffset(page.offset + page.limit)
      setConversationListHasMore(page.has_more)
    } catch (cause) {
      if (version === sessionVersionRef.current) setConversationListError(cause instanceof Error ? cause.message : 'Could not load saved conversations.')
    } finally {
      if (version === sessionVersionRef.current) setConversationListLoading(false)
    }
  }, [credential, currentAuth])

  useEffect(() => {
    if (!credential) {
      setConversationListLoading(false)
      return
    }
    void loadConversationPage(0)
  }, [credential, loadConversationPage])

  async function loadConversationHistory(conversationId: string, beforeSequence?: number) {
    const version = sessionVersionRef.current
    setHistoryLoading(true)
    setHistoryError('')
    try {
      const page = await getConversationMessages(conversationId, { limit: 100, beforeSequence }, currentAuth())
      if (version !== sessionVersionRef.current || activeIdRef.current !== conversationId) return
      const loaded: Message[] = page.messages.map((message) => ({
        id: message.id, role: message.role, content: message.content,
      }))
      setConversations((current) => current.map((item) => item.id === conversationId
        ? { ...item, messages: beforeSequence === undefined ? loaded : [...loaded, ...item.messages] }
        : item))
      setHistoryHasMore(page.has_more)
      setHistoryBeforeSequence(page.next_before_sequence)
    } catch (cause) {
      if (version === sessionVersionRef.current && activeIdRef.current === conversationId) {
        setHistoryError(cause instanceof Error ? cause.message : 'Could not load this conversation.')
      }
    } finally {
      if (version === sessionVersionRef.current && activeIdRef.current === conversationId) setHistoryLoading(false)
    }
  }

  function selectConversation(conversation: Conversation) {
    setActiveConversationId(conversation.id)
    setActiveView('Chat')
    setMobileMenuOpen(false)
    setNotice('')
    setHistoryLoading(false)
    setHistoryError('')
    setHistoryHasMore(false)
    setHistoryBeforeSequence(null)
    if (conversation.persisted) void loadConversationHistory(conversation.id)
  }

  useEffect(() => {
    function handleGlobalKeyDown(event: globalThis.KeyboardEvent) {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        newConversation()
      }
    }

    window.addEventListener('keydown', handleGlobalKeyDown)
    return () => window.removeEventListener('keydown', handleGlobalKeyDown)
  }, [newConversation])

  async function submitMessage(rawText: string, retryMessageId?: string) {
    const text = rawText.trim()
    if (!text) return
    if (text.length > MAX_MESSAGE_LENGTH) {
      setNotice(`Messages are limited to ${MAX_MESSAGE_LENGTH.toLocaleString()} characters.`)
      return
    }
    if (requestInFlightRef.current || pending) return

    requestInFlightRef.current = true
    setPending(true)
    const version = sessionVersionRef.current
    let conversationId = activeId
    let conversationIsPersisted = conversations.find((item) => item.id === conversationId)?.persisted ?? false
    try {
      if (!conversationId) {
        conversationId = createId()
        const initial: Conversation = {
          id: conversationId,
          title: text.slice(0, 44),
          updatedAt: Date.now(),
          messages: [{ id: createId(), role: 'user', content: text }],
          persisted: false,
        }
        setConversations((current) => [initial, ...current])
        setActiveConversationId(conversationId)
      } else if (retryMessageId) {
        setConversations((current) => current.map((conversation) => conversation.id === conversationId
          ? { ...conversation, messages: conversation.messages.map((message) => message.id === retryMessageId ? { ...message, error: false } : message) }
          : conversation))
      } else {
        setConversations((current) => current.map((conversation) => conversation.id === conversationId
          ? { ...conversation, title: conversation.messages.length === 0 ? text.slice(0, 44) : conversation.title, updatedAt: Date.now(), messages: [...conversation.messages, { id: createId(), role: 'user', content: text }] }
          : conversation))
      }

      setDraft('')
      setNotice('')
      const auth = currentAuth()
      const result = conversationIsPersisted && conversationId
        ? await sendChatMessage(text, auth, conversationId)
        : await sendChatMessage(text, auth)
      if (version !== sessionVersionRef.current) return
      setProviderInfo({ provider: result.provider, demo: result.demo })
      const persistedId = result.conversation_id || conversationId
      setConversations((current) => current.map((conversation) => conversation.id === conversationId
        ? { ...conversation, id: persistedId, persisted: Boolean(result.conversation_id) || conversation.persisted, updatedAt: Date.now(), messages: [...conversation.messages.filter((message) => !(retryMessageId && message.id === retryMessageId)), { id: createId(), role: 'assistant', content: result.reply, provider: result.provider, demo: result.demo, partial: result.partial, memoryContext: result.memory_context }] }
        : conversation))
      if (activeIdRef.current === conversationId) setActiveConversationId(persistedId)
      if (result.conversation_id && credential) void loadConversationPage(0)
    } catch (error) {
      if (version !== sessionVersionRef.current) return
      const detail = error instanceof Error ? error.message : 'Something went wrong. Please try again.'
      const failedMessage: Message = { id: retryMessageId ?? createId(), role: 'assistant', content: detail, error: true, retryText: text }
      setConversations((current) => current.map((conversation) => conversation.id === conversationId
        ? { ...conversation, updatedAt: Date.now(), messages: retryMessageId
          ? conversation.messages.map((message) => message.id === retryMessageId ? failedMessage : message)
          : [...conversation.messages, failedMessage] }
        : conversation))
    } finally {
      if (version === sessionVersionRef.current) {
        requestInFlightRef.current = false
        setPending(false)
        inputRef.current?.focus()
      }
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    void submitMessage(draft)
  }

  function handleComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      void submitMessage(draft)
    }
  }

  function selectNav(label: string) {
    if (label === 'Chat' || label === 'Memory') {
      setActiveView(label)
      setNotice('')
      setMobileMenuOpen(false)
      return
    }
    setNotice(`${label} is coming soon. This section is not available in this version.`)
    setMobileMenuOpen(false)
  }

  const sortedConversations = [...conversations].sort((a, b) => b.updatedAt - a.updatedAt || b.id.localeCompare(a.id))

  return (
    <div className="app-shell">
      {mobileMenuOpen && <button className="mobile-scrim" aria-label="Close navigation" onClick={() => setMobileMenuOpen(false)} />}
      <aside className={`sidebar ${mobileMenuOpen ? 'sidebar-open' : ''}`} aria-label="Main navigation">
        <div className="brand-row">
          <div className="brand-mark" aria-hidden="true"><span>J</span><i /></div>
          <div className="brand-copy"><span className="brand-name">JARVIS</span><span className="brand-caption">PERSONAL INTELLIGENCE</span></div>
          <button className="icon-button sidebar-close" aria-label="Close navigation" onClick={() => setMobileMenuOpen(false)}><X size={18} /></button>
        </div>

        <button className="new-chat-button" onClick={newConversation}>
          <Plus size={17} strokeWidth={2.2} /><span>New conversation</span><span className="shortcut">{shortcutLabel}</span>
        </button>

        <div className="sidebar-section-label">WORKSPACE</div>
        <nav className="primary-nav">
          {navItems.map(({ label, icon: Icon }) => {
            const active = label === activeView
            const available = label === 'Chat' || label === 'Memory'
            return <button key={label} className={`nav-item ${active ? 'nav-active' : ''}`} onClick={() => selectNav(label)} aria-current={active ? 'page' : undefined}>
              <Icon size={17} strokeWidth={1.8} /><span>{label}</span>{!available && <span className="nav-soon">SOON</span>}
            </button>
          })}
        </nav>

        <div className="history-heading"><span className="sidebar-section-label">RECENT</span><ChevronDown size={14} /></div>
        <div className="history-list">
          {!credential && <button className="history-unlock" type="button" onClick={() => setUnlockOpen(true)}>Unlock to load saved conversations</button>}
          {conversationListLoading && <p className="history-status" role="status">Loading saved conversations…</p>}
          {conversationListError && <div className="history-error" role="alert"><span>{conversationListError}</span><button type="button" onClick={() => void loadConversationPage(0)}>Retry</button></div>}
          {sortedConversations.length ? sortedConversations.map((conversation) => (
            <button key={conversation.id} className={`history-item ${activeId === conversation.id ? 'history-active' : ''}`} onClick={() => selectConversation(conversation)} title={conversation.title}>
              <MessageSquare size={15} /><span className="history-title">{conversation.title}</span><span className="history-date">{formatTime(conversation.updatedAt)}</span>
            </button>
          )) : !conversationListLoading && !conversationListError && <p className="history-empty">{credential ? 'No saved conversations yet.' : 'Your conversations will appear here.'}</p>}
          {conversationListHasMore && <button className="history-load-more" type="button" disabled={conversationListLoading} onClick={() => void loadConversationPage(conversationListOffset, true)}>Load more</button>}
        </div>

        <div className="sidebar-spacer" />
        <div className="sidebar-footer">
          <div className="safety-note"><ShieldCheck size={15} /><span>Local workspace</span></div>
          <button className="nav-item settings-item" onClick={() => selectNav('Settings')}><Settings2 size={17} /><span>Settings</span><span className="nav-soon">SOON</span></button>
          <div className="profile-row"><div className="avatar">A</div><div className="profile-copy"><strong>Local session</strong><span>Personal workspace</span></div><button className="icon-button help-button" aria-label="About JARVIS" onClick={() => setNotice('JARVIS uses the provider configured by the backend. Mock mode is deterministic; model providers are external.')}><CircleHelp size={17} /></button></div>
        </div>
      </aside>

      <main className="main-panel">
        <header className="topbar">
          <div className="topbar-left"><button className="icon-button mobile-menu-button" aria-label="Open navigation" onClick={() => setMobileMenuOpen(true)}><Menu size={20} /></button><div className="breadcrumb"><span>Workspace</span><span className="breadcrumb-sep">/</span><strong>{activeView}</strong></div></div>
          <div className="topbar-right"><div className="provider-pill"><span className="provider-dot" /><span>{providerInfo ? `${providerInfo.demo ? 'DEMO' : 'MODEL'} · ${providerInfo.provider.toUpperCase()}` : 'PROVIDER MODE'}</span></div>{credential && <button type="button" className="workspace-lock" onClick={lockWorkspace}>Lock workspace</button>}<span className="topbar-divider" /><SystemStatus /></div>
        </header>

        {activeView === 'Memory' ? <div className="content-area"><MemoryManager inspectIds={inspectMemoryIds} auth={{ token: credential || undefined, onUnauthorized: handleUnauthorized }} /></div> : <><div className={`content-area ${messages.length ? 'has-messages' : ''}`}>
          {notice && <div className="notice-banner" role="status"><CircleHelp size={16} /><span>{notice}</span><button className="icon-button" aria-label="Dismiss message" onClick={() => setNotice('')}><X size={15} /></button></div>}
          {historyError && <div className="notice-banner conversation-error" role="alert"><CircleHelp size={16} /><span>{historyError}</span><button className="icon-button" aria-label="Dismiss conversation error" onClick={() => setHistoryError('')}><X size={15} /></button></div>}
          {historyLoading && <div className="notice-banner" role="status">Loading conversation history…</div>}
          {!messages.length ? (
            <section className="welcome-view" aria-label="Welcome to JARVIS">
              <div className="welcome-orb" aria-hidden="true"><div className="orb-core"><AudioLines size={29} strokeWidth={1.5} /></div><span className="orb-ring ring-one" /><span className="orb-ring ring-two" /><span className="orb-spark spark-one" /><span className="orb-spark spark-two" /></div>
              <div className="eyebrow"><span /> YOUR PERSONAL AI WORKSPACE</div>
              <h1>Good to have you<br /><span>here, Ajay.</span></h1>
              <p className="welcome-subtitle">I’m JARVIS. Your workspace is ready.<br className="desktop-break" /> What would you like to explore today?</p>
              <div className="demo-disclaimer"><Sparkles size={14} /><span>The backend selects the response provider. Mock mode is local and deterministic; model providers require configuration.</span></div>
              <div className="suggestion-grid" aria-label="Suggested prompts">
                {suggestions.map(({ icon: Icon, title, text }) => <button key={title} className="suggestion-card" onClick={() => { setDraft(text); inputRef.current?.focus() }}><span className="suggestion-icon"><Icon size={17} strokeWidth={1.8} /></span><span className="suggestion-text"><strong>{title}</strong><small>{text}</small></span><ArrowDown size={14} className="suggestion-arrow" /></button>)}
              </div>
            </section>
          ) : (
            <section className="conversation-view" aria-label="Conversation">
              <div className="conversation-meta"><span className="conversation-line" /><span>SESSION CONVERSATION</span><span className="conversation-line" /></div>
              <div className="message-list">
                {historyHasMore && activeConversation?.persisted && <button className="history-load-older" type="button" disabled={historyLoading} onClick={() => activeConversation && historyBeforeSequence !== null && void loadConversationHistory(activeConversation.id, historyBeforeSequence)}>Load older messages</button>}
                {messages.map((message) => <article key={message.id} className={`message-row ${message.role === 'user' ? 'message-user' : 'message-assistant'}`}>
                  {message.role === 'assistant' && <div className="message-avatar"><span>J</span></div>}
                  <div className="message-content-wrap"><div className="message-label">{message.role === 'user' ? 'YOU' : 'JARVIS'}{message.role === 'assistant' && message.provider && <span className="message-demo-tag">{message.demo ? 'DEMO' : 'MODEL'} · {message.provider.toUpperCase()}</span>}{message.partial && <span className="message-partial-tag">PARTIAL RESULT</span>}</div><div className={`message-bubble ${message.error ? 'message-error' : ''}`}>{message.content}</div>{message.error && <small className="retry-warning">A manual retry may duplicate this exchange if the server saved it but the response was lost.</small>}{message.memoryContext && <button className="memory-context-link" type="button" onClick={() => { setInspectMemoryIds(message.memoryContext?.memory_ids ?? []); setActiveView('Memory') }}>Memory context · {message.memoryContext.outcome} · {message.memoryContext.memory_ids.length} record(s){message.memoryContext.memory_ids.length > 0 ? ` · ${message.memoryContext.memory_ids.join(', ')}` : ''}</button>}{message.error && <button className="retry-button" onClick={() => void submitMessage(message.retryText ?? '', message.id)}><Activity size={13} /> Try again</button>}</div>
                  {message.role === 'user' && <div className="user-avatar">A</div>}
                </article>)}
                {pending && <div className="message-row message-assistant" role="status" aria-label="JARVIS is responding"><div className="message-avatar"><span>J</span></div><div className="message-content-wrap"><div className="message-label">JARVIS</div><div className="typing-bubble"><span /><span /><span /><small>Thinking</small></div></div></div>}
                <div ref={bottomRef} />
              </div>
            </section>
          )}
        </div>

        <div className="composer-dock">
          <form className="composer" onSubmit={handleSubmit}>
            <textarea ref={inputRef} aria-label="Message JARVIS" placeholder="Message JARVIS…" value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={handleComposerKeyDown} maxLength={MAX_MESSAGE_LENGTH} rows={1} disabled={pending} />
            <div className="composer-bottom"><div className="composer-hint"><span className="hint-key">↵</span> to send <span className="hint-separator">·</span> <span className="hint-key">⇧ ↵</span> for new line <span className="hint-separator">·</span> Provider mode is shown on each reply</div><div className="composer-actions"><span className="char-count">{draft.length > 0 ? `${draft.length}/${MAX_MESSAGE_LENGTH}` : ''}</span><button type="submit" className="send-button" aria-label="Send message" disabled={!draft.trim() || pending || draft.trim().length > MAX_MESSAGE_LENGTH}><ArrowUp size={18} strokeWidth={2.3} /></button></div></div>
          </form>
          <div className="composer-footer"><span><ShieldCheck size={12} /> Memory saving is explicit</span><span><Clock3 size={12} /> Chat history depends on backend persistence</span></div>
        </div></>}
      </main>
      {unlockOpen && <div className="credential-backdrop"><section className="credential-dialog" role="dialog" aria-modal="true" aria-labelledby="credential-title" aria-describedby="credential-help"><h2 id="credential-title">Unlock local workspace</h2><p id="credential-help">Enter the local access token configured for this JARVIS instance. It is kept in this page's memory only and must be entered again after reload.</p><form onSubmit={(event) => { event.preventDefault(); const token = credentialDraft.trim(); if (token.length < 32) return; sessionVersionRef.current += 1; sessionControllerRef.current = new AbortController(); setCredential(token); setCredentialDraft(''); setUnlockOpen(false) }}><label htmlFor="workspace-token">Local access token</label><input id="workspace-token" type="password" autoComplete="off" value={credentialDraft} onChange={(event) => setCredentialDraft(event.target.value)} autoFocus /><div className="credential-actions"><button type="submit" disabled={credentialDraft.trim().length < 32}>Unlock</button><button type="button" onClick={() => { setCredentialDraft(''); setUnlockOpen(false) }}>Continue without unlocking</button></div></form><p className="credential-warning">This single-owner local prototype is not approved for shared or public deployment. Plain HTTP is unsafe on untrusted networks, and XSS could expose the in-memory token.</p></section></div>}
    </div>
  )
}
