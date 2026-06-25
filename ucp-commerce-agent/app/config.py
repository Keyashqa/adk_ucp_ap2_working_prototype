"""Centralised configuration — reads from .env, then environment, then defaults."""
from __future__ import annotations

from pathlib import Path
import os

from dotenv import load_dotenv

# Load .env from project root; real env vars (CI, Docker) always win
_ROOT = Path(__file__).parent.parent
load_dotenv(_ROOT / ".env", override=False)


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


# ── Agent server ───────────────────────────────────────────
AGENT_HOST: str = _get("AGENT_HOST", "0.0.0.0")
AGENT_PORT: int = int(_get("AGENT_PORT", "8000"))

# Derived: used by auth.py to create ADK sessions on itself
AGENT_BASE_URL: str = f"http://localhost:{AGENT_PORT}"

# ── Merchant service ────────────────────────────────────────
MERCHANT_BASE_URL: str = _get("MERCHANT_BASE_URL", "http://localhost:8001").rstrip("/")

# ── SQLite ──────────────────────────────────────────────────
_db_path_raw = _get("AGENT_DB_PATH")
DB_PATH: Path = Path(_db_path_raw) if _db_path_raw else _ROOT / "agent.db"

# ── Auth ────────────────────────────────────────────────────
TOKEN_TTL_HOURS: int = int(_get("TOKEN_TTL_HOURS", "72"))

# ── Gemini API ──────────────────────────────────────────────
GEMINI_API_KEY: str = _get("GEMINI_API_KEY", "")
MODEL_NAME: str = _get("MODEL_NAME", "gemini-2.5-flash-lite")
