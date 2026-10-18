import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import AssistantPanel from './AssistantPanel'
import {
  listCalendarEvents,
  listNotes,
  listReminders,
  listTasks,
  updateReminder,
  updateTask,
  type CalendarEventItem,
  type NoteItem,
  type ReminderItem,
  type TaskItem,
} from '../services/api'

vi.mock('../services/api', () => ({
  listTasks: vi.fn(),
  createTask: vi.fn(),
  updateTask: vi.fn(),
  deleteTask: vi.fn(),
  listNotes: vi.fn(),
  createNote: vi.fn(),
  updateNote: vi.fn(),
  deleteNote: vi.fn(),
  listReminders: vi.fn(),
  createReminder: vi.fn(),
  updateReminder: vi.fn(),
  deleteReminder: vi.fn(),
  listCalendarEvents: vi.fn(),
  createCalendarEvent: vi.fn(),
  deleteCalendarEvent: vi.fn(),
}))

const auth = { token: 'test-token' }

const sampleTask: TaskItem = {
  id: 'task-1',
  owner_id: 'owner-1',
  title: 'Implement v3.1 Subsystem',
  description: 'Add tests and panel',
  status: 'pending',
  priority: 'high',
  due_date: null,
  tags: ['release', 'assistant'],
  created_at: '2026-10-17T10:00:00Z',
  updated_at: '2026-10-17T10:00:00Z',
}

const sampleNote: NoteItem = {
  id: 'note-1',
  owner_id: 'owner-1',
  title: 'Architecture Blueprint',
  content: 'Autonomous operating system specifications',
  tags: ['architecture'],
  created_at: '2026-10-17T10:00:00Z',
  updated_at: '2026-10-17T10:00:00Z',
}

const sampleReminder: ReminderItem = {
  id: 'rem-1',
  owner_id: 'owner-1',
  task_id: null,
  title: 'Review build logs',
  remind_at: '2026-10-17T12:00:00Z',
  status: 'pending',
  created_at: '2026-10-17T10:00:00Z',
  updated_at: '2026-10-17T10:00:00Z',
}

const sampleEvent: CalendarEventItem = {
  id: 'ev-1',
  owner_id: 'owner-1',
  title: 'Team Sync',
  description: null,
  start_time: '2026-10-17T14:00:00Z',
  end_time: '2026-10-17T15:00:00Z',
  location: 'Local Room',
  created_at: '2026-10-17T10:00:00Z',
  updated_at: '2026-10-17T10:00:00Z',
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AssistantPanel', () => {
  it('requires unlocking and makes no request without a token', () => {
    render(<AssistantPanel auth={{}} onUnlock={() => undefined} />)
    expect(screen.getByText('Unlock the workspace to view and manage personal assistant items.')).toBeInTheDocument()
    expect(listTasks).not.toHaveBeenCalled()
  })

  it('renders tasks and handles completing a task', async () => {
    vi.mocked(listTasks).mockResolvedValue({ items: [sampleTask], total: 1 })
    vi.mocked(updateTask).mockResolvedValue({ ...sampleTask, status: 'completed' })

    render(<AssistantPanel auth={auth} onUnlock={() => undefined} />)

    await screen.findByText('Implement v3.1 Subsystem')
    expect(screen.getByText('high')).toBeInTheDocument()
    expect(screen.getByText('#release')).toBeInTheDocument()

    const checkbox = screen.getByRole('checkbox')
    await userEvent.click(checkbox)

    expect(updateTask).toHaveBeenCalledWith(sampleTask.id, { status: 'completed' }, auth)
  })

  it('navigates to notes, displays notes, and searches', async () => {
    vi.mocked(listTasks).mockResolvedValue({ items: [], total: 0 })
    vi.mocked(listNotes).mockResolvedValue({ items: [sampleNote], total: 1 })

    render(<AssistantPanel auth={auth} onUnlock={() => undefined} />)

    const notesTab = screen.getByRole('button', { name: /Notes/ })
    await userEvent.click(notesTab)

    await screen.findByText('Architecture Blueprint')
    expect(screen.getByText('Autonomous operating system specifications')).toBeInTheDocument()
  })

  it('navigates to reminders and dismisses a reminder', async () => {
    vi.mocked(listTasks).mockResolvedValue({ items: [], total: 0 })
    vi.mocked(listReminders).mockResolvedValue({ items: [sampleReminder], total: 1 })
    vi.mocked(updateReminder).mockResolvedValue({ ...sampleReminder, status: 'dismissed' })

    render(<AssistantPanel auth={auth} onUnlock={() => undefined} />)

    const remindersTab = screen.getByRole('button', { name: /Reminders/ })
    await userEvent.click(remindersTab)

    await screen.findByText('Review build logs')
    const dismissBtn = screen.getByRole('button', { name: 'Dismiss' })
    await userEvent.click(dismissBtn)

    expect(updateReminder).toHaveBeenCalledWith(sampleReminder.id, { status: 'dismissed' }, auth)
  })

  it('navigates to calendar and displays explicit local disclaimer banner', async () => {
    vi.mocked(listTasks).mockResolvedValue({ items: [], total: 0 })
    vi.mocked(listCalendarEvents).mockResolvedValue({ items: [sampleEvent], total: 1 })

    render(<AssistantPanel auth={auth} onUnlock={() => undefined} />)

    const calTab = screen.getByRole('button', { name: /Calendar/ })
    await userEvent.click(calTab)

    await screen.findByText('Team Sync')
    expect(screen.getByText(/Local Calendar Abstraction · Calendar data remains strictly local to JARVIS. External sync is disclaimed./)).toBeInTheDocument()
  })
})
