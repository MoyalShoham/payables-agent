import type { Bank, DraftState, Money } from './types'

const ils = new Intl.NumberFormat('en-IL', { style: 'currency', currency: 'ILS' })

/** Display only: the decimal string becomes a number just to be formatted. */
export const money = (m: Money | null | undefined): string =>
  m == null ? '—' : ils.format(Number(m))

export const bank = (b: Bank | null | undefined): string => {
  if (!b) return '—'
  if (b.iban) return b.iban
  return [
    b.bank_name,
    b.bank_code && `bank ${b.bank_code}`,
    b.branch && `branch ${b.branch}`,
    b.account && `account ${b.account}`,
  ]
    .filter(Boolean)
    .join(', ')
}

export const sameAccount = (a: Bank | null | undefined, b: Bank | null | undefined): boolean => {
  if (!a || !b) return true
  const key = (x: Bank) =>
    x.iban
      ? x.iban.replace(/\s/g, '').toUpperCase()
      : [x.bank_code, x.branch, x.account]
          .map((p) => (p ?? '').replace(/\D/g, '').replace(/^0+/, ''))
          .join('-')
  return key(a) === key(b)
}

export const STATE_LABEL: Record<DraftState, string> = {
  READY: 'Ready for approval',
  NEEDS_ATTENTION: 'Needs attention',
  BLOCKED: 'Blocked',
  EXTRACTION_FAILED: 'Could not read',
  APPROVED: 'Approved (simulated payment)',
  REJECTED: 'Rejected',
}

export const when = (iso: string | null): string =>
  iso ? new Date(iso).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' }) : '—'
