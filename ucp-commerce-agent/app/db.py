"""SQLite database setup for the agent server (users, wallets, sessions)."""
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
    CREATE TABLE IF NOT EXISTS users (
        id            TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
        email         TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        pin_hash      TEXT NOT NULL,
        created_at    TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS user_keys (
        user_id     TEXT PRIMARY KEY REFERENCES users(id),
        private_jwk TEXT NOT NULL,
        public_jwk  TEXT NOT NULL,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS wallets (
        id            TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
        user_id       TEXT UNIQUE NOT NULL REFERENCES users(id),
        balance_cents INTEGER NOT NULL DEFAULT 0,
        updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS wallet_transactions (
        id           TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
        wallet_id    TEXT NOT NULL REFERENCES wallets(id),
        delta_cents  INTEGER NOT NULL,
        reason       TEXT NOT NULL,
        reference_id TEXT,
        created_at   TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS auth_sessions (
        token      TEXT PRIMARY KEY,
        user_id    TEXT NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        expires_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS adk_sessions (
        adk_session_id TEXT PRIMARY KEY,
        user_id        TEXT NOT NULL REFERENCES users(id),
        created_at     TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """)
    conn.commit()
    conn.close()
