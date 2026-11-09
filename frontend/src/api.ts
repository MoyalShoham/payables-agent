import type {
  AuditEntry,
  AuditVerification,
  CallbackRecord,
  DraftDetail,
  DraftState,
  DraftSummary,
  Payment,
  User,
  Vendor,
} from './types'

export class ApiError extends Error {
  readonly status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

/** Sent on every write; the API refuses writes without it (CSRF protection). */
export const CLIENT_HEADER = { 'X-Payables-Client': 'web' }

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = init.method ?? 'GET'
  const headers: HeadersInit =
    method === 'GET' ? (init.headers ?? {}) : { ...CLIENT_HEADER, ...(init.headers ?? {}) }
  const response = await fetch(`/api${path}`, { ...init, headers, credentials: 'same-origin' })
  if (!response.ok) {
    let message = `Request failed (${response.status})`
    try {
      const body = (await response.json()) as { detail?: unknown }
      if (typeof body.detail === 'string') message = body.detail
    } catch {
      // not JSON
    }
    throw new ApiError(response.status, message)
  }
  return (await response.json()) as T
}

const json = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

export const api = {
  me: () => request<User>('/me'),
  login: (username: string, password: string) =>
    request<User>('/session', json('POST', { username, password })),
  logout: () => request<{ status: string }>('/session', { method: 'DELETE' }),
  drafts: (state?: DraftState) =>
    request<DraftSummary[]>(`/drafts${state ? `?state=${state}` : ''}`),
  draft: (id: number) => request<DraftDetail>(`/drafts/${id}`),
  approve: (id: number, comment: string, callback: CallbackRecord | null) =>
    request<{ state: string; simulated_payment_id: number }>(
      `/drafts/${id}/approve`,
      json('POST', { comment: comment || null, callback }),
    ),
  recheck: (id: number) => request<{ status: string }>(`/drafts/${id}/recheck`, { method: 'POST' }),
  reject: (id: number, comment: string) =>
    request<{ state: string }>(`/drafts/${id}/reject`, json('POST', { comment })),
  upload: (file: File, messageText: string) => {
    const body = new FormData()
    body.append('file', file)
    if (messageText.trim()) body.append('message_text', messageText.trim())
    return request<{ draft_id: number; duplicate_upload: boolean }>('/documents', {
      method: 'POST',
      body,
    })
  },
  vendors: () => request<Vendor[]>('/vendors'),
  payments: () => request<Payment[]>('/payments'),
  audit: () => request<AuditEntry[]>('/audit'),
  verifyAudit: () => request<AuditVerification>('/audit/verify'),
  documentUrl: (id: number) => `/api/documents/${id}/content`,
  previewUrl: (id: number) => `/api/documents/${id}/preview`,
}
