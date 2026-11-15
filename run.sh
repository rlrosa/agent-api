#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt
fi

if [ -z "${API_KEY:-}" ] && [ -z "${API_KEYS:-}" ]; then
    if [ -f "$HOME/.config/agent-api/env" ]; then
        set -a
        # shellcheck disable=SC1090
        source "$HOME/.config/agent-api/env"
        set +a
    elif [ -f "$DIR/.env" ]; then
        set -a
        # shellcheck disable=SC1091
        source "$DIR/.env"
        set +a
    fi
fi

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8090}"
LOG_LEVEL_LOWER=$(echo "${LOG_LEVEL:-info}" | tr '[:upper:]' '[:lower:]')

exec .venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT" --log-level "$LOG_LEVEL_LOWER"

