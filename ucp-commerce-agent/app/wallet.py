"""Wallet operations: balance, deduct, deposit, transactions."""
from __future__ import annotations

from app.db import get_conn


async def get_balance(user_id: str) -> int:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT balance_cents FROM wallets WHERE user_id=?", (user_id,)
        ).fetchone()
        return row["balance_cents"] if row else 0
    finally:
        conn.close()


async def deposit(user_id: str, amount_cents: int, reason: str = "topup", reference_id: str | None = None) -> int:
    conn = get_conn()
    try:
        row = conn.execute("SELECT id FROM wallets WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            raise ValueError(f"Wallet not found for user: {user_id}")
        wallet_id = row["id"]
        conn.execute(
            "UPDATE wallets SET balance_cents = balance_cents + ?, updated_at = datetime('now') WHERE id=?",
            (amount_cents, wallet_id),
        )
        conn.execute(
            "INSERT INTO wallet_transactions (wallet_id, delta_cents, reason, reference_id) VALUES (?,?,?,?)",
            (wallet_id, amount_cents, reason, reference_id),
        )
        conn.commit()
        return conn.execute("SELECT balance_cents FROM wallets WHERE id=?", (wallet_id,)).fetchone()["balance_cents"]
    finally:
        conn.close()


async def deduct(user_id: str, amount_cents: int, reason: str = "booking", reference_id: str | None = None) -> int:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, balance_cents FROM wallets WHERE user_id=?", (user_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"Wallet not found for user: {user_id}")
        wallet_id = row["id"]
        if row["balance_cents"] < amount_cents:
            raise ValueError(
                f"Insufficient funds: balance {row['balance_cents']}¢, need {amount_cents}¢"
            )
        conn.execute(
            "UPDATE wallets SET balance_cents = balance_cents - ?, updated_at = datetime('now') WHERE id=?",
            (amount_cents, wallet_id),
        )
        conn.execute(
            "INSERT INTO wallet_transactions (wallet_id, delta_cents, reason, reference_id) VALUES (?,?,?,?)",
            (wallet_id, -amount_cents, reason, reference_id),
        )
        conn.commit()
        return conn.execute("SELECT balance_cents FROM wallets WHERE id=?", (wallet_id,)).fetchone()["balance_cents"]
    finally:
        conn.close()


def get_transactions(user_id: str, limit: int = 20) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT wt.* FROM wallet_transactions wt
               JOIN wallets w ON w.id = wt.wallet_id
               WHERE w.user_id=?
               ORDER BY wt.created_at DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
