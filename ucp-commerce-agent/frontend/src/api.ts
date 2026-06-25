const AGENT = 'http://localhost:8000'

export interface AuthState {
  token: string
  userId: string
  email: string
  balanceCents: number
}

export async function apiRegister(email: string, password: string, pin: string): Promise<AuthState> {
  const r = await fetch(`${AGENT}/auth/register`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password, pin }),
  })
  if (!r.ok) { const e = await r.json(); throw new Error(e.detail || 'Registration failed') }
  const d = await r.json()
  return { token: d.token, userId: d.user_id, email: d.email, balanceCents: d.balance_cents }
}

export async function apiLogin(email: string, password: string): Promise<AuthState> {
  const r = await fetch(`${AGENT}/auth/login`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
  if (!r.ok) { const e = await r.json(); throw new Error(e.detail || 'Login failed') }
  const d = await r.json()
  return { token: d.token, userId: d.user_id, email: d.email, balanceCents: d.balance_cents }
}

export async function apiCreateAdkSession(token: string): Promise<string> {
  const r = await fetch(`${AGENT}/adk-sessions`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
  })
  if (!r.ok) { const e = await r.json(); throw new Error(e.detail || 'Session creation failed') }
  const d = await r.json()
  return d.adk_session_id
}

export async function apiVerifyPin(token: string, pin: string, adkSessionId: string): Promise<boolean> {
  const r = await fetch(`${AGENT}/auth/verify-pin`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token, pin, adk_session_id: adkSessionId }),
  })
  if (!r.ok) return false
  const d = await r.json()
  return d.ok === true
}

export async function apiGetWallet(token: string): Promise<{ balanceCents: number; transactions: Transaction[] }> {
  const r = await fetch(`${AGENT}/wallet/balance?token=${encodeURIComponent(token)}`)
  if (!r.ok) throw new Error('Failed to load wallet')
  const d = await r.json()
  return { balanceCents: d.balance_cents, transactions: d.transactions }
}

export async function apiTopup(token: string, amountCents: number): Promise<number> {
  const r = await fetch(`${AGENT}/wallet/topup`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token, amount_cents: amountCents }),
  })
  if (!r.ok) throw new Error('Topup failed')
  const d = await r.json()
  return d.balance_cents
}

export interface Transaction {
  id: string
  delta_cents: number
  reason: string
  reference_id: string | null
  created_at: string
}

export interface HitlRequest {
  interruptId: string
  question: string
}

export interface Message {
  id: string
  role: 'user' | 'agent' | 'system'
  text: string
}

export async function* streamAdkRun(
  userId: string,
  sessionId: string,
  parts: object[],
): AsyncGenerator<{ text?: string; hitl?: HitlRequest; done?: boolean }> {
  const body = JSON.stringify({
    app_name: 'app',
    user_id: userId,
    session_id: sessionId,
    streaming: true,
    new_message: { role: 'user', parts },
  })

  const resp = await fetch(`${AGENT}/run_sse`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body,
  })

  if (!resp.ok || !resp.body) {
    throw new Error(`SSE failed: ${resp.status}`)
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    const lines = buf.split('\n')
    buf = lines.pop() ?? ''
    for (const line of lines) {
      if (!line.startsWith('data:')) continue
      const raw = line.slice(5).trim()
      if (!raw || raw === '[DONE]') continue
      try {
        const ev = JSON.parse(raw)
        const parts = ev?.content?.parts ?? []
        for (const p of parts) {
          if (p.text) yield { text: p.text }
          if (p.functionCall?.name === 'adk_request_input') {
            yield {
              hitl: {
                interruptId: p.functionCall.id ?? p.functionCall.args?.interrupt_id ?? 'unknown',
                question: p.functionCall.args?.question ?? '',
              },
            }
          }
        }
      } catch { /* ignore malformed */ }
    }
  }
  yield { done: true }
}
