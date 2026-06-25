import { useEffect, useRef, useState } from 'react'
import {
  apiCreateAdkSession, apiVerifyPin, streamAdkRun,
  type AuthState, type HitlRequest, type Message
} from '../api'

interface Props {
  auth: AuthState
  onNavigate: (page: 'wallet') => void
  onLogout: () => void
  onBalanceChange?: (cents: number) => void
}

const uuid = () => Math.random().toString(36).slice(2)

export default function Chat({ auth, onNavigate, onLogout }: Props) {
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [hitl, setHitl] = useState<HitlRequest | null>(null)

  // PIN modal state
  const [pinValue, setPinValue] = useState('')
  const [pinError, setPinError] = useState('')
  const [pinLoading, setPinLoading] = useState(false)

  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    initSession()
  }, [])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const addMsg = (role: Message['role'], text: string) => {
    setMessages(prev => [...prev, { id: uuid(), role, text }])
  }

  const appendToLast = (text: string) => {
    setMessages(prev => {
      if (prev.length === 0) return prev
      const last = prev[prev.length - 1]
      if (last.role !== 'agent') return [...prev, { id: uuid(), role: 'agent', text }]
      return [...prev.slice(0, -1), { ...last, text: last.text + text }]
    })
  }

  // Core streaming function — runs parts through the ADK SSE endpoint.
  // explicitSid is used during init (before sessionId state propagates).
  const runStream = async (parts: object[], explicitSid?: string) => {
    const sid = explicitSid ?? sessionId
    if (!sid) return
    setBusy(true)
    let agentMsgStarted = false
    let receivedHitl = false
    try {
      for await (const ev of streamAdkRun(auth.userId, sid, parts)) {
        if (ev.text) {
          if (!agentMsgStarted) {
            agentMsgStarted = true
            setMessages(prev => [...prev, { id: uuid(), role: 'agent', text: ev.text! }])
          } else {
            appendToLast(ev.text)
          }
        }
        if (ev.hitl) {
          receivedHitl = true
          setHitl(ev.hitl)
          // chat_turn = normal conversation pause → unlock input immediately
          if (ev.hitl.interruptId === 'chat_turn') setBusy(false)
          // payment_auth → PIN modal appears; busy stays true to block the text input
        }
        if (ev.done && !receivedHitl) setBusy(false)
      }
    } catch (err) {
      addMsg('system', `❌ Error: ${err instanceof Error ? err.message : String(err)}`)
      setBusy(false)
    }
  }

  // On session creation, send a silent init trigger so the agent shows its welcome
  // message and establishes the first chat_turn HITL before the user types anything.
  const initSession = async () => {
    try {
      const sid = await apiCreateAdkSession(auth.token)
      setSessionId(sid)
      await runStream([{ text: 'start' }], sid)
    } catch (e) {
      addMsg('system', '❌ Failed to create session. Is the agent server running on port 8000?')
      setBusy(false)
    }
  }

  const sendMessage = async () => {
    const text = input.trim()
    if (!text || !sessionId) return
    setInput('')
    addMsg('user', text)

    if (hitl?.interruptId === 'chat_turn') {
      // Every conversational turn resumes via the chat_turn HITL
      const capturedHitl = hitl
      setHitl(null)
      await runStream([{
        functionResponse: {
          id: capturedHitl.interruptId,
          name: 'adk_request_input',
          response: { result: text },
        },
      }])
    } else {
      // No active HITL (e.g. workflow restarted externally) — plain text trigger
      await runStream([{ text }])
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage() }
  }

  // PIN modal — verify PIN then resume ADK with "confirmed"
  const submitPin = async () => {
    if (!hitl || !sessionId) return
    setPinError('')
    setPinLoading(true)
    try {
      const ok = await apiVerifyPin(auth.token, pinValue, sessionId)
      if (!ok) {
        setPinError('Incorrect PIN. Try again.')
        setPinLoading(false)
        return
      }
      const capturedHitl = hitl
      setHitl(null)
      setPinValue('')
      setPinLoading(false)
      await runStream([{
        functionResponse: {
          id: capturedHitl.interruptId,
          name: 'adk_request_input',
          response: { result: 'confirmed' },
        },
      }])
    } catch {
      setPinError('Verification failed. Try again.')
      setPinLoading(false)
    }
  }

  const cancelPin = () => {
    const capturedHitl = hitl
    setHitl(null)
    setPinValue('')
    setPinError('')
    setBusy(false)
    addMsg('system', '⚠️ Payment cancelled.')
    if (sessionId && capturedHitl) {
      runStream([{
        functionResponse: {
          id: capturedHitl.interruptId,
          name: 'adk_request_input',
          response: { result: 'cancelled' },
        },
      }])
    }
  }

  const fmt$ = (c: number) => `$${(c / 100).toFixed(2)}`

  // Input is disabled while the LLM is running (busy=true).
  // Once chat_turn HITL arrives, busy is set to false → input re-enabled.
  // During payment_auth HITL, busy stays true (PIN modal handles input instead).
  const inputDisabled = busy

  return (
    <div className="chat-layout">
      {/* PIN Modal */}
      {hitl?.interruptId === 'payment_auth' && (
        <div className="modal-overlay">
          <div className="modal-card">
            <h2>🔐 Confirm Payment</h2>
            <p>Enter your PIN to authorise this booking</p>
            <input
              className="pin-input"
              type="password" inputMode="numeric" maxLength={6}
              value={pinValue} onChange={e => setPinValue(e.target.value)}
              autoFocus
              onKeyDown={e => e.key === 'Enter' && submitPin()}
            />
            {pinError && <p className="error-msg">{pinError}</p>}
            <div className="modal-actions">
              <button className="btn-cancel" onClick={cancelPin}>Cancel</button>
              <button
                className="btn-confirm"
                onClick={submitPin}
                disabled={pinLoading || pinValue.length < 4}
              >
                {pinLoading ? 'Verifying…' : 'Confirm'}
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="chat-topbar">
        <h2>🎬 Cinema Agent</h2>
        <div className="nav-links">
          <span
            className="wallet-badge"
            style={{ cursor: 'pointer' }}
            onClick={() => onNavigate('wallet')}
          >
            💳 {fmt$(auth.balanceCents)}
          </span>
          <button className="btn-secondary" onClick={() => onNavigate('wallet')}>Wallet</button>
          <button className="btn-secondary" onClick={onLogout}>Sign Out</button>
        </div>
      </div>

      <div className="messages">
        {messages.map(m => (
          <div key={m.id} className={`msg msg-${m.role}`}>{m.text}</div>
        ))}
        {busy && (
          <div className="msg msg-agent">
            <div className="typing"><span /><span /><span /></div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      <div className="chat-input-area">
        <div className="chat-input-row">
          <input
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask about movies, shows, or book a ticket…"
            disabled={inputDisabled}
          />
          <button
            className="send-btn"
            onClick={sendMessage}
            disabled={inputDisabled || !input.trim()}
          >
            Send
          </button>
        </div>
      </div>
    </div>
  )
}
