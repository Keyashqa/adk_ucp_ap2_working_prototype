"""SQLite database setup for the agent server.

Schema v2 — ledger-based wallet (no mutable balance_cents).
"""
from __future__ import annotations

import sqlite3

from app.config import DB_PATH


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row

    # WAL mode — do NOT set foreign_keys here; we need to drop tables freely
    conn.execute("PRAGMA journal_mode=WAL")

    # ── Schema migration v1 → v2 ─────────────────────────────────────────────
    # v1 stored balance_cents on wallets + a wallet_transactions table.
    # v2 removes both and uses an append-only double-entry ledger instead.
    # We detect v1 by the presence of wallet_transactions and migrate in-place.
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }

    if "wallet_transactions" in tables and "ledger" not in tables:
        # Drop v1 wallet tables (child first so no FK issue even with FK=ON)
        conn.execute("DROP TABLE IF EXISTS wallet_transactions")
        conn.execute("DROP TABLE IF EXISTS wallets")
        conn.commit()

    # ── Identity & auth ───────────────────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
            email         TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            pin_hash      TEXT NOT NULL,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_keys (
            user_id     TEXT PRIMARY KEY REFERENCES users(id),
            private_jwk TEXT NOT NULL,
            public_jwk  TEXT NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS auth_sessions (
            token      TEXT PRIMARY KEY,
            user_id    TEXT NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS adk_sessions (
            adk_session_id TEXT PRIMARY KEY,
            user_id        TEXT NOT NULL REFERENCES users(id),
            created_at     TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # ── Wallet (v2 — no balance_cents) ───────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wallets (
            id         TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
            user_id    TEXT UNIQUE NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # ── Double-entry ledger ───────────────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ledger (
            id           TEXT PRIMARY KEY,
            journal_id   TEXT NOT NULL,
            account_id   TEXT NOT NULL,
            delta_cents  INTEGER NOT NULL CHECK(delta_cents != 0),
            counterpart  TEXT NOT NULL,
            reason       TEXT NOT NULL,
            reference_id TEXT,
            prev_hash    TEXT NOT NULL,
            entry_hash   TEXT NOT NULL,
            created_at   TEXT NOT NULL
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_account ON ledger(account_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_journal ON ledger(journal_id)")

    conn.commit()
    conn.close()
