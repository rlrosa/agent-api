#!/usr/bin/env bash
# dev-server.sh — Manage an isolated side-by-side agent-api server for local testing.
#
# Usage:
#   ./scripts/dev-server.sh start  [port]   # default: 8091 (refuses 8090)
#   ./scripts/dev-server.sh status [port]
#   ./scripts/dev-server.sh logs   [port] [-f]
#   ./scripts/dev-server.sh stop   [port]
#
# Starts uvicorn bound strictly to 127.0.0.1:<port> with isolated SQLite DBs and
# workspaces under /tmp/agent-api-dev-<port> (mode 0700), SSH_CONNECTION=1, and
# BWRAP_ENABLED=1, and configures ~/.cache/agent-api/session-cli.json (mode 0600)
# so ./scripts/session-cli talks to it out of the box.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UVICORN_BIN="${REPO_ROOT}/.venv/bin/uvicorn"
SESSION_CLI="${REPO_ROOT}/scripts/session-cli"

ACTION="${1:-}"
PORT="${2:-8091}"
EXTRA_ARG="${3:-}"

usage() {
  cat <<'EOF' >&2
Usage: ./scripts/dev-server.sh <start|stop|status|logs> [port]

Commands:
  start  [port]       Start isolated loopback server (default port: 8091; 8090 is refused)
  status [port]       Show PID and /healthz status for the dev server
  logs   [port] [-f]  Show recent (or follow) dev server logs
  stop   [port]       Close active sessions, stop uvicorn, wipe temp dir, and reset session-cli
EOF
  exit "${1:-1}"
}

validate_port() {
  local p="$1"
  if ! [[ "$p" =~ ^[0-9]+$ ]] || [ "$p" -lt 1024 ] || [ "$p" -gt 65535 ]; then
    echo "error: invalid port '$p' (must be 1024..65535)" >&2
    exit 2
  fi
  if [ "$p" = "8090" ]; then
    echo "error: refusing port 8090 (reserved for main deployed agent-api.service)" >&2
    exit 2
  fi
}

[ -z "$ACTION" ] && usage 1
case "$ACTION" in
  -h|--help|help) usage 0 ;;
esac

validate_port "$PORT"

STATE_DIR="/tmp/agent-api-dev-${PORT}"
PID_FILE="${STATE_DIR}/uvicorn.pid"
LOG_FILE="${STATE_DIR}/uvicorn.log"
TARGET_URL="http://127.0.0.1:${PORT}"

is_running() {
  [ ! -L "$PID_FILE" ] && [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

terminate_pid() {
  local pid="$1"
  kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do
    if ! kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
    sleep 0.15
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null || true
    for _ in $(seq 1 10); do
      if ! kill -0 "$pid" 2>/dev/null; then
        return 0
      fi
      sleep 0.1
    done
  fi
  ! kill -0 "$pid" 2>/dev/null
}

case "$ACTION" in
  start)
    if [ ! -x "$UVICORN_BIN" ]; then
      echo "error: uvicorn not found at $UVICORN_BIN (create .venv first)" >&2
      exit 1
    fi
    if is_running; then
      echo "dev-server already running on ${TARGET_URL} (PID $(cat "$PID_FILE"))"
      exit 0
    fi

    # Reject if another process already owns 127.0.0.1:<PORT>
    if ! python3 - "$PORT" <<'PY'
import socket, sys
port = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("127.0.0.1", port))
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
    then
      echo "error: port 127.0.0.1:${PORT} is already in use by another process" >&2
      exit 1
    fi

    # Securely create/validate STATE_DIR (reject symlinks, foreign ownership, or unsafe permissions)
    python3 - "$STATE_DIR" "$LOG_FILE" "$PID_FILE" <<'PY'
import errno, os, stat, sys

state_dir, log_file, pid_file = sys.argv[1], sys.argv[2], sys.argv[3]
uid = os.getuid()

try:
    os.mkdir(state_dir, 0o700)
except OSError as e:
    if e.errno != errno.EEXIST:
        raise

st = os.lstat(state_dir)
if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
    sys.exit(f"error: refusing unsafe state path {state_dir} (symlink or non-directory)")
if st.st_uid != uid:
    sys.exit(f"error: refusing state directory {state_dir} owned by uid {st.st_uid} (expected {uid})")
if (st.st_mode & 0o077) != 0:
    sys.exit(f"error: refusing state directory {state_dir} with group/world permissions ({oct(st.st_mode & 0o777)})")

os.chmod(state_dir, 0o700)
for sub in ("jobs", "sessions"):
    p = os.path.join(state_dir, sub)
    try:
        os.mkdir(p, 0o700)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise
    sub_st = os.lstat(p)
    if stat.S_ISLNK(sub_st.st_mode) or not stat.S_ISDIR(sub_st.st_mode) or sub_st.st_uid != uid:
        sys.exit(f"error: refusing unsafe subdirectory {p}")

