import { useEffect, useState } from 'react'
import './App.css'

const API = 'http://localhost:8001'

interface Booking {
  id: string
  session_id: string
  theater_name: string
  movie_title: string
  slot: string
  seat_code: string
  qty: number
  charged_cents: number
  confirmed_at: string
}

interface Stats {
  total_revenue_cents: number
  total_bookings: number
  total_seats_sold: number
}

function App() {
  const [bookings, setBookings] = useState<Booking[]>([])
  const [stats, setStats] = useState<Stats | null>(null)
  const [loading, setLoading] = useState(true)

  const fetchData = async () => {
    setLoading(true)
    try {
      const [bRes, sRes] = await Promise.all([
        fetch(`${API}/bookings`),
        fetch(`${API}/stats`),
      ])
      setBookings(await bRes.json())
      setStats(await sRes.json())
    } catch (e) {
      console.error(e)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchData()
    const t = setInterval(fetchData, 10000)
    return () => clearInterval(t)
  }, [])

  const fmt$ = (cents: number) => `$${(cents / 100).toFixed(2)}`

  return (
    <div className="dashboard">
      <header>
        <h1>🎬 UCP Cinema Merchant Dashboard</h1>
        <button onClick={fetchData} className="refresh-btn">↻ Refresh</button>
      </header>

      {stats && (
        <div className="stats-grid">
          <div className="stat-card">
            <span className="stat-value">{stats.total_bookings}</span>
            <span className="stat-label">Total Bookings</span>
          </div>
          <div className="stat-card">
            <span className="stat-value">{fmt$(stats.total_revenue_cents)}</span>
            <span className="stat-label">Total Revenue</span>
          </div>
          <div className="stat-card">
            <span className="stat-value">{stats.total_seats_sold}</span>
            <span className="stat-label">Seats Sold</span>
          </div>
        </div>
      )}

      <section>
        <h2>Recent Bookings</h2>
        {loading ? (
          <p className="loading">Loading...</p>
        ) : bookings.length === 0 ? (
          <p className="empty">No bookings yet. Book a movie from the agent UI!</p>
        ) : (
          <table className="bookings-table">
            <thead>
              <tr>
                <th>Booking ID</th>
                <th>Movie</th>
                <th>Theater</th>
                <th>Show</th>
                <th>Seats</th>
                <th>Amount</th>
                <th>Confirmed</th>
              </tr>
            </thead>
            <tbody>
              {bookings.map(b => (
                <tr key={b.id}>
                  <td className="mono">{b.id}</td>
                  <td>{b.movie_title}</td>
                  <td>{b.theater_name}</td>
                  <td>Slot {b.slot}</td>
                  <td>{b.seat_code} × {b.qty}</td>
                  <td className="amount">{fmt$(b.charged_cents)}</td>
                  <td className="timestamp">{new Date(b.confirmed_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  )
}

export default App
