import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import NotificationsPanel from './NotificationsPanel'
import { getNotifications, markNotificationRead, type JarvisNotification } from '../services/api'

vi.mock('../services/api', () => ({ getNotifications: vi.fn(), markNotificationRead: vi.fn() }))

const auth = { token: 'test-token' }
const note = (overrides: Partial<JarvisNotification> = {}): JarvisNotification => ({
  id: 'n-1', owner_id: 'o', category: 'briefing', title: 'Daily briefing for 2026-10-24', message: 'Nothing overdue.',
  priority: 'normal', read: false, delivered_at: '2026-10-24T07:00:00Z', created_at: '2026-10-24T07:00:00Z', ...overrides,
})

function stubNotification(permission: NotificationPermission | null, requested: NotificationPermission = 'granted') {
  if (permission === null) {
    vi.stubGlobal('Notification', undefined)
    Reflect.deleteProperty(window, 'Notification')
    return
  }
  const fake = Object.assign(vi.fn(), { permission, requestPermission: vi.fn().mockResolvedValue(requested) })
  vi.stubGlobal('Notification', fake)
  return fake
}

afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals() })

describe('NotificationsPanel', () => {
  it('requires unlocking and makes no request without a token', () => {
    stubNotification('default')
    render(<NotificationsPanel auth={{}} onUnlock={() => undefined} />)
    expect(screen.getByText('Unlock the workspace to view notifications.')).toBeInTheDocument()
    expect(getNotifications).not.toHaveBeenCalled()
  })

  it('lists notifications and marks one as read', async () => {
    stubNotification('granted')
    vi.mocked(getNotifications).mockResolvedValue({ items: [note(), note({ id: 'n-2', title: 'Old', read: true })], total: 2 })
    vi.mocked(markNotificationRead).mockResolvedValue({ marked: true })
    const changed = vi.fn()
    render(<NotificationsPanel auth={auth} onUnlock={() => undefined} onChanged={changed} />)
    await screen.findByText('Daily briefing for 2026-10-24')
    expect(getNotifications).toHaveBeenCalledWith(false, 50, auth)
    expect(screen.getAllByRole('button', { name: 'Mark as read' })).toHaveLength(1)
    expect(screen.getByText('Desktop notifications are on for this browser.')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Mark as read' }))
    expect(markNotificationRead).toHaveBeenCalledWith('n-1', auth)
    expect(screen.queryByRole('button', { name: 'Mark as read' })).toBeNull()
    expect(changed).toHaveBeenCalled()
  })

  it('asks for desktop permission only when the owner clicks', async () => {
    const fake = stubNotification('default', 'granted')!
    vi.mocked(getNotifications).mockResolvedValue({ items: [], total: 0 })
    render(<NotificationsPanel auth={auth} onUnlock={() => undefined} />)
    await screen.findByText('No notifications yet.')
    expect(fake.requestPermission).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Enable desktop notifications' }))
    expect(fake.requestPermission).toHaveBeenCalledTimes(1)
    expect(await screen.findByText('Desktop notifications are on for this browser.')).toBeInTheDocument()
  })

  it('explains blocked or unsupported desktop notifications and shows load errors', async () => {
    stubNotification('denied')
    vi.mocked(getNotifications).mockRejectedValue(new Error('Notifications are unavailable.'))
    render(<NotificationsPanel auth={auth} onUnlock={() => undefined} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Notifications are unavailable.')
    expect(screen.getByText(/blocked in this browser's site settings/)).toBeInTheDocument()
    cleanup()
    stubNotification(null)
    vi.mocked(getNotifications).mockResolvedValue({ items: [], total: 0 })
    render(<NotificationsPanel auth={auth} onUnlock={() => undefined} />)
    expect(await screen.findByText('This browser does not support desktop notifications.')).toBeInTheDocument()
  })
})
