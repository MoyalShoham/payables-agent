import { useState } from 'react'
import { api, ApiError } from '../api'
import type { DraftDetail, User } from '../types'

export const MIN_NOTE = 15

/** Approve or reject. Approval needs a recorded phone callback whenever bank details changed. */
export function DecisionPanel({
  draft,
  user,
  onDone,
}: {
  draft: DraftDetail
  user: User
  onDone: () => void
}) {
  const [comment, setComment] = useState('')
  const [note, setNote] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  if (draft.state === 'APPROVED' || draft.state === 'REJECTED') {
    return (
      <section className="card">
        <h2>Decision</h2>
        <p>
          {draft.state === 'APPROVED' ? 'Approved' : 'Rejected'} by {draft.decided_by}
          {draft.decision_comment ? `: “${draft.decision_comment}”` : ''}
        </p>
        {draft.bank_verification_note && (
          <p>Bank details verified: {draft.bank_verification_note}</p>
        )}
        {draft.payment && (
          <p className="simulated">
            Simulated payment {draft.payment.reference}. No money was moved.
          </p>
        )}
      </section>
    )
  }

  const canApproveState = draft.state === 'READY' || draft.state === 'NEEDS_ATTENTION'
  const isApprover = user.role === 'APPROVER'
  const ownUpload = draft.document.uploaded_by === user.display_name
  const noteMissing = draft.requires_bank_verification && note.trim().length < MIN_NOTE

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
      onDone()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Something went wrong')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="card">
      <h2>Decision</h2>
      {!isApprover && (
        <p className="muted">Only an approver can approve payments. You can reject.</p>
      )}
      {ownUpload && isApprover && (
        <p className="muted">You uploaded this document, so another approver has to approve it.</p>
      )}
      {draft.requires_bank_verification && (
        <label className="field">
          <span>
            The bank details changed. Call the vendor on a number you already have on file (not one
            from the invoice or email) and record who confirmed the account.
          </span>
          <textarea
            value={note}
            onChange={(e) => setNote(e.target.value)}
            rows={3}
            maxLength={1000}
            aria-label="Bank verification note"
          />
        </label>
      )}
      <label className="field">
        <span>Comment</span>
        <textarea
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          rows={2}
          maxLength={1000}
          aria-label="Comment"
        />
      </label>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="actions">
        <button
          type="button"
          className="primary"
          disabled={busy || !isApprover || ownUpload || !canApproveState || noteMissing}
          onClick={() =>
            void act(() =>
              api.approve(draft.id, comment, draft.requires_bank_verification ? note : null),
            )
          }
        >
          Approve (simulated payment)
        </button>
        <button
          type="button"
          className="danger"
          disabled={busy || !comment.trim()}
          title={comment.trim() ? '' : 'Write the reason in the comment'}
          onClick={() => void act(() => api.reject(draft.id, comment))}
        >
          Reject
        </button>
      </div>
      {!canApproveState && <p className="muted">A draft in this state can only be rejected.</p>}
    </section>
  )
}
