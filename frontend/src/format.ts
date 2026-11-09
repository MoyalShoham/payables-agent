import type { Bank, DraftState, Money } from './types'

const formats = new Map<string, Intl.NumberFormat>()

const formatter = (currency: string): Intl.NumberFormat => {
  let f = formats.get(currency)
  if (!f) {
    try {
      f = new Intl.NumberFormat('en-IL', { style: 'currency', currency })
    } catch {
      // Not an ISO code the browser knows: show the code next to the number.
      f = new Intl.NumberFormat('en-IL', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    }
    formats.set(currency, f)
  }
  return f
}

/** Display only: the decimal string becomes a number just to be formatted. */
export const money = (m: Money | null | undefined, currency = 'ILS'): string => {
  if (m == null) return '—'
  const text = formatter(currency).format(Number(m))
  return /^[A-Z]{3}$/.test(currency) && !text.match(/[^\d.,\s-]/) ? `${text} ${currency}` : text
}

/**
 * Every field that is present: an IBAN and a local account can disagree, so both are shown. Meant
 * for a left-to-right element; a Hebrew bank name sits beside its code so it cannot reorder the
 * numbers.
 */
export const bank = (b: Bank | null | undefined): string => {
  if (!b) return '—'
  const code = b.bank_code
    ? `bank ${b.bank_code}${b.bank_name ? ` ${b.bank_name}` : ''}`
    : b.bank_name
  const parts = [
    code,
    b.branch && `branch ${b.branch}`,
    b.account && `account ${b.account}`,
    b.iban && `IBAN ${b.iban}`,
  ].filter(Boolean)
  return parts.length ? parts.join(', ') : '—'
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
