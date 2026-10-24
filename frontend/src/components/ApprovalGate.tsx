import { useState } from 'react'

/**
 * v6.7: shared pieces for approval-gated actions in the Workflows and Integrations views.
 *
 * A gated route answers HTTP 202 with an approval request and runs nothing. The owner approves it in the Activity
 * view, then repeats the same action with that approval ID. Pending approval IDs are remembered per action key in a
 * map owned by the app (cleared on lock), so they survive switching to the Activity view and back.
 */
export interface PendingApproval {
  approvalId: string
  /** What the approval was requested for, shown to the owner. */
  summary: string
  /** Whatever the panel needs to repeat exactly the same request (an approval only covers identical input). */
  replay?: unknown
}
export type ApprovalMemo = Map<string, PendingApproval>

export function ApprovalRequested({ pending, retryLabel, onRetry, onForget, onOpenApprovals, disabled }: {
  pending: PendingApproval
  retryLabel: string
  onRetry: () => void
  onForget: () => void
  onOpenApprovals?: () => void
  disabled?: boolean
}) {
  return <div className="gate-approval" role="group" aria-label={`Approval ${pending.approvalId}`}>
    <p>Approval requested for {pending.summary}: <code>{pending.approvalId}</code>. Nothing was executed. Approve it in the Activity view, then run it here once with this approval ID.</p>
    <div className="activity-actions">
      {onOpenApprovals && <button type="button" onClick={onOpenApprovals}>Open approvals</button>}
      <button type="button" onClick={onRetry} disabled={disabled}>{retryLabel}</button>
      <button type="button" onClick={onForget} disabled={disabled}>Forget approval</button>
    </div>
  </div>
}

/** A button that asks for confirmation inline before running a destructive action. */
export function ConfirmButton({ label, question, confirmLabel, onConfirm, disabled }: {
  label: string
  question: string
  confirmLabel: string
  onConfirm: () => void
  disabled?: boolean
}) {
  const [asking, setAsking] = useState(false)
  if (!asking) return <button type="button" className="activity-reject" onClick={() => setAsking(true)} disabled={disabled}>{label}</button>
  return <span className="gate-confirm" role="alertdialog" aria-label={question}>
    <span>{question}</span>
    <button type="button" className="activity-reject" onClick={() => { setAsking(false); onConfirm() }} disabled={disabled}>{confirmLabel}</button>
    <button type="button" onClick={() => setAsking(false)} autoFocus>Cancel</button>
  </span>
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

export function formatUtc(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : `${date.toISOString().slice(0, 16).replace('T', ' ')} UTC`
}

export function shortJson(value: unknown, limit = 400): string {
  let text: string
  try { text = JSON.stringify(value) ?? '' } catch { text = String(value) }
  return text.length > limit ? `${text.slice(0, limit)}…` : text
}
