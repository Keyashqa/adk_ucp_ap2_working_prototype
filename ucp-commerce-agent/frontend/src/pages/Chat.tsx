import { useEffect, useRef, useState } from 'react'
import {
  apiCreateAdkSession, apiVerifyPin, streamAdkRun,
  type AuthState, type HitlRequest, type Message, type A2uiSurface,
} from '../api'
import A2uiRenderer from '../components/A2uiRenderer'

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
      if (last.role !== 'agent' || last.a2ui) return [...prev, { id: uuid(), role: 'agent' as const, text }]
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
        if (ev.a2ui) {
          setMessages(prev => [...prev, { id: uuid(), role: 'agent', text: '', a2ui: ev.a2ui as A2uiSurface[] }])
          agentMsgStarted = false
        }
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
          // Unlock for all A2UI button-driven HITLs; keep busy for PIN modal
          if (ev.hitl.interruptId !== 'payment_auth') setBusy(false)
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

  // Universal button handler: any A2UI button click resumes the current HITL
  // with the button's action name as the response value. The backend node parses it.
  const handleA2uiAction = (actionName: string) => {
    if (!hitl || busy) return
    const h = hitl
    setHitl(null)
    runStream([{
      functionResponse: {
        id: h.interruptId,
        name: 'adk_request_input',
        response: { result: actionName },
      },
    }])
  }

  const fmt$ = (c: number) => `$${(c / 100).toFixed(2)}`

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
        <h2>🎬 CineAgent</h2>
        <div className="nav-links">
          <span className="wallet-badge" onClick={() => onNavigate('wallet')}>
            💳 {fmt$(auth.balanceCents)}
          </span>
          <button className="btn-secondary" onClick={() => onNavigate('wallet')}>Wallet</button>
          <button className="btn-secondary" onClick={onLogout}>Sign Out</button>
        </div>
      </div>

      <div className="messages">
        {messages.map(m => (
          <div key={m.id} className={`msg msg-${m.role}${m.a2ui ? ' msg-a2ui' : ''}`}>
            {m.a2ui
              ? <A2uiRenderer surfaces={m.a2ui} onAction={handleA2uiAction} disabled={busy} />
              : m.text}
          </div>
        ))}
        {busy && (
          <div className="msg msg-agent">
            <div className="typing"><span /><span /><span /></div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Text input hidden: flow is fully button-driven via A2UI cards */}
    </div>
  )
}
