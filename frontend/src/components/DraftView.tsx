import { useEffect, useState } from 'react'
import { api, ApiError } from '../api'
import { bank, money, STATE_LABEL, when } from '../format'
import type { DraftDetail, User } from '../types'
import { DecisionPanel } from './DecisionPanel'

const STATUS_ICON = { PASS: '✓', WARN: '!', FAIL: '✗', SKIP: '–' } as const

export function DraftView({ id, user, onBack }: { id: number; user: User; onBack: () => void }) {
  const [draft, setDraft] = useState<DraftDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [version, setVersion] = useState(0)

  useEffect(() => {
    let cancelled = false
    api
      .draft(id)
      .then((d) => {
        if (!cancelled) setDraft(d)
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof ApiError ? e.message : 'Could not load the draft')
      })
    return () => {
      cancelled = true
    }
  }, [id, version])

  if (error) return <p className="error">{error}</p>
  if (!draft) return <p className="muted">Loading…</p>
  const x = draft.extracted
  const invoiceBank = x?.bank ?? null
  // The backend compares every printed field; the UI shows its verdict rather than redoing it.
  const bankDiffers = draft.flags.includes('BANK_DETAILS_CHANGED')
  const bankUnverifiable = draft.flags.includes('BANK_DETAILS_UNVERIFIABLE')
  const cur = draft.currency
  const submitter = draft.document.uploaded_by
    ? `submitted by ${draft.document.uploaded_by}`
    : draft.document.submitted_by_email
      ? `emailed from ${draft.document.submitted_by_email} (sender not verified)`
      : 'from the inbox folder (submitter unknown)'

  return (
    <div className="draft">
      <button type="button" className="link" onClick={onBack}>
        ← Back to the queue
      </button>
      <header className="draft-head">
        <h1 dir="auto">{x?.vendor_name ?? draft.document.filename}</h1>
        <span className={`state ${draft.state}`}>{STATE_LABEL[draft.state]}</span>
      </header>
      <div className="split">
        <section className="card preview">
          <img alt="Invoice document" src={api.previewUrl(draft.document.id)} />
          <p className="muted">
            <bdi>{draft.document.filename}</bdi>, {submitter}.{' '}
            <a href={api.documentUrl(draft.document.id)}>Download the original</a>
          </p>
          {draft.document.message_text && (
            <blockquote>
              <strong>Covering message:</strong>{' '}
              <span dir="auto" className="message">
                {draft.document.message_text}
              </span>
            </blockquote>
          )}
        </section>
        <div className="stack">
          {draft.flag_details.length > 0 && (
            <section className="card flags">
              <h2>Risks</h2>
              <ul>
                {draft.flag_details.map((f) => (
                  <li key={f.code} className={f.severity}>
                    <strong>{f.code.replaceAll('_', ' ').toLowerCase()}</strong>
                    <span dir="auto">{f.detail}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}
          <section className="card">
            <h2>Payment</h2>
            <dl className="facts">
              <dt>Amount</dt>
              <dd>{money(draft.amount, cur)}</dd>
              <dt>Pays to (vendor master)</dt>
              <dd dir="ltr">{bank(draft.pay_to)}</dd>
              <dt>Account on the invoice</dt>
              <dd dir="ltr" className={bankDiffers || bankUnverifiable ? 'differs' : ''}>
                {bank(invoiceBank)}
                {bankDiffers && ' (differs from the account on file)'}
                {bankUnverifiable && ' (cannot be compared with the account on file)'}
              </dd>
            </dl>
          </section>
          <section className="card">
            <h2>What the agent read</h2>
            {draft.extraction_error ? (
              <p className="error">{draft.extraction_error}</p>
            ) : (
              x && (
                <dl className="facts">
                  <dt>Tax ID</dt>
                  <dd>{x.vendor_tax_id ?? '—'}</dd>
                  <dt>Invoice</dt>
                  <dd>
                    {x.invoice_number ?? '—'} ({x.document_type.replaceAll('_', ' ').toLowerCase()})
                  </dd>
                  <dt>Dates</dt>
                  <dd>
                    issued {x.issue_date ?? '—'}, due {x.due_date ?? '—'}
                  </dd>
                  <dt>Subtotal / VAT / total</dt>
                  <dd>
                    {money(x.subtotal, cur)} / {money(x.vat_amount, cur)} / {money(x.total, cur)}
                  </dd>
                  <dt>Lines</dt>
                  <dd>
                    <ul className="lines">
                      {x.line_items.map((li, i) => (
                        // The description is isolated so a Hebrew one does not reorder the numbers.
                        <li key={i}>
                          <bdi>{li.description}</bdi>: {li.quantity ?? '—'} ×{' '}
                          {money(li.unit_price, cur)} = {money(li.amount, cur)}
                        </li>
                      ))}
                    </ul>
                  </dd>
                  {x.notes && (
                    <>
                      <dt>Notes on the document</dt>
                      <dd dir="auto">{x.notes}</dd>
                    </>
                  )}
                </dl>
              )
            )}
            <p className="muted">
              Read by {draft.model}; prepared by {draft.prepared_by} on {when(draft.created_at)}.
            </p>
          </section>
          <section className="card">
            <h2>Checks</h2>
            <ul className="checks">
              {draft.checks.map((c) => (
                <li key={c.name} className={c.status}>
                  <span aria-label={c.status}>{STATUS_ICON[c.status]}</span>
                  <strong>{c.name.replaceAll('_', ' ')}</strong>
                  <span>{c.detail}</span>
                </li>
              ))}
            </ul>
          </section>
          <DecisionPanel draft={draft} user={user} onDone={() => setVersion((v) => v + 1)} />
        </div>
      </div>
    </div>
  )
}
