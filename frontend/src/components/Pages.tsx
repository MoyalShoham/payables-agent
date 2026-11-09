import { useEffect, useState } from 'react'
import { api, ApiError } from '../api'
import { bank, money, STATE_LABEL, when } from '../format'
import type { AuditEntry, DraftState, DraftSummary, Payment, Vendor } from '../types'

function useLoad<T>(load: () => Promise<T>, deps: unknown[]): [T | null, string | null] {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let cancelled = false
    load()
      .then((d) => {
        if (!cancelled) setData(d)
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof ApiError ? e.message : 'Could not load')
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  return [data, error]
}

export function Queue({ onOpen }: { onOpen: (id: number) => void }) {
  const [filter, setFilter] = useState<DraftState | ''>('')
  const [drafts, error] = useLoad(() => api.drafts(filter || undefined), [filter])
  return (
    <section className="stack">
      <div className="toolbar">
        <h1>Review queue</h1>
        <select
          aria-label="Filter"
          value={filter}
          onChange={(e) => setFilter(e.target.value as DraftState | '')}
        >
          <option value="">All</option>
          {(Object.keys(STATE_LABEL) as DraftState[]).map((s) => (
            <option key={s} value={s}>
              {STATE_LABEL[s]}
            </option>
          ))}
        </select>
      </div>
      {error && <p className="error">{error}</p>}
      {drafts && drafts.length === 0 && <p className="muted">Nothing here.</p>}
      {drafts && drafts.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Vendor</th>
              <th>Invoice</th>
              <th className="num">Amount</th>
              <th>State</th>
              <th>Risks</th>
            </tr>
          </thead>
          <tbody>
            {drafts.map((d: DraftSummary) => (
              <tr key={d.id} onClick={() => onOpen(d.id)} className="clickable">
                <td>{d.id}</td>
                <td dir="auto">{d.vendor ?? '—'}</td>
                <td>{d.invoice_number ?? '—'}</td>
                <td className="num">{money(d.amount, d.currency)}</td>
                <td>
                  <span className={`state ${d.state}`}>{STATE_LABEL[d.state]}</span>
                </td>
                <td>
                  {[
                    ...d.flags.map((f) => f.replaceAll('_', ' ').toLowerCase()),
                    ...d.failed_checks.map((c) => `failed: ${c.replaceAll('_', ' ')}`),
                  ].join(', ')}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}

export function Upload({ onUploaded }: { onUploaded: (draftId: number) => void }) {
  const [message, setMessage] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  return (
    <section className="card stack">
      <h1>Upload an invoice</h1>
      <p className="muted">
        PDF, PNG, JPEG or WebP up to 10 MB. The agent reads it, checks it and prepares a draft; a
        person approves it.
      </p>
      <label className="field">
        <span>Covering email or message (optional, checked for bank-change requests)</span>
        <textarea
          value={message}
          rows={3}
          maxLength={4000}
          onChange={(e) => setMessage(e.target.value)}
        />
      </label>
      <input
        type="file"
        aria-label="Invoice file"
        accept=".pdf,.png,.jpg,.jpeg,.webp"
        disabled={busy}
        onChange={(e) => {
          const file = e.target.files?.[0]
          if (!file) return
          setBusy(true)
          setError(null)
          api
            .upload(file, message)
            .then((r) => onUploaded(r.draft_id))
            .catch((err: unknown) =>
              setError(err instanceof ApiError ? err.message : 'Upload failed'),
            )
            .finally(() => setBusy(false))
        }}
      />
      {busy && <p className="muted">Reading the invoice…</p>}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </section>
  )
}

export function Vendors() {
  const [vendors, error] = useLoad(api.vendors, [])
  return (
    <section className="stack">
      <h1>Vendor master</h1>
      <p className="muted">
        Payments always go to these accounts, never to an account printed on an invoice.
      </p>
      {error && <p className="error">{error}</p>}
      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Tax ID</th>
            <th>Bank account</th>
            <th>Phones on file</th>
          </tr>
        </thead>
        <tbody>
          {(vendors ?? []).map((v: Vendor) => (
            <tr key={v.id}>
              <td dir="auto">{v.name}</td>
              <td>{v.tax_id}</td>
              <td dir="ltr">{bank(v.bank)}</td>
              <td>{v.phones.length ? v.phones.join(', ') : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}

export function Payments() {
  const [payments, error] = useLoad(api.payments, [])
  return (
    <section className="stack">
      <h1>Simulated payments</h1>
      <p className="simulated">
        Nothing in this application sends money. These are records of what would be paid.
      </p>
      {error && <p className="error">{error}</p>}
      <table>
        <thead>
          <tr>
            <th>Reference</th>
            <th>Draft</th>
            <th className="num">Amount</th>
            <th>To</th>
            <th>When</th>
          </tr>
        </thead>
        <tbody>
          {(payments ?? []).map((p: Payment) => (
            <tr key={p.id}>
              <td>{p.reference}</td>
              <td>#{p.draft_id}</td>
              <td className="num">{money(p.amount, p.currency)}</td>
              <td dir="ltr">{bank(p.pay_to)}</td>
              <td>{when(p.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}

export function Audit() {
  const [entries, error] = useLoad(api.audit, [])
  const [verdict, setVerdict] = useState<string | null>(null)
  return (
    <section className="stack">
      <div className="toolbar">
        <h1>Audit log</h1>
        <button
          type="button"
          onClick={() =>
            void api
              .verifyAudit()
              .then((v) =>
                setVerdict(
                  v.intact
                    ? `Chain intact: ${v.entries} entries` +
                        (v.checkpoint
                          ? `, including checkpoint ${v.checkpoint.seq}`
                          : ' (no checkpoint yet)')
                    : `Problem: ${v.problem ?? `entry ${v.first_broken_entry}`}`,
                ),
              )
          }
        >
          Verify the audit chain
        </button>
      </div>
      {verdict && <p role="status">{verdict}</p>}
      {error && <p className="error">{error}</p>}
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>When</th>
            <th>Who</th>
            <th>Action</th>
            <th>On</th>
            <th>Hash</th>
          </tr>
        </thead>
        <tbody>
          {(entries ?? []).map((e: AuditEntry) => (
            <tr key={e.id}>
              <td>{e.id}</td>
              <td>{when(e.at)}</td>
              <td>{e.actor}</td>
              <td>{e.action}</td>
              <td>
                {e.entity}
                {e.entity_id != null ? ` #${e.entity_id}` : ''}
              </td>
              <td className="mono">{e.hash}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
