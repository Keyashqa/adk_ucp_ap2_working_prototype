import { useState, useEffect } from 'react'
import Auth from './pages/Auth'
import Wallet from './pages/Wallet'
import Chat from './pages/Chat'
import type { AuthState } from './api'
import './App.css'

type Page = 'auth' | 'wallet' | 'chat'

const STORAGE_KEY = 'ucp_auth'

function loadAuth(): AuthState | null {
  try {
    const s = localStorage.getItem(STORAGE_KEY)
    return s ? JSON.parse(s) : null
  } catch { return null }
}

function saveAuth(state: AuthState) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state))
}

function clearAuth() {
  localStorage.removeItem(STORAGE_KEY)
}

export default function App() {
  const [auth, setAuth] = useState<AuthState | null>(loadAuth)
  const [page, setPage] = useState<Page>(auth ? 'wallet' : 'auth')

  useEffect(() => {
    if (!auth) setPage('auth')
  }, [auth])

  const handleAuth = (state: AuthState) => {
    saveAuth(state)
    setAuth(state)
    setPage('wallet')
  }

  const handleLogout = () => {
    clearAuth()
    setAuth(null)
    setPage('auth')
  }

  const handleBalanceChange = (cents: number) => {
    if (auth) {
      const updated = { ...auth, balanceCents: cents }
      saveAuth(updated)
      setAuth(updated)
    }
  }

  if (!auth || page === 'auth') {
    return <Auth onAuth={handleAuth} />
  }

  if (page === 'wallet') {
    return (
      <Wallet
        token={auth.token}
        email={auth.email}
        onNavigate={setPage}
        onLogout={handleLogout}
      />
    )
  }

  return (
    <Chat
      auth={auth}
      onNavigate={setPage}
      onLogout={handleLogout}
      onBalanceChange={handleBalanceChange}
    />
  )
}
