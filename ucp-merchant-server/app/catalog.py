"""Seed data and catalog query functions for the cinema merchant server."""
from __future__ import annotations

from app.db import get_conn

# ── Seed data ─────────────────────────────────────────────────────────────────

THEATERS = [
    {"id": "pvr-001", "name": "PVR Cinemas", "location": "Phoenix Mall, 2nd Floor"},
    {"id": "inox-001", "name": "INOX Multiplex", "location": "Orion Mall, 3rd Floor"},
]

MOVIES = [
    {"id": "mov-001", "title": "Interstellar Redux",     "genre": "Sci-Fi",          "duration_min": 169, "rating": "U/A", "language": "English"},
    {"id": "mov-002", "title": "Dune: Messiah",           "genre": "Sci-Fi/Epic",     "duration_min": 155, "rating": "U/A", "language": "English"},
    {"id": "mov-003", "title": "Avengers: Kang Dynasty", "genre": "Action",          "duration_min": 148, "rating": "U/A", "language": "English"},
    {"id": "mov-004", "title": "Leo: The Beginning",     "genre": "Action/Thriller", "duration_min": 162, "rating": "U/A", "language": "Tamil/Hindi"},
]

THEATER_MOVIES = [
    ("pvr-001",  "mov-001"),
    ("pvr-001",  "mov-002"),
    ("pvr-001",  "mov-003"),
    ("inox-001", "mov-001"),
    ("inox-001", "mov-002"),
    ("inox-001", "mov-004"),
]

SHOWS = [
    {"slot": "A", "time_label": "10:00 AM"},
    {"slot": "B", "time_label": "2:30 PM"},
    {"slot": "C", "time_label": "7:00 PM"},
    {"slot": "D", "time_label": "10:15 PM"},
]

SEAT_CATEGORIES = [
    {"code": "S", "label": "Standard",         "price_cents": 1200},
    {"code": "P", "label": "Premium Recliner", "price_cents": 1800},
    {"code": "I", "label": "IMAX",             "price_cents": 2200},
]


def seed_db() -> None:
    """Insert seed data if tables are empty."""
    conn = get_conn()
    try:
        if conn.execute("SELECT COUNT(*) FROM theaters").fetchone()[0] > 0:
            return

        for t in THEATERS:
            conn.execute("INSERT INTO theaters VALUES (?,?,?)", (t["id"], t["name"], t["location"]))

        for m in MOVIES:
            conn.execute("INSERT INTO movies VALUES (?,?,?,?,?,?)",
                         (m["id"], m["title"], m["genre"], m["duration_min"], m["rating"], m["language"]))

        for theater_id, movie_id in THEATER_MOVIES:
            conn.execute("INSERT INTO theater_movies VALUES (?,?)", (theater_id, movie_id))

        for t in THEATERS:
            for m_id in [x[1] for x in THEATER_MOVIES if x[0] == t["id"]]:
                for s in SHOWS:
                    show_id = f"{t['id']}-{m_id}-{s['slot']}"
                    conn.execute(
                        "INSERT INTO showtimes VALUES (?,?,?,?,?,?,?)",
                        (show_id, t["id"], m_id, s["slot"], s["time_label"], 100, 0),
                    )
            for sc in SEAT_CATEGORIES:
                conn.execute(
                    "INSERT INTO seat_categories VALUES (?,?,?,?)",
                    (sc["code"], t["id"], sc["label"], sc["price_cents"]),
                )

        conn.commit()
    finally:
        conn.close()


# ── Query helpers ──────────────────────────────────────────────────────────────

def get_movies(theater_id: str, query: str = "", limit: int = 10) -> list[dict]:
    conn = get_conn()
    try:
        sql = """
            SELECT m.* FROM movies m
            JOIN theater_movies tm ON tm.movie_id = m.id
            WHERE tm.theater_id = ?
        """
        params: list = [theater_id]
        if query:
            sql += " AND (LOWER(m.title) LIKE ? OR LOWER(m.genre) LIKE ?)"
            q = f"%{query.lower()}%"
            params += [q, q]
        sql += " LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_showtimes(theater_id: str, movie_id: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM showtimes WHERE theater_id=? AND movie_id=? ORDER BY slot",
            (theater_id, movie_id),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_seat_categories(theater_id: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM seat_categories WHERE theater_id=?", (theater_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_theater(theater_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM theaters WHERE id=?", (theater_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_movie(movie_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM movies WHERE id=?", (movie_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_showtime(theater_id: str, movie_id: str, slot: str) -> dict | None:
    conn = get_conn()
    try:
        show_id = f"{theater_id}-{movie_id}-{slot}"
        row = conn.execute("SELECT * FROM showtimes WHERE id=?", (show_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_all_bookings() -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT b.*, cs.theater_id, cs.movie_id, cs.slot, cs.seat_code, cs.qty,
                   cs.total_cents, t.name AS theater_name, m.title AS movie_title
            FROM bookings b
            JOIN checkout_sessions cs ON cs.session_id = b.session_id
            JOIN theaters t ON t.id = cs.theater_id
            JOIN movies m ON m.id = cs.movie_id
            ORDER BY b.confirmed_at DESC
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_stats() -> dict:
    conn = get_conn()
    try:
        total_revenue = conn.execute(
            "SELECT COALESCE(SUM(charged_cents), 0) FROM bookings"
        ).fetchone()[0]
        total_bookings = conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
        total_seats_sold = conn.execute(
            "SELECT COALESCE(SUM(qty), 0) FROM checkout_sessions WHERE status='confirmed'"
        ).fetchone()[0]
        return {
            "total_revenue_cents": total_revenue,
            "total_bookings": total_bookings,
            "total_seats_sold": total_seats_sold,
        }
    finally:
        conn.close()
