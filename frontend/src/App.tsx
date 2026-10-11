import { useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import {
  Activity, ArrowDown, ArrowUp, AudioLines, Check, ChevronDown, CircleHelp,
  Clock3, Command, Cpu, FileText, Folder, Menu, MessageSquare, Plus, Search,
  Settings2, ShieldCheck, Sparkles, X,
} from 'lucide-react'
import { sendChatMessage } from './services/api'

const MAX_MESSAGE_LENGTH = 4000

type Message = {
  id: string
  role: 'user' | 'assistant'
  content: string
  error?: boolean
  retryText?: string
  provider?: string
  demo?: boolean
}

type Conversation = {
  id: string
  title: string
  updatedAt: number
  messages: Message[]
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

export default function App() {
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [pending, setPending] = useState(false)
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false)
  const [notice, setNotice] = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const requestInFlightRef = useRef(false)

  const shortcutLabel = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/i.test(navigator.platform)
    ? '⌘ K'
    : 'Ctrl K'

  const activeConversation = conversations.find((conversation) => conversation.id === activeId) ?? null
  const messages = activeConversation?.messages ?? []

  useEffect(() => {
    if (messages.length) bottomRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'end' })
  }, [messages, pending])

  const newConversation = useCallback(() => {
    setActiveId(null)
    setDraft('')
    setMobileMenuOpen(false)
    setNotice('')
    inputRef.current?.focus()
  }, [])

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
    let conversationId = activeId
    try {
      if (!conversationId) {
        conversationId = createId()
        const initial: Conversation = {
          id: conversationId,
          title: text.slice(0, 44),
          updatedAt: Date.now(),
          messages: [{ id: createId(), role: 'user', content: text }],
        }
        setConversations((current) => [initial, ...current])
        setActiveId(conversationId)
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
      const result = await sendChatMessage(text)
      setConversations((current) => current.map((conversation) => conversation.id === conversationId
        ? { ...conversation, updatedAt: Date.now(), messages: [...conversation.messages.filter((message) => !(retryMessageId && message.id === retryMessageId)), { id: createId(), role: 'assistant', content: result.reply, provider: result.provider, demo: result.demo }] }
        : conversation))
    } catch (error) {
      const detail = error instanceof Error ? error.message : 'Something went wrong. Please try again.'
      const failedMessage: Message = { id: retryMessageId ?? createId(), role: 'assistant', content: detail, error: true, retryText: text }
      setConversations((current) => current.map((conversation) => conversation.id === conversationId
        ? { ...conversation, updatedAt: Date.now(), messages: retryMessageId
          ? conversation.messages.map((message) => message.id === retryMessageId ? failedMessage : message)
          : [...conversation.messages, failedMessage] }
        : conversation))
    } finally {
      requestInFlightRef.current = false
      setPending(false)
      inputRef.current?.focus()
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
    if (label === 'Chat') {
      setNotice('')
      return
    }
    setNotice(`${label} is coming soon. This section is not available in this version.`)
    setMobileMenuOpen(false)
  }

  const sortedConversations = [...conversations].sort((a, b) => b.updatedAt - a.updatedAt)

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
          {navItems.map(({ label, icon: Icon, active }) => (
            <button key={label} className={`nav-item ${active ? 'nav-active' : ''}`} onClick={() => selectNav(label)} aria-current={active ? 'page' : undefined}>
              <Icon size={17} strokeWidth={1.8} /><span>{label}</span>{!active && <span className="nav-soon">SOON</span>}
            </button>
          ))}
        </nav>

        <div className="history-heading"><span className="sidebar-section-label">RECENT</span><ChevronDown size={14} /></div>
        <div className="history-list">
          {sortedConversations.length ? sortedConversations.map((conversation) => (
            <button key={conversation.id} className={`history-item ${activeId === conversation.id ? 'history-active' : ''}`} onClick={() => { setActiveId(conversation.id); setMobileMenuOpen(false); setNotice('') }}>
              <MessageSquare size={15} /><span className="history-title">{conversation.title}</span><span className="history-date">{formatTime(conversation.updatedAt)}</span>
            </button>
          )) : <p className="history-empty">Your conversations will appear here.</p>}
        </div>

        <div className="sidebar-spacer" />
        <div className="sidebar-footer">
          <div className="safety-note"><ShieldCheck size={15} /><span>Your messages stay in this session</span></div>
          <button className="nav-item settings-item" onClick={() => selectNav('Settings')}><Settings2 size={17} /><span>Settings</span><span className="nav-soon">SOON</span></button>
          <div className="profile-row"><div className="avatar">A</div><div className="profile-copy"><strong>Local session</strong><span>Personal workspace</span></div><button className="icon-button help-button" aria-label="About JARVIS" onClick={() => setNotice('JARVIS is currently running with a deterministic mock provider.')}><CircleHelp size={17} /></button></div>
        </div>
      </aside>

      <main className="main-panel">
        <header className="topbar">
          <div className="topbar-left"><button className="icon-button mobile-menu-button" aria-label="Open navigation" onClick={() => setMobileMenuOpen(true)}><Menu size={20} /></button><div className="breadcrumb"><span>Workspace</span><span className="breadcrumb-sep">/</span><strong>Chat</strong></div></div>
          <div className="topbar-right"><div className="provider-pill"><span className="provider-dot" /><span>DEMO MODE</span></div><span className="topbar-divider" /><div className="system-status"><span className="status-ring"><Check size={11} /></span><span>System ready</span></div></div>
        </header>

        <div className={`content-area ${messages.length ? 'has-messages' : ''}`}>
          {notice && <div className="notice-banner" role="status"><CircleHelp size={16} /><span>{notice}</span><button className="icon-button" aria-label="Dismiss message" onClick={() => setNotice('')}><X size={15} /></button></div>}
          {!messages.length ? (
            <section className="welcome-view" aria-label="Welcome to JARVIS">
              <div className="welcome-orb" aria-hidden="true"><div className="orb-core"><AudioLines size={29} strokeWidth={1.5} /></div><span className="orb-ring ring-one" /><span className="orb-ring ring-two" /><span className="orb-spark spark-one" /><span className="orb-spark spark-two" /></div>
              <div className="eyebrow"><span /> YOUR PERSONAL AI WORKSPACE</div>
              <h1>Good to have you<br /><span>here, Ajay.</span></h1>
              <p className="welcome-subtitle">I’m JARVIS. Your workspace is ready.<br className="desktop-break" /> What would you like to explore today?</p>
              <div className="demo-disclaimer"><Sparkles size={14} /><span>Currently in demo mode — responses are generated by a local mock provider.</span></div>
              <div className="suggestion-grid" aria-label="Suggested prompts">
                {suggestions.map(({ icon: Icon, title, text }) => <button key={title} className="suggestion-card" onClick={() => { setDraft(text); inputRef.current?.focus() }}><span className="suggestion-icon"><Icon size={17} strokeWidth={1.8} /></span><span className="suggestion-text"><strong>{title}</strong><small>{text}</small></span><ArrowDown size={14} className="suggestion-arrow" /></button>)}
              </div>
            </section>
          ) : (
            <section className="conversation-view" aria-label="Conversation">
              <div className="conversation-meta"><span className="conversation-line" /><span>SESSION CONVERSATION</span><span className="conversation-line" /></div>
              <div className="message-list">
                {messages.map((message) => <article key={message.id} className={`message-row ${message.role === 'user' ? 'message-user' : 'message-assistant'}`}>
                  {message.role === 'assistant' && <div className="message-avatar"><span>J</span></div>}
                  <div className="message-content-wrap"><div className="message-label">{message.role === 'user' ? 'YOU' : 'JARVIS'}{message.demo && <span className="message-demo-tag">DEMO · {message.provider?.toUpperCase()}</span>}</div><div className={`message-bubble ${message.error ? 'message-error' : ''}`}>{message.content}</div>{message.error && <button className="retry-button" onClick={() => void submitMessage(message.retryText ?? '', message.id)}><Activity size={13} /> Try again</button>}</div>
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
            <div className="composer-bottom"><div className="composer-hint"><span className="hint-key">↵</span> to send <span className="hint-separator">·</span> <span className="hint-key">⇧ ↵</span> for new line <span className="hint-separator">·</span> Demo replies are not AI-generated</div><div className="composer-actions"><span className="char-count">{draft.length > 0 ? `${draft.length}/${MAX_MESSAGE_LENGTH}` : ''}</span><button type="submit" className="send-button" aria-label="Send message" disabled={!draft.trim() || pending || draft.trim().length > MAX_MESSAGE_LENGTH}><ArrowUp size={18} strokeWidth={2.3} /></button></div></div>
          </form>
          <div className="composer-footer"><span><ShieldCheck size={12} /> Private to this session</span><span><Clock3 size={12} /> No history is saved after closing</span></div>
        </div>
      </main>
    </div>
  )
}
