"""SQLite database setup and helpers for the merchant server."""
from __future__ import annotations

import sqlite3

from app.config import DB_PATH


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS theaters (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        location    TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS movies (
        id           TEXT PRIMARY KEY,
        title        TEXT NOT NULL,
        genre        TEXT NOT NULL,
        duration_min INTEGER NOT NULL,
        rating       TEXT NOT NULL,
        language     TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS theater_movies (
        theater_id TEXT NOT NULL REFERENCES theaters(id),
        movie_id   TEXT NOT NULL REFERENCES movies(id),
        PRIMARY KEY (theater_id, movie_id)
    );

    CREATE TABLE IF NOT EXISTS showtimes (
        id          TEXT PRIMARY KEY,
        theater_id  TEXT NOT NULL REFERENCES theaters(id),
        movie_id    TEXT NOT NULL REFERENCES movies(id),
        slot        TEXT NOT NULL,
        time_label  TEXT NOT NULL,
        seats_total INTEGER NOT NULL DEFAULT 100,
        seats_sold  INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS seat_categories (
        code        TEXT NOT NULL,
        theater_id  TEXT NOT NULL REFERENCES theaters(id),
        label       TEXT NOT NULL,
        price_cents INTEGER NOT NULL,
        PRIMARY KEY (code, theater_id)
    );

    CREATE TABLE IF NOT EXISTS checkout_sessions (
        session_id       TEXT PRIMARY KEY,
        theater_id       TEXT NOT NULL,
        movie_id         TEXT NOT NULL,
        slot             TEXT NOT NULL,
        seat_code        TEXT NOT NULL,
        qty              INTEGER NOT NULL,
        total_cents      INTEGER NOT NULL,
        expires_at       TEXT NOT NULL,
        status           TEXT NOT NULL DEFAULT 'pending',
        cart_mandate_jwt TEXT,
        created_at       TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS bookings (
        id                  TEXT PRIMARY KEY,
        session_id          TEXT NOT NULL REFERENCES checkout_sessions(session_id),
        payment_mandate_jwt TEXT NOT NULL,
        payment_mandate_id  TEXT NOT NULL,
        user_public_jwk     TEXT NOT NULL,
        charged_cents       INTEGER NOT NULL,
        confirmed_at        TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """)
    conn.commit()
    conn.close()
