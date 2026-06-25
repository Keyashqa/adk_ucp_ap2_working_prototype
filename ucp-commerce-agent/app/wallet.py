"""Ledger-based wallet operations.

Architecture
------------
Balance is NEVER stored as a mutable number. It is always derived:

    balance = SELECT SUM(delta_cents) FROM ledger WHERE account_id = ?

Every transfer (topup, booking payment) creates exactly TWO ledger rows inside
one SQLite IMMEDIATE transaction (double-entry bookkeeping):

    Topup $50:
      account=system      delta=-5000  (system gives money)
      account=<user_id>   delta=+5000  (user receives money)

    Booking $36:
      account=<user_id>   delta=-3600  (user pays)
      account=system      delta=+3600  (platform receives)

The sum of ALL delta_cents across ALL accounts always equals zero.

Hash chain
----------
Every row for a given account_id includes:
  prev_hash  — entry_hash of the most-recent prior row for that account
  entry_hash — sha256(id | journal_id | account_id | delta_cents | counterpart
                      | reason | reference_id | created_at | prev_hash)

If any row is tampered with, its hash changes → the next row's prev_hash no
longer matches → verify_chain() detects the break immediately.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from app.db import get_conn

SYSTEM = "system"
GENESIS_HASH = "0" * 64


# ── Internal helpers ───────────────────────────────────────────────────────────

def _sha256(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _prev_hash(conn, account_id: str) -> str:
    """Return entry_hash of the latest ledger row for this account, or genesis."""
    row = conn.execute(
        "SELECT entry_hash FROM ledger WHERE account_id=? ORDER BY rowid DESC LIMIT 1",
        (account_id,),
    ).fetchone()
    return row["entry_hash"] if row else GENESIS_HASH


def _insert_entry(
    conn,
    journal_id: str,
    account_id: str,
    delta_cents: int,
    counterpart: str,
    reason: str,
    reference_id: str | None,
    now: str,
) -> str:
    """Insert one side of a double-entry. Returns the computed entry_hash."""
    entry_id = uuid.uuid4().hex
    ph = _prev_hash(conn, account_id)
    entry_hash = _sha256(
        entry_id, journal_id, account_id,
        str(delta_cents), counterpart, reason,
        reference_id or "", now, ph,
    )
    conn.execute(
        """INSERT INTO ledger
               (id, journal_id, account_id, delta_cents, counterpart,
                reason, reference_id, prev_hash, entry_hash, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (entry_id, journal_id, account_id, delta_cents, counterpart,
         reason, reference_id, ph, entry_hash, now),
    )
    return entry_hash


def _double_entry(
    conn,
    debit_account: str,
    credit_account: str,
    amount_cents: int,
    reason: str,
    reference_id: str | None,
) -> str:
    """Write debit + credit atomically. Returns journal_id.

    The caller must hold an IMMEDIATE transaction so both inserts are atomic.
    Debit side  → delta = -amount  (money leaves debit_account)
    Credit side → delta = +amount  (money enters credit_account)
    """
    journal_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    _insert_entry(conn, journal_id, debit_account,  -amount_cents, credit_account, reason, reference_id, now)
    _insert_entry(conn, journal_id, credit_account, +amount_cents, debit_account,  reason, reference_id, now)
    return journal_id


def _balance_sync(conn, account_id: str) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(delta_cents), 0) AS bal FROM ledger WHERE account_id=?",
        (account_id,),
    ).fetchone()["bal"]


# ── Public API ─────────────────────────────────────────────────────────────────

async def get_balance(user_id: str) -> int:
    """Derive the current balance by summing all ledger entries for this user."""
    conn = get_conn()
    try:
        return _balance_sync(conn, user_id)
    finally:
        conn.close()


async def deposit(
    user_id: str,
    amount_cents: int,
    reason: str = "topup",
    reference_id: str | None = None,
) -> int:
    """Credit user wallet (system → user). Returns new balance."""
    if amount_cents <= 0:
        raise ValueError("deposit amount must be positive")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _double_entry(conn, SYSTEM, user_id, amount_cents, reason, reference_id)
        conn.commit()
        return _balance_sync(conn, user_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def deduct(
    user_id: str,
    amount_cents: int,
    reason: str = "booking",
    reference_id: str | None = None,
) -> int:
    """Debit user wallet (user → system). Returns new balance."""
    if amount_cents <= 0:
        raise ValueError("deduct amount must be positive")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        balance = _balance_sync(conn, user_id)
        if balance < amount_cents:
            raise ValueError(
                f"Insufficient funds: balance {balance}¢, need {amount_cents}¢"
            )
        _double_entry(conn, user_id, SYSTEM, amount_cents, reason, reference_id)
        conn.commit()
        return _balance_sync(conn, user_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_transactions(user_id: str, limit: int = 20) -> list[dict]:
    """Return the most-recent ledger entries for a user (their account_id)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT id, journal_id, delta_cents, counterpart, reason,
                      reference_id, created_at
               FROM ledger
               WHERE account_id=?
               ORDER BY rowid DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def verify_chain(user_id: str) -> dict:
    """Walk every ledger entry for this user and recompute all hashes.

    Returns {"valid": True, "entries": N}  if the chain is intact.
    Returns {"valid": False, "error": "..."}  if any row was tampered with.

    This is the tamper-detection mechanism: editing any row (even in the raw
    DB file) breaks the hash chain at that point and this function catches it.
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT id, journal_id, account_id, delta_cents, counterpart, reason,
                      reference_id, prev_hash, entry_hash, created_at
               FROM ledger WHERE account_id=? ORDER BY rowid ASC""",
            (user_id,),
        ).fetchall()

        expected_prev = GENESIS_HASH
        for row in rows:
            if row["prev_hash"] != expected_prev:
                return {
                    "valid": False,
                    "error": f"Chain broken at entry {row['id']}: "
                             f"prev_hash mismatch (expected {expected_prev[:16]}…, "
                             f"got {row['prev_hash'][:16]}…)",
                }
            recomputed = _sha256(
                row["id"], row["journal_id"], row["account_id"],
                str(row["delta_cents"]), row["counterpart"], row["reason"],
                row["reference_id"] or "", row["created_at"], row["prev_hash"],
            )
            if row["entry_hash"] != recomputed:
                return {
                    "valid": False,
                    "error": f"Hash corrupted at entry {row['id']}: "
                             f"stored {row['entry_hash'][:16]}… ≠ "
                             f"computed {recomputed[:16]}…",
                }
            expected_prev = row["entry_hash"]

        return {"valid": True, "entries": len(rows)}
    finally:
        conn.close()
