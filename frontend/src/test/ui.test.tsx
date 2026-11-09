import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from '../App'
import { DecisionPanel } from '../components/DecisionPanel'
import { DraftView } from '../components/DraftView'
import { bank, money } from '../format'
import type { DraftDetail, User } from '../types'

const approver: User = {
  id: 2,
  username: 'approver',
  display_name: 'Dana (approver)',
  role: 'APPROVER',
}
const admin: User = { id: 3, username: 'admin', display_name: 'Avi (admin)', role: 'ADMIN' }

const draft = (over: Partial<DraftDetail> = {}): DraftDetail => ({
  id: 7,
  state: 'NEEDS_ATTENTION',
  vendor: 'Acme',
  invoice_number: '1',
  amount: '1180.00',
  currency: 'ILS',
  flags: ['BANK_DETAILS_CHANGED'],
  failed_checks: [],
  requires_bank_verification: true,
  created_at: '2025-06-01T10:00:00Z',
  extracted: null,
  extraction_error: null,
  model: 'claude-opus-5-5',
  checks: [],
  flag_details: [],
  reasoning: [],
  pay_to: { bank_name: 'Hapoalim', bank_code: '12', branch: '600', account: '123456', iban: null },
  vendor_phones: ['03-5551234', '052-1234567'],
  master_changed: false,
  prepared_by: 'Payables agent',
  decided_by: null,
  decided_at: null,
  decision_comment: null,
  callback: null,
  document: {
    id: 1,
    filename: 'a.pdf',
    media_type: 'application/pdf',
    source: 'UPLOAD',
    message_text: null,
    uploaded_by: 'Avi (admin)',
    submitted_by_email: null,
  },
  payment: null,
  ...over,
})

afterEach(() => vi.unstubAllGlobals())

describe('DecisionPanel', () => {
  it('needs a recorded callback on a number from the vendor master before approving', async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(new Response('{"state":"APPROVED"}', { status: 200 })),
    )
    vi.stubGlobal('fetch', fetchMock)
    const onDone = vi.fn()
    render(<DecisionPanel draft={draft()} user={approver} onDone={onDone} />)
    const approve = screen.getByRole('button', { name: /approve/i })
    expect(approve).toBeDisabled()
    // Only the numbers on file can be chosen.
    const numbers = screen.getAllByRole('option').map((o) => o.textContent)
    expect(numbers).toEqual(['03-5551234', '052-1234567'])

    await userEvent.selectOptions(screen.getByLabelText('Number called'), '052-1234567')
    await userEvent.type(screen.getByLabelText('Who confirmed the account'), 'Rina, accounts')
    expect(approve).toBeEnabled()
    await userEvent.click(approve)
    await waitFor(() => expect(onDone).toHaveBeenCalled())
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/api/drafts/7/approve')
    const body = JSON.parse(String(init.body)) as {
      callback: { phone: string; person: string; at: string }
    }
    expect(body.callback.phone).toBe('052-1234567')
    expect(body.callback.person).toBe('Rina, accounts')
    expect(Number.isNaN(Date.parse(body.callback.at))).toBe(false)
    expect(init.headers).toMatchObject({ 'X-Payables-Client': 'web' })
  })

  it('cannot approve without a payee or after the vendor master changed', () => {
    const { unmount } = render(
      <DecisionPanel
        draft={draft({ requires_bank_verification: false, pay_to: null })}
        user={approver}
        onDone={() => {}}
      />,
    )
    expect(screen.getByRole('button', { name: /approve/i })).toBeDisabled()
    expect(screen.getByText(/no verified vendor account/i)).toBeInTheDocument()
    unmount()
    render(
      <DecisionPanel
        draft={draft({ requires_bank_verification: false, master_changed: true })}
        user={approver}
        onDone={() => {}}
      />,
    )
    expect(screen.getByRole('button', { name: /approve/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: /re-check/i })).toBeEnabled()
  })

  it('does not let an admin approve, or an approver approve their own upload', () => {
    render(
      <DecisionPanel
        draft={draft({ requires_bank_verification: false })}
        user={admin}
        onDone={() => {}}
      />,
    )
    expect(screen.getByRole('button', { name: /approve/i })).toBeDisabled()
    expect(screen.getByText(/only an approver/i)).toBeInTheDocument()
  })

  it('blocks approval of an approver’s own upload and of blocked drafts, but allows rejection', async () => {
    const own = draft({
      requires_bank_verification: false,
      document: { ...draft().document, uploaded_by: 'Dana (approver)' },
    })
    render(<DecisionPanel draft={own} user={approver} onDone={() => {}} />)
    expect(screen.getByRole('button', { name: /approve/i })).toBeDisabled()
    expect(screen.getByText(/another approver/i)).toBeInTheDocument()
    const reject = screen.getByRole('button', { name: /reject/i })
    expect(reject).toBeDisabled()
    await userEvent.type(screen.getByLabelText('Comment'), 'wrong vendor')
    expect(reject).toBeEnabled()
  })

  it('shows the simulated payment once approved', () => {
    render(
      <DecisionPanel
        draft={draft({
          state: 'APPROVED',
          decided_by: 'Dana (approver)',
          payment: { reference: 'SIM-1', amount: '1.00', currency: 'ILS', simulated: true },
          callback: { phone: '03-5551234', person: 'Rina', at: '2025-06-01T10:00:00Z' },
        })}
        user={approver}
        onDone={() => {}}
      />,
    )
    expect(screen.getByText(/No money was moved/)).toBeInTheDocument()
    expect(screen.getByText(/confirmed by phone: 03-5551234, Rina/)).toBeInTheDocument()
  })
})

