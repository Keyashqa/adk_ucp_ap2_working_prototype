"""Auth router: register, login, verify-pin, adk-sessions, wallet endpoints."""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt as _bcrypt
import httpx
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, HTTPException
from jwcrypto.jwk import JWK
from pydantic import BaseModel


def _hash_secret(secret: str) -> str:
    return _bcrypt.hashpw(secret.encode(), _bcrypt.gensalt()).decode()


def _verify_secret(secret: str, hashed: str) -> bool:
    return _bcrypt.checkpw(secret.encode(), hashed.encode())

from app.config import AGENT_BASE_URL, MERCHANT_BASE_URL, TOKEN_TTL_HOURS
from app.db import get_conn, init_db
from app import wallet as wallet_ops

router = APIRouter()


# ── Pydantic models ────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: str
    password: str
    pin: str


class LoginRequest(BaseModel):
    email: str
    password: str


class VerifyPinRequest(BaseModel):
    token: str
    pin: str
    adk_session_id: str
    interrupt_id: str = "payment_auth"


class TopupRequest(BaseModel):
    token: str
    amount_cents: int


class CreateAdkSessionRequest(BaseModel):
    token: str
    initial_state: dict | None = None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_user_from_token(token: str) -> dict:
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT u.* FROM users u
               JOIN auth_sessions s ON s.user_id = u.id
               WHERE s.token=? AND s.expires_at > datetime('now')""",
            (token,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        return dict(row)
    finally:
        conn.close()


def _generate_user_keypair(user_id: str) -> tuple[str, str]:
    """Generate EC P-256 key pair, return (private_jwk_json, public_jwk_json)."""
    raw = ec.generate_private_key(ec.SECP256R1())
    jwk = JWK.from_pyca(raw)
    jwk_dict = json.loads(jwk.export())
    jwk_dict["kid"] = user_id
    jwk_dict["alg"] = "ES256"
    private_jwk = json.dumps(jwk_dict)
    public_jwk = json.dumps(json.loads(JWK.from_json(private_jwk).export_public()))
    return private_jwk, public_jwk


# ── Register ───────────────────────────────────────────────────────────────────

@router.post("/auth/register")
async def register(req: RegisterRequest) -> dict:
    if len(req.pin) < 4 or not req.pin.isdigit():
        raise HTTPException(status_code=400, detail="PIN must be 4-6 digits")

    conn = get_conn()
    try:
        existing = conn.execute("SELECT id FROM users WHERE email=?", (req.email,)).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="Email already registered")

        user_id = uuid.uuid4().hex[:16]
        password_hash = _hash_secret(req.password)
        pin_hash = _hash_secret(req.pin)

        private_jwk, public_jwk = _generate_user_keypair(user_id)

        conn.execute(
            "INSERT INTO users (id, email, password_hash, pin_hash) VALUES (?,?,?,?)",
            (user_id, req.email, password_hash, pin_hash),
        )
        conn.execute(
            "INSERT INTO user_keys (user_id, private_jwk, public_jwk) VALUES (?,?,?)",
            (user_id, private_jwk, public_jwk),
        )
        wallet_id = uuid.uuid4().hex[:16]
        conn.execute(
            "INSERT INTO wallets (id, user_id) VALUES (?,?)",
            (wallet_id, user_id),
        )
        token = secrets.token_hex(32)
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=TOKEN_TTL_HOURS)).isoformat()
        conn.execute(
            "INSERT INTO auth_sessions (token, user_id, expires_at) VALUES (?,?,?)",
            (token, user_id, expires_at),
        )
        conn.commit()
    finally:
        conn.close()

    return {"user_id": user_id, "token": token, "email": req.email, "balance_cents": 0}


# ── Login ──────────────────────────────────────────────────────────────────────

@router.post("/auth/login")
async def login(req: LoginRequest) -> dict:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, email, password_hash FROM users WHERE email=?", (req.email,)
        ).fetchone()
        if not row or not _verify_secret(req.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid credentials")

        user_id = row["id"]
        balance = await wallet_ops.get_balance(user_id)

        token = secrets.token_hex(32)
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=TOKEN_TTL_HOURS)).isoformat()
        conn.execute(
            "INSERT INTO auth_sessions (token, user_id, expires_at) VALUES (?,?,?)",
            (token, user_id, expires_at),
        )
        conn.commit()
    finally:
        conn.close()

    return {"user_id": user_id, "token": token, "email": req.email, "balance_cents": balance}


# ── Verify PIN ─────────────────────────────────────────────────────────────────

@router.post("/auth/verify-pin")
async def verify_pin(req: VerifyPinRequest) -> dict:
    user = _get_user_from_token(req.token)
    user_id = user["id"]

    if not _verify_secret(req.pin, user["pin_hash"]):
        return {"ok": False, "error": "Incorrect PIN"}

    # Verify the adk_session_id belongs to this user
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT user_id FROM adk_sessions WHERE adk_session_id=?", (req.adk_session_id,)
        ).fetchone()
        if not row or row["user_id"] != user_id:
            return {"ok": False, "error": "Session not found"}
    finally:
        conn.close()

    return {"ok": True}


# ── ADK Session creation ───────────────────────────────────────────────────────

@router.post("/adk-sessions")
async def create_adk_session(req: CreateAdkSessionRequest) -> dict:
    user = _get_user_from_token(req.token)
    user_id = user["id"]

    # Fetch user's public key to embed in ADK session state
    conn = get_conn()
    try:
        key_row = conn.execute(
            "SELECT public_jwk FROM user_keys WHERE user_id=?", (user_id,)
        ).fetchone()
    finally:
        conn.close()

    user_public_jwk = json.loads(key_row["public_jwk"]) if key_row else {}

    adk_session_id = uuid.uuid4().hex

    # Create ADK session via ADK's own endpoint with initial state carrying user_id
    initial_state = {
        "user_id": user_id,
        "user_public_jwk": user_public_jwk,
        **(req.initial_state or {}),
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{AGENT_BASE_URL}/apps/app/users/{user_id}/sessions",
            json={"session_id": adk_session_id, "state": initial_state},
        )
        if resp.status_code not in (200, 201):
            raise HTTPException(status_code=502, detail=f"ADK session creation failed: {resp.text}")

    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO adk_sessions (adk_session_id, user_id) VALUES (?,?)",
            (adk_session_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()

    return {"adk_session_id": adk_session_id, "user_id": user_id}


# ── Wallet endpoints ───────────────────────────────────────────────────────────

@router.get("/wallet/balance")
async def get_wallet_balance(token: str) -> dict:
    user = _get_user_from_token(token)
    balance = await wallet_ops.get_balance(user["id"])
    transactions = wallet_ops.get_transactions(user["id"])
    return {
        "balance_cents": balance,
        "transactions": [dict(t) for t in transactions],
    }


@router.post("/wallet/topup")
async def topup_wallet(req: TopupRequest) -> dict:
    user = _get_user_from_token(req.token)
    if req.amount_cents <= 0 or req.amount_cents > 1_000_000:
        raise HTTPException(status_code=400, detail="Amount must be between 1¢ and $10,000")
    new_balance = await wallet_ops.deposit(user["id"], req.amount_cents, reason="topup")
    return {"balance_cents": new_balance}


@router.get("/wallet/verify-chain")
async def verify_wallet_chain(token: str) -> dict:
    """Walk the ledger hash chain for this user and verify integrity.

    Returns {"valid": true, "entries": N} if nothing was tampered with.
    Returns {"valid": false, "error": "..."} if any row was modified.

    Try it:  curl "http://localhost:8000/wallet/verify-chain?token=<your-token>"
    """
    user = _get_user_from_token(token)
    return wallet_ops.verify_chain(user["id"])
