import { useEffect, useState } from 'react'
import { apiGetWallet, apiTopup, type Transaction } from '../api'

interface Props {
  token: string
  onNavigate: (page: 'chat') => void
  onLogout: () => void
  email: string
}

const TOPUP_AMOUNTS = [500, 1000, 2000, 5000] // cents

export default function Wallet({ token, onNavigate, onLogout, email }: Props) {
  const [balance, setBalance] = useState(0)
  const [transactions, setTransactions] = useState<Transaction[]>([])
  const [loading, setLoading] = useState(true)
  const [topping, setTopping] = useState(false)

  const load = async () => {
    try {
      const d = await apiGetWallet(token)
      setBalance(d.balanceCents)
      setTransactions(d.transactions)
    } catch { /* ignore */ }
    finally { setLoading(false) }
  }

  useEffect(() => { load() }, [])

  const topup = async (cents: number) => {
    setTopping(true)
    try {
      const b = await apiTopup(token, cents)
      setBalance(b)
      await load()
    } finally { setTopping(false) }
  }

  const fmt$ = (c: number) => `$${(c / 100).toFixed(2)}`

  return (
    <div className="page-wide">
      <div className="wallet-header">
        <h2>👋 {email}</h2>
        <div className="nav-links">
          <button className="btn-secondary" onClick={() => onNavigate('chat')}>Chat →</button>
          <button className="btn-secondary" onClick={onLogout}>Sign Out</button>
        </div>
      </div>

      <div className="balance-card">
        <div className="balance-amount">{loading ? '…' : fmt$(balance)}</div>
        <div className="balance-label">Wallet Balance</div>
        <div className="topup-row">
          {TOPUP_AMOUNTS.map(amt => (
            <button key={amt} className="topup-btn" onClick={() => topup(amt)} disabled={topping}>
              + {fmt$(amt)}
            </button>
          ))}
        </div>
      </div>

      <section>
        <h3>Transaction History</h3>
        {transactions.length === 0 ? (
          <p style={{ color: '#718096', padding: '.5rem 0' }}>No transactions yet.</p>
        ) : (
          <div className="txn-list">
            {transactions.map(t => (
              <div key={t.id} className="txn-item">
                <div>
                  <div>{t.reason}</div>
                  {t.reference_id && <div className="txn-label">{t.reference_id}</div>}
                </div>
                <div>
                  <span className={t.delta_cents > 0 ? 'txn-credit' : 'txn-debit'}>
                    {t.delta_cents > 0 ? '+' : ''}{fmt$(t.delta_cents)}
                  </span>
                  <div className="txn-label">{new Date(t.created_at).toLocaleString()}</div>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
