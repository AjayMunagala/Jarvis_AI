import { useEffect, useRef, useState } from 'react'
import {
  getNotifications,
  markNotificationRead,
  type ApiAuth,
  type JarvisNotification,
} from '../services/api'
import './ActivityPanel.css'

type Permission = 'default' | 'granted' | 'denied' | 'unsupported'

export function desktopPermission(): Permission {
  if (typeof window === 'undefined' || !('Notification' in window)) return 'unsupported'
  return window.Notification.permission as Permission
}

export default function NotificationsPanel({ auth, onUnlock, onChanged }: { auth: ApiAuth; onUnlock: () => void; onChanged?: () => void }) {
  const [items, setItems] = useState<JarvisNotification[] | null>(null)
  const [error, setError] = useState('')
  const [permission, setPermission] = useState<Permission>(desktopPermission())
  const generation = useRef(0)

  async function refresh(version = generation.current) {
    try {
      const page = await getNotifications(false, 50, auth)
      if (version !== generation.current) return
      setItems(page.items); setError('')
    } catch (cause) {
      if (version === generation.current) setError(cause instanceof Error ? cause.message : 'Notifications are unavailable.')
    }
  }

  useEffect(() => {
    generation.current += 1
    setItems(null); setError('')
    if (auth.token) void refresh(generation.current)
  }, [auth.token]) // eslint-disable-line react-hooks/exhaustive-deps

  async function markRead(item: JarvisNotification) {
    try {
      await markNotificationRead(item.id, auth)
      setItems((current) => current?.map((entry) => entry.id === item.id ? { ...entry, read: true } : entry) ?? null)
      onChanged?.()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'The notification could not be updated.')
    }
  }

  async function enableDesktop() {
    if (permission === 'unsupported') return
    try {
      await window.Notification.requestPermission()  // older Safari returns nothing; read the result back below
    } catch { /* fall through to the browser's current answer */ }
    setPermission(desktopPermission())
  }

  return <section className="activity-panel" aria-labelledby="notifications-heading">
    <p className="activity-kicker">REMINDERS · BRIEFINGS · ALERTS</p>
    <h1 id="notifications-heading">Notifications</h1>
    <p>Reminders, daily briefings and alerts from JARVIS. While JARVIS is open in a browser tab, new ones also appear as desktop notifications if you allow them. Nothing is sent to other devices.</p>
    {!auth.token && <div role="status"><p>Unlock the workspace to view notifications.</p><button type="button" onClick={onUnlock}>Unlock notifications</button></div>}
    {auth.token && <div className="activity-actions">
      {permission === 'default' && <button type="button" onClick={() => void enableDesktop()}>Enable desktop notifications</button>}
      {permission === 'granted' && <span className="activity-meta">Desktop notifications are on for this browser.</span>}
      {permission === 'denied' && <span className="activity-meta">Desktop notifications are blocked in this browser's site settings.</span>}
      {permission === 'unsupported' && <span className="activity-meta">This browser does not support desktop notifications.</span>}
    </div>}
    {error && <p role="alert">{error}</p>}
    {items && (items.length ? <ul className="activity-approvals">{items.map((item) => <li key={item.id}>
      <div><strong>{item.title}</strong> <span className={`activity-risk activity-risk-${item.priority === 'urgent' ? 'critical' : item.priority === 'high' ? 'high' : 'low'}`}>{item.category}</span>{!item.read && <span className="activity-risk activity-risk-medium">new</span>}</div>
      <p>{item.message}</p>
      <small>{new Date(item.delivered_at ?? item.created_at).toLocaleString()}</small>
      {!item.read && <div className="activity-actions"><button type="button" onClick={() => void markRead(item)}>Mark as read</button></div>}
    </li>)}</ul> : <p>No notifications yet.</p>)}
  </section>
}
