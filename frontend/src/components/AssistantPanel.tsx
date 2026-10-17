import { useEffect, useRef, useState, type FormEvent } from 'react'
import {
  createCalendarEvent,
  createNote,
  createReminder,
  createTask,
  deleteCalendarEvent,
  deleteNote,
  deleteReminder,
  deleteTask,
  listCalendarEvents,
  listNotes,
  listReminders,
  listTasks,
  updateReminder,
  updateTask,
  type ApiAuth,
  type CalendarEventItem,
  type NoteItem,
  type ReminderItem,
  type TaskItem,
  type TaskPriority,
  type TaskStatus,
} from '../services/api'
import './AssistantPanel.css'

export default function AssistantPanel({ auth, onUnlock }: { auth: ApiAuth; onUnlock: () => void }) {
  const [tab, setTab] = useState<'tasks' | 'notes' | 'reminders' | 'calendar'>('tasks')
  const [tasks, setTasks] = useState<TaskItem[]>([])
  const [notes, setNotes] = useState<NoteItem[]>([])
  const [reminders, setReminders] = useState<ReminderItem[]>([])
  const [events, setEvents] = useState<CalendarEventItem[]>([])
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)

  // Task form state
  const [taskTitle, setTaskTitle] = useState('')
  const [taskPriority, setTaskPriority] = useState<TaskPriority>('medium')
  const [taskTags, setTaskTags] = useState('')

  // Note form state
  const [noteTitle, setNoteTitle] = useState('')
  const [noteContent, setNoteContent] = useState('')
  const [noteTags, setNoteTags] = useState('')
  const [noteQuery, setNoteQuery] = useState('')

  // Reminder form state
  const [reminderTitle, setReminderTitle] = useState('')
  const [reminderDate, setReminderDate] = useState('')

  // Calendar form state
  const [eventTitle, setEventTitle] = useState('')
  const [eventStart, setEventStart] = useState('')
  const [eventEnd, setEventEnd] = useState('')
  const [eventLocation, setEventLocation] = useState('')

  const generation = useRef(0)

  async function loadData(version = generation.current) {
    if (!auth.token) return
    setError('')
    try {
      if (tab === 'tasks') {
        const page = await listTasks(auth)
        if (version !== generation.current) return
        setTasks(page.items)
      } else if (tab === 'notes') {
        const page = await listNotes(auth, noteQuery || undefined)
        if (version !== generation.current) return
        setNotes(page.items)
      } else if (tab === 'reminders') {
        const page = await listReminders(auth)
        if (version !== generation.current) return
        setReminders(page.items)
      } else if (tab === 'calendar') {
        const now = new Date()
        const start = new Date(now.getTime() - 7 * 86400000).toISOString()
        const end = new Date(now.getTime() + 30 * 86400000).toISOString()
        const page = await listCalendarEvents(start, end, auth)
        if (version !== generation.current) return
        setEvents(page.items)
      }
    } catch (cause) {
      if (version === generation.current) {
        setError(cause instanceof Error ? cause.message : 'Failed to load assistant data.')
      }
    }
  }

  useEffect(() => {
    generation.current += 1
    setError('')
    setNotice('')
    if (auth.token) void loadData(generation.current)
  }, [auth.token, tab, noteQuery]) // eslint-disable-line react-hooks/exhaustive-deps

  // Task actions
  async function handleCreateTask(e: FormEvent) {
    e.preventDefault()
    if (!taskTitle.trim() || busy) return
    setBusy(true)
    setError('')
    try {
      const tags = taskTags.split(',').map((t) => t.trim()).filter(Boolean)
      await createTask({ title: taskTitle.trim(), priority: taskPriority, tags }, auth)
      setTaskTitle('')
      setTaskTags('')
      setNotice('Task created successfully.')
      await loadData()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Failed to create task.')
    } finally {
      setBusy(false)
    }
  }

  async function handleToggleTaskStatus(task: TaskItem) {
    if (busy) return
    setBusy(true)
    const nextStatus: TaskStatus = task.status === 'completed' ? 'pending' : 'completed'
    try {
      await updateTask(task.id, { status: nextStatus }, auth)
      await loadData()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Failed to update task.')
    } finally {
      setBusy(false)
    }
  }

  async function handleDeleteTask(taskId: string) {
    if (busy) return
    setBusy(true)
    try {
      await deleteTask(taskId, auth)
      setNotice('Task deleted.')
      await loadData()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Failed to delete task.')
    } finally {
      setBusy(false)
    }
  }

  // Note actions
  async function handleCreateNote(e: FormEvent) {
    e.preventDefault()
    if (!noteTitle.trim() || !noteContent.trim() || busy) return
    setBusy(true)
    setError('')
    try {
      const tags = noteTags.split(',').map((t) => t.trim()).filter(Boolean)
      await createNote({ title: noteTitle.trim(), content: noteContent.trim(), tags }, auth)
      setNoteTitle('')
      setNoteContent('')
      setNoteTags('')
      setNotice('Note saved.')
      await loadData()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Failed to create note.')
    } finally {
      setBusy(false)
    }
  }

  async function handleDeleteNote(noteId: string) {
    if (busy) return
    setBusy(true)
    try {
      await deleteNote(noteId, auth)
      setNotice('Note removed.')
      await loadData()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Failed to delete note.')
    } finally {
      setBusy(false)
    }
  }

  // Reminder actions
  async function handleCreateReminder(e: FormEvent) {
    e.preventDefault()
    if (!reminderTitle.trim() || !reminderDate || busy) return
    setBusy(true)
    setError('')
    try {
      const isoDate = new Date(reminderDate).toISOString()
      await createReminder({ title: reminderTitle.trim(), remind_at: isoDate }, auth)
      setReminderTitle('')
      setReminderDate('')
      setNotice('Reminder set.')
      await loadData()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Failed to create reminder.')
    } finally {
      setBusy(false)
    }
  }

  async function handleDismissReminder(reminderId: string) {
    if (busy) return
    setBusy(true)
    try {
      await updateReminder(reminderId, { status: 'dismissed' }, auth)
      await loadData()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Failed to dismiss reminder.')
    } finally {
      setBusy(false)
    }
  }

  async function handleDeleteReminder(reminderId: string) {
    if (busy) return
    setBusy(true)
    try {
      await deleteReminder(reminderId, auth)
      setNotice('Reminder deleted.')
      await loadData()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Failed to delete reminder.')
    } finally {
      setBusy(false)
    }
  }

  // Calendar actions
  async function handleCreateEvent(e: FormEvent) {
    e.preventDefault()
    if (!eventTitle.trim() || !eventStart || !eventEnd || busy) return
    setBusy(true)
    setError('')
    try {
      const startIso = new Date(eventStart).toISOString()
      const endIso = new Date(eventEnd).toISOString()
      await createCalendarEvent({
        title: eventTitle.trim(),
        start_time: startIso,
        end_time: endIso,
        location: eventLocation.trim() || undefined,
      }, auth)
      setEventTitle('')
      setEventStart('')
      setEventEnd('')
      setEventLocation('')
      setNotice('Calendar event created.')
      await loadData()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Failed to create calendar event.')
    } finally {
      setBusy(false)
    }
  }

  async function handleDeleteEvent(eventId: string) {
    if (busy) return
    setBusy(true)
    try {
      await deleteCalendarEvent(eventId, auth)
      setNotice('Calendar event deleted.')
      await loadData()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Failed to delete calendar event.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="assistant-panel" aria-labelledby="assistant-heading">
      <p className="assistant-kicker">PERSONAL ASSISTANT FOUNDATION (v3.1)</p>
      <h1 id="assistant-heading">Personal Assistant</h1>
      <p className="assistant-lead">
        Manage tasks, notes, scheduled reminders, and local calendar events. Actions are recorded in the audit log.
      </p>

      {!auth.token && (
        <div role="status" className="assistant-locked">
          <p>Unlock the workspace to view and manage personal assistant items.</p>
          <button type="button" onClick={onUnlock}>Unlock Assistant</button>
        </div>
      )}

      {error && <div role="alert" className="assistant-error">{error}</div>}
      {notice && <div role="status" className="assistant-notice">{notice}</div>}

      <div className="assistant-subnav">
        <button
          type="button"
          className={tab === 'tasks' ? 'active' : ''}
          onClick={() => setTab('tasks')}
        >
          Tasks ({tasks.length})
        </button>
        <button
          type="button"
          className={tab === 'notes' ? 'active' : ''}
          onClick={() => setTab('notes')}
        >
          Notes ({notes.length})
        </button>
        <button
          type="button"
          className={tab === 'reminders' ? 'active' : ''}
          onClick={() => setTab('reminders')}
        >
          Reminders ({reminders.length})
        </button>
        <button
          type="button"
          className={tab === 'calendar' ? 'active' : ''}
          onClick={() => setTab('calendar')}
        >
          Calendar ({events.length})
        </button>
      </div>

      {/* TASKS TAB */}
      {tab === 'tasks' && (
        <div className="assistant-tab-content">
          <form className="assistant-form" onSubmit={handleCreateTask}>
            <h3>New Task</h3>
            <div className="form-row">
              <input
                type="text"
                placeholder="Task title..."
                value={taskTitle}
                onChange={(e) => setTaskTitle(e.target.value)}
                disabled={!auth.token || busy}
                required
              />
              <select
                value={taskPriority}
                onChange={(e) => setTaskPriority(e.target.value as TaskPriority)}
                disabled={!auth.token || busy}
              >
                <option value="low">Low</option>
                <option value="medium">Medium</option>
                <option value="high">High</option>
                <option value="urgent">Urgent</option>
              </select>
              <input
                type="text"
                placeholder="Tags (comma-separated)..."
                value={taskTags}
                onChange={(e) => setTaskTags(e.target.value)}
                disabled={!auth.token || busy}
              />
              <button type="submit" disabled={!auth.token || busy || !taskTitle.trim()}>
                Add Task
              </button>
            </div>
          </form>

          <ul className="assistant-list">
            {tasks.length === 0 ? (
              <li className="empty-message">No tasks found.</li>
            ) : (
              tasks.map((task) => (
                <li key={task.id} className={`assistant-item task-${task.status}`}>
                  <label className="task-toggle">
                    <input
                      type="checkbox"
                      checked={task.status === 'completed'}
                      onChange={() => handleToggleTaskStatus(task)}
                      disabled={!auth.token || busy}
                    />
                    <span className={task.status === 'completed' ? 'completed' : ''}>
                      {task.title}
                    </span>
                  </label>
                  <div className="item-meta">
                    <span className={`priority-badge priority-${task.priority}`}>
                      {task.priority}
                    </span>
                    {task.tags.map((tg) => (
                      <span key={tg} className="tag-badge">#{tg}</span>
                    ))}
                    <button
                      type="button"
                      className="delete-button"
                      onClick={() => handleDeleteTask(task.id)}
                      disabled={!auth.token || busy}
                    >
                      Delete
                    </button>
                  </div>
                </li>
              ))
            )}
          </ul>
        </div>
      )}

      {/* NOTES TAB */}
      {tab === 'notes' && (
        <div className="assistant-tab-content">
          <div className="notes-search-bar">
            <input
              type="text"
              placeholder="Search notes..."
              value={noteQuery}
              onChange={(e) => setNoteQuery(e.target.value)}
              disabled={!auth.token}
            />
          </div>
          <form className="assistant-form" onSubmit={handleCreateNote}>
            <h3>New Note</h3>
            <input
              type="text"
              placeholder="Note title..."
              value={noteTitle}
              onChange={(e) => setNoteTitle(e.target.value)}
              disabled={!auth.token || busy}
              required
            />
            <textarea
              placeholder="Note content..."
              value={noteContent}
              onChange={(e) => setNoteContent(e.target.value)}
              disabled={!auth.token || busy}
              rows={3}
              required
            />
            <div className="form-row">
              <input
                type="text"
                placeholder="Tags (comma-separated)..."
                value={noteTags}
                onChange={(e) => setNoteTags(e.target.value)}
                disabled={!auth.token || busy}
              />
              <button type="submit" disabled={!auth.token || busy || !noteTitle.trim() || !noteContent.trim()}>
                Save Note
              </button>
            </div>
          </form>

          <ul className="assistant-list">
            {notes.length === 0 ? (
              <li className="empty-message">No notes found.</li>
            ) : (
              notes.map((note) => (
                <li key={note.id} className="assistant-item note-item">
                  <div className="note-header">
                    <h4>{note.title}</h4>
                    <button
                      type="button"
                      className="delete-button"
                      onClick={() => handleDeleteNote(note.id)}
                      disabled={!auth.token || busy}
                    >
                      Delete
                    </button>
                  </div>
                  <p className="note-text">{note.content}</p>
                  <div className="item-meta">
                    {note.tags.map((tg) => (
                      <span key={tg} className="tag-badge">#{tg}</span>
                    ))}
                  </div>
                </li>
              ))
            )}
          </ul>
        </div>
      )}

      {/* REMINDERS TAB */}
      {tab === 'reminders' && (
        <div className="assistant-tab-content">
          <form className="assistant-form" onSubmit={handleCreateReminder}>
            <h3>Set Reminder</h3>
            <div className="form-row">
              <input
                type="text"
                placeholder="Reminder title..."
                value={reminderTitle}
                onChange={(e) => setReminderTitle(e.target.value)}
                disabled={!auth.token || busy}
                required
              />
              <input
                type="datetime-local"
                value={reminderDate}
                onChange={(e) => setReminderDate(e.target.value)}
                disabled={!auth.token || busy}
                required
              />
              <button type="submit" disabled={!auth.token || busy || !reminderTitle.trim() || !reminderDate}>
                Schedule
              </button>
            </div>
          </form>

          <ul className="assistant-list">
            {reminders.length === 0 ? (
              <li className="empty-message">No reminders scheduled.</li>
            ) : (
              reminders.map((rem) => (
                <li key={rem.id} className="assistant-item reminder-item">
                  <div>
                    <strong>{rem.title}</strong>
                    <div className="item-meta">
                      <span>Due: {new Date(rem.remind_at).toLocaleString()}</span>
                      <span className={`status-badge status-${rem.status}`}>{rem.status}</span>
                    </div>
                  </div>
                  <div className="item-meta">
                    {rem.status === 'pending' && (
                      <button
                        type="button"
                        onClick={() => handleDismissReminder(rem.id)}
                        disabled={!auth.token || busy}
                      >
                        Dismiss
                      </button>
                    )}
                    <button
                      type="button"
                      className="delete-button"
                      onClick={() => handleDeleteReminder(rem.id)}
                      disabled={!auth.token || busy}
                    >
                      Delete
                    </button>
                  </div>
                </li>
              ))
            )}
          </ul>
        </div>
      )}

      {/* CALENDAR TAB */}
      {tab === 'calendar' && (
        <div className="assistant-tab-content">
          <div className="disclaimer-banner">
            Local Calendar Abstraction · Calendar data remains strictly local to JARVIS. External sync is disclaimed.
          </div>

          <form className="assistant-form" onSubmit={handleCreateEvent}>
            <h3>Schedule Event</h3>
            <div className="form-row">
              <input
                type="text"
                placeholder="Event title..."
                value={eventTitle}
                onChange={(e) => setEventTitle(e.target.value)}
                disabled={!auth.token || busy}
                required
              />
              <input
                type="text"
                placeholder="Location (optional)..."
                value={eventLocation}
                onChange={(e) => setEventLocation(e.target.value)}
                disabled={!auth.token || busy}
              />
            </div>
            <div className="form-row">
              <label>
                Start:
                <input
                  type="datetime-local"
                  value={eventStart}
                  onChange={(e) => setEventStart(e.target.value)}
                  disabled={!auth.token || busy}
                  required
                />
              </label>
              <label>
                End:
                <input
                  type="datetime-local"
                  value={eventEnd}
                  onChange={(e) => setEventEnd(e.target.value)}
                  disabled={!auth.token || busy}
                  required
                />
              </label>
              <button type="submit" disabled={!auth.token || busy || !eventTitle.trim() || !eventStart || !eventEnd}>
                Add Event
              </button>
            </div>
          </form>

          <ul className="assistant-list">
            {events.length === 0 ? (
              <li className="empty-message">No upcoming events.</li>
            ) : (
              events.map((ev) => (
                <li key={ev.id} className="assistant-item event-item">
                  <div>
                    <h4>{ev.title}</h4>
                    {ev.location && <p className="event-loc">📍 {ev.location}</p>}
                    <div className="item-meta">
                      <span>{new Date(ev.start_time).toLocaleString()} – {new Date(ev.end_time).toLocaleTimeString()}</span>
                    </div>
                  </div>
                  <button
                    type="button"
                    className="delete-button"
                    onClick={() => handleDeleteEvent(ev.id)}
                    disabled={!auth.token || busy}
                  >
                    Delete
                  </button>
                </li>
              ))
            )}
          </ul>
        </div>
      )}
    </section>
  )
}
