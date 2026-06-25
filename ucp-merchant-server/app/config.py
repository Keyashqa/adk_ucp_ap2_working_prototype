"""Centralised configuration — reads from .env, then environment, then defaults."""
from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
import os

_ROOT = Path(__file__).parent.parent
load_dotenv(_ROOT / ".env", override=False)


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


# ── Merchant server ─────────────────────────────────────────
MERCHANT_HOST: str = _get("MERCHANT_HOST", "0.0.0.0")
MERCHANT_PORT: int = int(_get("MERCHANT_PORT", "8001"))

# Public base URL embedded in UCP discovery response
MERCHANT_BASE_URL: str = _get("MERCHANT_BASE_URL", "http://localhost:8001").rstrip("/")

# ── SQLite ──────────────────────────────────────────────────
_db_path_raw = _get("MERCHANT_DB_PATH")
DB_PATH: Path = Path(_db_path_raw) if _db_path_raw else _ROOT / "merchant.db"