describe('DraftView', () => {
  it('keeps Hebrew descriptions from reordering line amounts and shows the submitter', async () => {
    const d = draft({
      extracted: {
        vendor_name: 'גלבוע ציוד משרדי',
        vendor_tax_id: '513456780',
        invoice_number: '4471',
        document_type: 'TAX_INVOICE',
        issue_date: '2025-06-01',
        due_date: null,
        currency: 'ILS',
        line_items: [
          { description: 'נייר צילום', quantity: '10', unit_price: '25.50', amount: '255.00' },
        ],
        subtotal: '255.00',
        vat_rate: '0.18',
        vat_amount: '45.90',
        total: '300.90',
        bank: null,
        notes: null,
      },
      document: { ...draft().document, uploaded_by: null, submitted_by_email: 'x@vendor.example' },
    })
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(d), { status: 200 }))),
    )
    render(<DraftView id={7} user={approver} onBack={() => {}} />)
    const line = await screen.findByText(/× ₪25\.50/)
    expect(line.tagName).toBe('LI')
    expect(line.querySelector('bdi')?.textContent).toBe('נייר צילום')
    expect(screen.getByText(/emailed from x@vendor.example/)).toBeInTheDocument()
  })
})

describe('format', () => {
  it('shows every bank field and the currency', () => {
    const a = {
      bank_name: null,
      bank_code: '12',
      branch: '600',
      account: '123456',
      iban: 'IL390126000000000123456',
    }
    expect(bank(a)).toBe('bank 12, branch 600, account 123456, IBAN IL390126000000000123456')
    expect(money('1180.00')).toContain('1,180.00')
    expect(money('1180.00', 'USD')).toMatch(/\$|USD/)
  })
})

describe('App', () => {
  it('signs in and shows the queue', async () => {
    let signedIn = false
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, init?: RequestInit) => {
        if (url === '/api/me')
          return Promise.resolve(new Response('{}', { status: signedIn ? 200 : 401 }))
        if (url === '/api/session' && init?.method === 'POST') {
          signedIn = true
          return Promise.resolve(new Response(JSON.stringify(approver), { status: 200 }))
        }
        if (url.startsWith('/api/drafts')) {
          return Promise.resolve(
            new Response(JSON.stringify([{ ...draft(), extracted: undefined }]), { status: 200 }),
          )
        }
        return Promise.resolve(new Response('[]', { status: 200 }))
      }),
    )
    render(<App />)
    await userEvent.type(await screen.findByLabelText('Password'), 'pw')
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByText('Review queue', { selector: 'h1' })).toBeInTheDocument()
    expect(await screen.findByText('bank details changed')).toBeInTheDocument()
  })
})
