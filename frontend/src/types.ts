/** API types. Amounts are decimal strings and stay strings until they are formatted. */
export type Money = string

export interface User {
  id: number
  username: string
  display_name: string
  role: 'AGENT' | 'APPROVER' | 'ADMIN'
}

export type DraftState =
  'READY' | 'NEEDS_ATTENTION' | 'BLOCKED' | 'EXTRACTION_FAILED' | 'APPROVED' | 'REJECTED'

export interface DraftSummary {
  id: number
  state: DraftState
  vendor: string | null
  invoice_number: string | null
  amount: Money | null
  currency: string
  flags: string[]
  failed_checks: string[]
  requires_bank_verification: boolean
  created_at: string
}

export interface Bank {
  bank_name: string | null
  bank_code: string | null
  branch: string | null
  account: string | null
  iban: string | null
}

export interface LineItem {
  description: string
  quantity: string | null
  unit_price: Money | null
  amount: Money
}

export interface Extracted {
  vendor_name: string | null
  vendor_tax_id: string | null
  invoice_number: string | null
  document_type: string
  issue_date: string | null
  due_date: string | null
  currency: string
  line_items: LineItem[]
  subtotal: Money | null
  vat_rate: string | null
  vat_amount: Money | null
  total: Money | null
  bank: Bank | null
  notes: string | null
}

export interface Check {
  name: string
  status: 'PASS' | 'WARN' | 'FAIL' | 'SKIP'
  detail: string
}

export interface FlagDetail {
  code: string
  severity: 'HIGH' | 'MEDIUM'
  detail: string
}

/** The phone call that confirmed a vendor's bank account before a bank-change payment. */
export interface CallbackRecord {
  phone: string
  person: string
  at: string
}

export interface DraftDetail extends DraftSummary {
  extracted: Extracted | null
  extraction_error: string | null
  model: string
  checks: Check[]
  flag_details: FlagDetail[]
  reasoning: string[]
  pay_to: Bank | null
  /** Contact numbers on the vendor master: the only numbers a callback may use. */
  vendor_phones: string[]
  /** The vendor master's account changed after drafting; approval needs a re-check first. */
  master_changed: boolean
  prepared_by: string | null
  decided_by: string | null
  decided_at: string | null
  decision_comment: string | null
  callback: CallbackRecord | null
  document: {
    id: number
    filename: string
    media_type: string
    source: string
    message_text: string | null
    uploaded_by: string | null
    submitted_by_email: string | null
  }
  payment: { reference: string; amount: Money; currency: string; simulated: true } | null
}

export interface Vendor {
  id: number
  tax_id: string
  name: string
  bank: Bank | null
  phones: string[]
}

export interface AuditVerification {
  intact: boolean
  entries: number
  first_broken_entry: number | null
  problem: string | null
  checkpoint: { seq: number; hash: string; at: string } | null
}

export interface AuditEntry {
  id: number
  at: string
  actor: string
  action: string
  entity: string
  entity_id: number | null
  details: Record<string, unknown>
  hash: string
}

export interface Payment {
  id: number
  draft_id: number
  amount: Money
  currency: string
  pay_to: Bank
  reference: string
  created_at: string
  simulated: true
}
