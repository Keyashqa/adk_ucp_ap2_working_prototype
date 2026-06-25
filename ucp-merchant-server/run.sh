#!/bin/bash
# Terminal 1: Merchant Server
set -a; [ -f .env ] && source .env; set +a
HOST=${MERCHANT_HOST:-0.0.0.0}
PORT=${MERCHANT_PORT:-8001}
cd "$(dirname "$0")"
exec uv run uvicorn app.main:app --host "$HOST" --port "$PORT" --reload
