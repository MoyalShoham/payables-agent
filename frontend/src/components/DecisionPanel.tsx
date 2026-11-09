import { useState } from 'react'
import { api, ApiError } from '../api'
import { when } from '../format'
import type { DraftDetail, User } from '../types'

/** `datetime-local` value for now, in the browser's time zone. */
const localNow = (): string => {
  const d = new Date()
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset())
  return d.toISOString().slice(0, 16)
}

/**
 * Approve or reject. When bank details changed (or cannot be compared), approval needs a recorded
 * phone callback: a number from the vendor master, who confirmed, and when. The server checks all
 * three; the form only makes them easy to give.
 */
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
  const [phone, setPhone] = useState(draft.vendor_phones[0] ?? '')
  const [person, setPerson] = useState('')
  const [at, setAt] = useState(localNow)
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
        {draft.callback && (
          <p>
            Bank account confirmed by phone: {draft.callback.phone}, {draft.callback.person},{' '}
            {when(draft.callback.at)}.
          </p>
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
  const needsCall = draft.requires_bank_verification
  const callMissing = needsCall && (!phone || person.trim().length < 2 || !at)
  const noPayee = draft.pay_to === null

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

  const approve = () =>
    api.approve(
      draft.id,
      comment,
      needsCall ? { phone, person: person.trim(), at: new Date(at).toISOString() } : null,
    )

  return (
    <section className="card">
      <h2>Decision</h2>
      {!isApprover && (
        <p className="muted">Only an approver can approve payments. You can reject.</p>
      )}
      {ownUpload && isApprover && (
        <p className="muted">You submitted this document, so another approver has to approve it.</p>
      )}
      {noPayee && canApproveState && (
        <p className="muted">
          There is no verified vendor account to pay. Add the vendor and its account to the vendor
          master, then re-check.
        </p>
      )}
      {draft.master_changed && (
        <p className="error" role="alert">
          The vendor master changed after this draft was prepared. Re-check it before deciding.
        </p>
      )}
      {needsCall && (
        <fieldset className="field callback">
          <legend>
            The bank details changed or cannot be confirmed. Call the vendor on a number from the
            vendor master (not one from the invoice or the email) and record the call.
          </legend>
          {draft.vendor_phones.length === 0 ? (
            <p className="error">
              The vendor master has no phone number for this vendor. An admin must add one.
            </p>
          ) : (
            <label>
              <span>Number called</span>
              <select
                value={phone}
                onChange={(e) => setPhone(e.target.value)}
                aria-label="Number called"
              >
                {draft.vendor_phones.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
            </label>
          )}
          <label>
            <span>Who confirmed the account</span>
            <input
              value={person}
              onChange={(e) => setPerson(e.target.value)}
              maxLength={120}
              aria-label="Who confirmed the account"
            />
          </label>
          <label>
            <span>When</span>
            <input
              type="datetime-local"
              value={at}
              onChange={(e) => setAt(e.target.value)}
              aria-label="When"
            />
          </label>
        </fieldset>
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
          disabled={
            busy ||
            !isApprover ||
            ownUpload ||
            !canApproveState ||
            callMissing ||
            noPayee ||
            draft.master_changed
          }
          onClick={() => void act(approve)}
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
        {draft.state !== 'EXTRACTION_FAILED' && user.role !== 'AGENT' && (
          <button
            type="button"
            disabled={busy}
            title="Run the checks again against the current vendor master and history"
            onClick={() => void act(() => api.recheck(draft.id))}
          >
            Re-check
          </button>
        )}
      </div>
      {!canApproveState && <p className="muted">A draft in this state can only be rejected.</p>}
    </section>
  )
}
