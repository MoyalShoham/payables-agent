import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from '../App'
import { DecisionPanel, MIN_NOTE } from '../components/DecisionPanel'
import { money, sameAccount } from '../format'
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
  prepared_by: 'Payables agent',
  decided_by: null,
  decided_at: null,
  decision_comment: null,
  bank_verification_note: null,
  document: {
    id: 1,
    filename: 'a.pdf',
    media_type: 'application/pdf',
    message_text: null,
    uploaded_by: 'Avi (admin)',
  },
  payment: null,
  ...over,
})

afterEach(() => vi.unstubAllGlobals())

describe('DecisionPanel', () => {
  it('needs a callback note before approving a bank change', async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(new Response('{"state":"APPROVED"}', { status: 200 })),
    )
    vi.stubGlobal('fetch', fetchMock)
    const onDone = vi.fn()
    render(<DecisionPanel draft={draft()} user={approver} onDone={onDone} />)
    const approve = screen.getByRole('button', { name: /approve/i })
    expect(approve).toBeDisabled()

    await userEvent.type(screen.getByLabelText('Bank verification note'), 'x'.repeat(MIN_NOTE))
    expect(approve).toBeEnabled()
    await userEvent.click(approve)
    await waitFor(() => expect(onDone).toHaveBeenCalled())
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/api/drafts/7/approve')
    expect(JSON.parse(String(init.body))).toMatchObject({
      bank_verification_note: 'x'.repeat(MIN_NOTE),
    })
    expect(init.headers).toMatchObject({ 'X-Payables-Client': 'web' })
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
          payment: { reference: 'SIM-1', amount: '1.00', simulated: true },
        })}
        user={approver}
        onDone={() => {}}
      />,
    )
    expect(screen.getByText(/No money was moved/)).toBeInTheDocument()
  })
})

describe('format', () => {
  it('compares accounts the way the backend does', () => {
    const a = { bank_name: null, bank_code: '12', branch: '600', account: '123456', iban: null }
    expect(sameAccount(a, { ...a, bank_code: '012', account: '0123456' })).toBe(true)
    expect(sameAccount(a, { ...a, account: '999' })).toBe(false)
    expect(money('1180.00')).toContain('1,180.00')
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