for fpath in (log_file, pid_file):
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(fpath, flags, 0o600)
    os.close(fd)
PY

    DEV_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"

    umask 077
    env \
      SSH_CONNECTION=1 \
      API_KEY="$DEV_KEY" \
      HOST=127.0.0.1 \
      PORT="$PORT" \
      DB_PATH="${STATE_DIR}/jobs.db" \
      LOG_EVENTS_DB="${STATE_DIR}/events.db" \
      WORK_ROOT="${STATE_DIR}/jobs" \
      SESSION_WORK_ROOT="${STATE_DIR}/sessions" \
      BWRAP_ENABLED=1 \
      ALLOW_UNCONFINED=0 \
      setsid "$UVICORN_BIN" app.main:app --host 127.0.0.1 --port "$PORT" \
      </dev/null >>"$LOG_FILE" 2>&1 &
    NEW_PID=$!
    echo "$NEW_PID" > "$PID_FILE"

    ready=0
    for _ in $(seq 1 35); do
      if ! kill -0 "$NEW_PID" 2>/dev/null; then
        break
      fi
      if curl -sf "${TARGET_URL}/healthz" >/dev/null 2>&1; then
        sleep 0.05
        if kill -0 "$NEW_PID" 2>/dev/null; then
          ready=1
          break
        fi
      fi
      sleep 0.2
    done

    if [ "$ready" -ne 1 ]; then
      echo "error: dev-server failed to start on ${TARGET_URL}. Recent logs:" >&2
      tail -n 30 "$LOG_FILE" >&2 || true
      if terminate_pid "$NEW_PID"; then
        rm -f "$PID_FILE"
      else
        echo "warning: uvicorn PID ${NEW_PID} still alive; keeping ${PID_FILE}" >&2
      fi
      exit 1
    fi

    if [ -x "$SESSION_CLI" ]; then
      "$SESSION_CLI" --url "$TARGET_URL" --key "$DEV_KEY" --save-config >/dev/null 2>&1 || true
      python3 -c '
import json, os
p = os.path.expanduser("~/.cache/agent-api/session-cli.json")
if os.path.exists(p):
    d = json.load(open(p))
    d["session_id"] = None
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=2)
        f.write("\n")
'
    fi

    echo "Started isolated dev-server on ${TARGET_URL} (PID ${NEW_PID}, dir ${STATE_DIR})"
    echo "Ready: run ./scripts/session-cli to chat, or ./scripts/dev-server.sh stop ${PORT} when done."
    ;;

  status)
    if is_running; then
      echo "RUNNING on ${TARGET_URL} (PID $(cat "$PID_FILE"), dir ${STATE_DIR})"
      curl -s "${TARGET_URL}/healthz" && echo
    else
      echo "STOPPED (no active dev-server for port ${PORT})"
      exit 1
    fi
    ;;

  logs)
    if [ ! -f "$LOG_FILE" ] || [ -L "$LOG_FILE" ]; then
      echo "No valid log file at ${LOG_FILE}" >&2
      exit 1
    fi
    if [ "$EXTRA_ARG" = "-f" ] || [ "$EXTRA_ARG" = "--follow" ]; then
      exec tail -f "$LOG_FILE"
    else
      tail -n 50 "$LOG_FILE"
    fi
    ;;

  stop)
    if [ -x "$SESSION_CLI" ] && [ -f "$HOME/.cache/agent-api/session-cli.json" ]; then
      current_url="$(python3 -c 'import json, os; p=os.path.expanduser("~/.cache/agent-api/session-cli.json"); print(json.load(open(p)).get("url",""))' 2>/dev/null || true)"
      if [ "$current_url" = "$TARGET_URL" ]; then
        "$SESSION_CLI" --close >/dev/null 2>&1 || true
        "$SESSION_CLI" --url "http://127.0.0.1:8090" --key "" --save-config >/dev/null 2>&1 || true
      fi
    fi

    if is_running; then
      pid="$(cat "$PID_FILE")"
      terminate_pid "$pid" || true
      echo "Stopped dev-server PID ${pid} on ${TARGET_URL}"
    else
      echo "No running dev-server PID for port ${PORT}"
    fi

    if [ ! -L "$STATE_DIR" ] && [ -d "$STATE_DIR" ] && [ -O "$STATE_DIR" ]; then
      rm -rf "$STATE_DIR"
      echo "Cleaned up ${STATE_DIR}"
    fi
    ;;

  *)
    usage 1
    ;;
esac
