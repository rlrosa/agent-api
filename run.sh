#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt
fi

load_env_file() {
    local env_file="$1"
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        if [ -z "$line" ] || [[ "$line" == \#* ]]; then
            continue
        fi
        if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
            local key="${BASH_REMATCH[1]}"
            local val="${BASH_REMATCH[2]}"
            if [[ "$val" =~ ^\"(.*)\"$ ]]; then
                val="${BASH_REMATCH[1]}"
            elif [[ "$val" =~ ^\'(.*)\'$ ]]; then
                val="${BASH_REMATCH[1]}"
            fi
            if [ -z "${!key+x}" ]; then
                export "$key=$val"
            fi
        fi
    done < "$env_file"
}

if [ -z "${API_KEY:-}" ] && [ -z "${API_KEYS:-}" ]; then
    if [ -f "$HOME/.config/agent-api/env" ]; then
        load_env_file "$HOME/.config/agent-api/env"
    elif [ -f "$DIR/.env" ]; then
        load_env_file "$DIR/.env"
    fi
fi

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8090}"
LOG_LEVEL_LOWER=$(echo "${LOG_LEVEL:-info}" | tr '[:upper:]' '[:lower:]')

exec .venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT" --log-level "$LOG_LEVEL_LOWER"

