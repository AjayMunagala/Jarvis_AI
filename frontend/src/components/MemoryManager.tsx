import { useEffect, useRef, useState, type FormEvent } from 'react'
import { createMemory, deleteMemory, getMemory, ingestDocument, listMemories, searchMemories, updateMemory, type ApiAuth, type MemoryInput, type MemoryRecord } from '../services/api'

const PAGE_SIZE = 20
const emptyForm = { content: '', category: '', source_reference: '' }
type FormState = typeof emptyForm

export default function MemoryManager({ inspectIds = [], auth }: { inspectIds?: string[]; auth?: ApiAuth }) {
  const [items, setItems] = useState<MemoryRecord[]>([])
  const [total, setTotal] = useState(0)
  const [pageCount, setPageCount] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [offset, setOffset] = useState(0)
  const [categoryFilter, setCategoryFilter] = useState('')
  const [categoryInput, setCategoryInput] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [activeSearch, setActiveSearch] = useState('')
  const [form, setForm] = useState<FormState>(emptyForm)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<MemoryRecord | null>(null)
  const [importTarget, setImportTarget] = useState<File | null>(null)
  const [uploading, setUploading] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [missingIds, setMissingIds] = useState<string[]>([])

  async function load(nextOffset = offset, category = categoryFilter, search = activeSearch) {
    setLoading(true); setError('')
    try {
      const page = search ? await searchMemories(search, { limit: PAGE_SIZE, offset: nextOffset }, auth) : await listMemories({ limit: PAGE_SIZE, offset: nextOffset, category: category || undefined }, auth)
      setItems(page.items); setTotal(page.total); setOffset(nextOffset); setPageCount(page.items.length); setHasMore(page.has_more)
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Could not load memories. Please try again.') }
    finally { setLoading(false) }
  }
  useEffect(() => { void load(0) }, [auth?.token])
  useEffect(() => {
    let cancelled = false
    if (inspectIds.length) Promise.all(inspectIds.map(async (id) => {
      try { return { id, record: await getMemory(id, auth), missing: false } }
      catch (cause) {
        const missing = cause instanceof Error && cause.message.startsWith('That memory is no longer available.')
        return { id, record: null, missing, error: missing ? '' : cause instanceof Error ? cause.message : 'Could not inspect a referenced memory.' }
      }
    })).then((records) => {
      if (cancelled) return
      const found = records.map((r) => r.record).filter((r): r is MemoryRecord => r !== null)
      setItems((current) => [...new Map([...found, ...current].map((r) => [r.id, r])).values()])
      setMissingIds(records.filter((r) => r.missing).map((r) => r.id))
      const inspectionError = records.find((r) => 'error' in r && r.error)?.error
      if (inspectionError) setError(inspectionError)
    })
    return () => { cancelled = true }
  }, [inspectIds.join('|'), auth?.token])

  function beginEdit(record: MemoryRecord) { setEditingId(record.id); setForm({ content: record.content, category: record.category ?? '', source_reference: record.source_reference ?? '' }); setNotice('') }
  function beginCreate() { setEditingId(null); setForm(emptyForm); setNotice('') }
  async function save(event: FormEvent) {
    event.preventDefault()
    if (!form.content.trim()) { setError('Memory content is required.'); return }
    setSaving(true); setError('')
    const payload: MemoryInput = { content: form.content.trim(), category: form.category.trim() || null, source_reference: form.source_reference.trim() || null }
    try { if (editingId) await updateMemory(editingId, payload, auth); else await createMemory(payload, auth); beginCreate(); setNotice(editingId ? 'Memory updated.' : 'Memory saved.'); await load(0) }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Could not save memory. Please try again.') }
    finally { setSaving(false) }
  }
  async function confirmDelete() {
    if (!deleteTarget) return
    setSaving(true); setError('')
    try { await deleteMemory(deleteTarget.id, auth); setDeleteTarget(null); setNotice('Memory permanently deleted.'); await load(Math.max(0, offset - (items.length === 1 ? PAGE_SIZE : 0))) }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Could not delete memory. Please try again.') }
    finally { setSaving(false) }
  }
  function selectDocument(file: File | undefined) {
    setError(''); setNotice('')
    if (!file) return
    if (!/\.(txt|md)$/i.test(file.name)) { setError('Choose a .txt or .md document.'); if (fileInput.current) fileInput.current.value = ''; return }
    if (file.size > 16 * 1024) { setError('The document exceeds the 16 KiB upload limit.'); if (fileInput.current) fileInput.current.value = ''; return }
    setImportTarget(file)
  }
  async function confirmImport() {
    if (!importTarget) return
    setUploading(true); setError(''); setNotice('')
    try {
      await ingestDocument(importTarget, auth)
      setNotice(`Imported ${importTarget.name} as one document record. Identical content is not stored twice.`)
      setImportTarget(null)
      if (fileInput.current) fileInput.current.value = ''
      await load(0)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not import the document. Please try again.')
    } finally { setUploading(false) }
  }
  function applyCategory(event: FormEvent) { event.preventDefault(); const category = categoryInput.trim(); if (categoryInput && !category) { setError('Category must not be blank.'); return }; setActiveSearch(''); setSearchInput(''); setCategoryFilter(category); void load(0, category, '') }
  function applySearch(event: FormEvent) { event.preventDefault(); if (!searchInput.trim()) { setError('Enter a search phrase.'); return }; setCategoryFilter(''); setCategoryInput(''); setActiveSearch(searchInput.trim()); void load(0, '', searchInput.trim()) }

  return <section className="memory-view" aria-labelledby="memory-heading">
    <header className="memory-header"><div><p className="memory-kicker">EXPLICITLY MANAGED</p><h1 id="memory-heading">Memory</h1><p>Inspect and manage saved reference records. Chat never creates memories automatically.</p></div><button type="button" className="memory-primary" onClick={beginCreate}>Add memory</button></header>
    <p className="memory-local-note" role="note">Single-owner local prototype: authentication is not PostgreSQL row-level security and does not make this safe for shared or public deployment.</p>
    {error && !importTarget && <div className="memory-error" role="alert"><span>{error}</span><button type="button" onClick={() => void load()}>Retry</button></div>}
    {notice && <p className="memory-notice" role="status">{notice}</p>}
    {inspectIds.length > 0 && <p className="memory-stale" role="note">Current records for this response’s memory IDs are shown below when available. Records may have changed since that response; this is not a historical snapshot.</p>}
    {missingIds.length > 0 && <p className="memory-stale" role="status">Some referenced memories are no longer available: {missingIds.join(', ')}</p>}
    <div className="memory-columns"><section className="memory-panel" aria-label="Saved memories">
      <div className="memory-tools"><label htmlFor="memory-document">Import one .txt or .md document (16 KiB maximum)</label><input ref={fileInput} id="memory-document" type="file" accept=".txt,.md,text/plain,text/markdown" disabled={uploading} onChange={(event) => selectDocument(event.currentTarget.files?.[0])} /></div>
      <div className="memory-tools"><form onSubmit={applySearch} className="memory-search"><label htmlFor="memory-search">Lexical search</label><div><input id="memory-search" value={searchInput} onChange={(e) => setSearchInput(e.target.value)} maxLength={200} /><button type="submit">Search</button></div></form>
      <form onSubmit={applyCategory} className="memory-search"><label htmlFor="memory-category">Exact category</label><div><input id="memory-category" value={categoryInput} onChange={(e) => setCategoryInput(e.target.value)} maxLength={80} /><button type="submit">Filter</button></div></form>
      {(activeSearch || categoryFilter) && <button className="memory-clear-filter" type="button" onClick={() => { setActiveSearch(''); setSearchInput(''); setCategoryFilter(''); setCategoryInput(''); void load(0, '', '') }}>Clear filters</button>}</div>
      <div className="memory-list-heading"><strong>{total} {total === 1 ? 'memory' : 'memories'}</strong><span>{activeSearch ? 'Search results' : categoryFilter ? `Category: ${categoryFilter}` : 'Newest first'}</span></div>
      {loading ? <p role="status">Loading memories…</p> : items.length === 0 ? <p className="memory-empty">No memories found. Add one explicitly or change your filters.</p> : <ul className="memory-list">{items.map((record) => <li className="memory-card" key={record.id}><div className="memory-card-head"><span>{record.source_kind === 'document' ? <span className="memory-kind-tag">DOCUMENT</span> : null}{record.category || (record.source_kind === 'document' ? '' : 'Uncategorized')}</span><time dateTime={record.updated_at}>Updated {new Date(record.updated_at).toLocaleDateString()}</time></div><p>{record.content}</p>{record.source_reference && <small>Source: {record.source_reference}</small>}<div className="memory-card-actions"><button type="button" onClick={() => beginEdit(record)}>Edit</button><button type="button" className="danger-link" onClick={() => setDeleteTarget(record)}>Delete</button></div></li>)}</ul>}
      <div className="memory-pagination"><button type="button" disabled={loading || offset === 0} onClick={() => void load(Math.max(0, offset - PAGE_SIZE))}>Previous</button><span>{total ? `${offset + 1}–${Math.min(offset + pageCount, total)} of ${total}` : '0 records'}</span><button type="button" disabled={loading || !hasMore} onClick={() => void load(offset + PAGE_SIZE)}>Next</button></div>
    </section><section className="memory-panel memory-editor" aria-labelledby="memory-form-title"><h2 id="memory-form-title">{editingId ? 'Edit memory' : 'Add a memory'}</h2><p>Save only information you intentionally want available as reference.</p><form onSubmit={save}>
      <label htmlFor="memory-content">Content</label><textarea id="memory-content" value={form.content} onChange={(e) => setForm({ ...form, content: e.target.value })} maxLength={4000} rows={5} required />
      <label htmlFor="memory-category-input">Category <span>optional</span></label><input id="memory-category-input" value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value })} maxLength={80} placeholder="e.g. project" />
      <label htmlFor="memory-source">Source reference <span>optional</span></label><input id="memory-source" value={form.source_reference} onChange={(e) => setForm({ ...form, source_reference: e.target.value })} maxLength={512} placeholder="e.g. a document or ticket" />
      <p className="memory-clear-help">Clear an optional field by leaving it empty when saving.</p><div className="memory-editor-actions"><button type="submit" className="memory-primary" disabled={saving}>{saving ? 'Saving…' : editingId ? 'Save changes' : 'Save memory'}</button>{editingId && <button type="button" onClick={beginCreate}>Cancel edit</button>}</div>
    </form></section></div>
    {importTarget && <div className="memory-dialog-backdrop"><section className="memory-dialog" role="alertdialog" aria-modal="true" aria-labelledby="import-memory-title" aria-describedby="import-memory-description" onKeyDown={(event) => {
      if (event.key === 'Escape' && !uploading) { event.preventDefault(); setImportTarget(null); if (fileInput.current) fileInput.current.value = ''; return }
      if (event.key === 'Tab') {
        const buttons = event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')
        const first = buttons[0], last = buttons[buttons.length - 1]
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
      }
    }}><h2 id="import-memory-title">Import this document?</h2><p id="import-memory-description">The selected file will be stored as one memory. Its contents are treated as untrusted reference text.</p><p><strong>{importTarget.name}</strong></p>{error && <p role="alert">{error}</p>}{uploading && <p role="status" aria-live="polite">Importing document…</p>}<div><button type="button" autoFocus onClick={() => { setImportTarget(null); if (fileInput.current) fileInput.current.value = '' }} disabled={uploading}>Cancel</button><button type="button" className="memory-primary" onClick={() => void confirmImport()} disabled={uploading}>{uploading ? 'Importing…' : 'Import document'}</button></div></section></div>}
    {deleteTarget && <div className="memory-dialog-backdrop"><section className="memory-dialog" role="alertdialog" aria-modal="true" aria-labelledby="delete-memory-title" aria-describedby="delete-memory-description" onKeyDown={(event) => {
      if (event.key === 'Escape' && !saving) { event.preventDefault(); setDeleteTarget(null); return }
      if (event.key === 'Tab') {
        const buttons = event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')
        const first = buttons[0], last = buttons[buttons.length - 1]
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
      }
    }}><h2 id="delete-memory-title">Permanently delete memory?</h2><p id="delete-memory-description">This permanently deletes the selected memory and cannot be undone.</p><blockquote>{deleteTarget.content}</blockquote><div><button type="button" autoFocus onClick={() => setDeleteTarget(null)} disabled={saving}>Cancel</button><button type="button" className="memory-delete-button" onClick={() => void confirmDelete()} disabled={saving}>{saving ? 'Deleting…' : 'Delete permanently'}</button></div></section></div>}
  </section>
}
