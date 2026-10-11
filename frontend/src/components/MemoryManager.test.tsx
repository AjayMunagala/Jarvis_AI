import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import MemoryManager from './MemoryManager'
import { createMemory, deleteMemory, getMemory, listMemories, searchMemories, updateMemory } from '../services/api'

vi.mock('../services/api', () => ({ createMemory: vi.fn(), deleteMemory: vi.fn(), getMemory: vi.fn(), listMemories: vi.fn(), searchMemories: vi.fn(), updateMemory: vi.fn() }))
const record = { id: 'memory-1', content: 'A saved fact', category: 'project', source_reference: 'doc-1', created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z' }
const page = { items: [record], total: 1, limit: 20, offset: 0, has_more: false }
afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('MemoryManager', () => {
  it('shows loading and advances through a paginated list', async () => {
    let resolveFirst!: (value: typeof page) => void
    vi.mocked(listMemories).mockReturnValueOnce(new Promise((resolve) => { resolveFirst = resolve }))
      .mockResolvedValueOnce({ ...page, items: [{ ...record, id: 'memory-2', content: 'Second page' }], total: 21, offset: 20, has_more: false })
    render(<MemoryManager />)
    expect(await screen.findByRole('status')).toHaveTextContent('Loading memories')
    await act(async () => resolveFirst({ ...page, total: 21, has_more: true }))
    await screen.findByText('A saved fact')
    await userEvent.click(screen.getByRole('button', { name: 'Next' }))
    expect(await screen.findByText('Second page')).toBeInTheDocument()
    expect(listMemories).toHaveBeenLastCalledWith({ limit: 20, offset: 20, category: undefined }, undefined)
  })

  it('loads, filters by category, and searches lexically', async () => {
    vi.mocked(listMemories).mockResolvedValue(page); vi.mocked(searchMemories).mockResolvedValue(page)
    render(<MemoryManager />); await screen.findByText('A saved fact')
    await userEvent.type(screen.getByLabelText('Exact category'), '  project  '); await userEvent.click(screen.getByRole('button', { name: 'Filter' }))
    await waitFor(() => expect(listMemories).toHaveBeenLastCalledWith({ limit: 20, offset: 0, category: 'project' }, undefined))
    const requestsBeforeBlankCategory = vi.mocked(listMemories).mock.calls.length
    await userEvent.clear(screen.getByLabelText('Exact category')); await userEvent.type(screen.getByLabelText('Exact category'), '   '); await userEvent.click(screen.getByRole('button', { name: 'Filter' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Category must not be blank.')
    expect(listMemories).toHaveBeenCalledTimes(requestsBeforeBlankCategory)
    await userEvent.type(screen.getByLabelText('Lexical search'), 'saved fact'); await userEvent.click(screen.getByRole('button', { name: 'Search' }))
    await waitFor(() => expect(searchMemories).toHaveBeenCalledWith('saved fact', { limit: 20, offset: 0 }, undefined))
  })
  it('creates and edits records and clears optional fields explicitly', async () => {
    vi.mocked(listMemories).mockResolvedValue(page); vi.mocked(createMemory).mockResolvedValue(record); vi.mocked(updateMemory).mockResolvedValue({ ...record, category: null, source_reference: null })
    render(<MemoryManager />); await screen.findByText('A saved fact')
    await userEvent.type(screen.getByLabelText('Content'), 'New memory'); await userEvent.click(screen.getByRole('button', { name: 'Save memory' }))
    await waitFor(() => expect(createMemory).toHaveBeenCalledWith({ content: 'New memory', category: null, source_reference: null }, undefined))
    await userEvent.click(screen.getByRole('button', { name: 'Edit' }))
    await userEvent.clear(screen.getAllByLabelText(/category/i)[1]); await userEvent.clear(screen.getByLabelText(/source reference/i))
    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(updateMemory).toHaveBeenCalledWith('memory-1', { content: 'A saved fact', category: null, source_reference: null }, undefined))
  })
  it('requires confirmation and Cancel never sends DELETE', async () => {
    vi.mocked(listMemories).mockResolvedValue(page); vi.mocked(deleteMemory).mockResolvedValue()
    render(<MemoryManager />); await screen.findByText('A saved fact'); await userEvent.click(screen.getByRole('button', { name: 'Delete' }))
    expect(screen.getByRole('alertdialog')).toHaveTextContent(/permanently deletes.*cannot be undone/i)
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' })); expect(deleteMemory).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Delete' })); await userEvent.click(screen.getByRole('button', { name: 'Delete permanently' }))
    await waitFor(() => expect(deleteMemory).toHaveBeenCalledWith('memory-1', undefined))
  })
  it('handles unavailable storage and stale selected IDs', async () => {
    vi.mocked(listMemories).mockRejectedValue(new Error('Memory storage is disabled or unavailable. Enable local PostgreSQL persistence to manage saved memories.'))
    vi.mocked(getMemory).mockRejectedValue(new Error('That memory is no longer available. Refresh the list and try again.'))
    render(<MemoryManager inspectIds={['deleted-id']} />)
    expect(await screen.findByRole('alert')).toHaveTextContent(/Memory storage is disabled/)
    expect(await screen.findByRole('status')).toHaveTextContent(/deleted-id/)
    expect(screen.getByText(/not PostgreSQL row-level security/i)).toBeInTheDocument()
  })
})
