#!/bin/bash
# Terminal 2: Agent Server
set -a; [ -f .env ] && source .env; set +a
HOST=${AGENT_HOST:-0.0.0.0}
PORT=${AGENT_PORT:-8000}
cd "$(dirname "$0")"
exec uv run uvicorn app.fast_api_app:app --host "$HOST" --port "$PORT" --reload
