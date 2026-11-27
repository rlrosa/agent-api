#!/usr/bin/env bash
# dev-server.sh — Manage an isolated side-by-side agent-api server for local testing.
#
# Usage:
#   ./scripts/dev-server.sh start  [port]   # default: 8091 (refuses 8090)
#   ./scripts/dev-server.sh status [port]
#   ./scripts/dev-server.sh logs   [port] [-f]
#   ./scripts/dev-server.sh stop   [port]
#
# Starts uvicorn via an inherited pre-bound loopback socket (127.0.0.1:<port>) with
# isolated SQLite DBs and workspaces under /tmp/agent-api-dev-<port> (mode 0700),
# flock-serialized startup/stop, pidfd + /proc/<pid>/stat start-time identity checks,
# SSH_CONNECTION=1, and BWRAP_ENABLED=1.

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

STATE_ROOT="${DEV_SERVER_STATE_ROOT:-/tmp}"
STATE_DIR="${STATE_ROOT}/agent-api-dev-${PORT}"
LOCK_FILE="${STATE_ROOT}/.agent-api-dev-${PORT}.lock"
PID_FILE="${STATE_DIR}/uvicorn.pid"
LOG_FILE="${STATE_DIR}/uvicorn.log"
TARGET_URL="http://127.0.0.1:${PORT}"

# Acquire a stable per-port lock outside STATE_DIR so start and stop are serialized
# even before STATE_DIR exists and across STATE_DIR removal.
acquire_port_lock() {
  python3 - "$STATE_ROOT" "$LOCK_FILE" <<'PY'
import os, stat, sys

state_root, lock_file = sys.argv[1:3]
uid = os.getuid()

rst = os.lstat(state_root)
if stat.S_ISLNK(rst.st_mode) or not stat.S_ISDIR(rst.st_mode):
    sys.exit(f"error: refusing unsafe state root {state_root} (symlink or non-directory)")
if state_root != "/tmp" and (rst.st_uid != uid or (rst.st_mode & 0o077) != 0):
    sys.exit(f"error: refusing unsafe custom state root {state_root}")

try:
    lst = os.lstat(lock_file)
    if stat.S_ISLNK(lst.st_mode) or not stat.S_ISREG(lst.st_mode):
        sys.exit(f"error: refusing unsafe lock file {lock_file} (symlink or non-regular)")
    if lst.st_uid != uid:
        sys.exit(f"error: refusing lock file {lock_file} owned by uid {lst.st_uid} (expected {uid})")
    if (lst.st_mode & 0o077) != 0:
        sys.exit(f"error: refusing lock file {lock_file} with group/world permissions ({oct(lst.st_mode & 0o777)})")
except FileNotFoundError:
    pass

lfd = os.open(lock_file, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
try:
    st = os.fstat(lfd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != uid:
        sys.exit(f"error: refusing lock file {lock_file} after open")
    os.fchmod(lfd, 0o600)
finally:
    os.close(lfd)
PY
  exec 9<>"$LOCK_FILE"
  flock -x 9
}

# Validate STATE_DIR / PID_FILE / LOG_FILE WITHOUT truncating live PID or log files.
validate_state_dir() {
  local create_mode="${1:-0}"
  python3 - "$STATE_DIR" "$LOG_FILE" "$PID_FILE" "$create_mode" <<'PY'
import errno, os, stat, sys

state_dir, log_file, pid_file, create_mode = sys.argv[1:5]
create_b = create_mode == "1"
uid = os.getuid()

if create_b:
    try:
        os.mkdir(state_dir, 0o700)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise

try:
    st = os.lstat(state_dir)
except FileNotFoundError:
    sys.exit(0)

if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
    sys.exit(f"error: refusing unsafe state path {state_dir} (symlink or non-directory)")
if st.st_uid != uid:
    sys.exit(f"error: refusing state directory {state_dir} owned by uid {st.st_uid} (expected {uid})")
if (st.st_mode & 0o077) != 0:
    sys.exit(f"error: refusing state directory {state_dir} with group/world permissions ({oct(st.st_mode & 0o777)})")

for existing in (pid_file, log_file):
    try:
        fst = os.lstat(existing)
    except FileNotFoundError:
        continue
    if stat.S_ISLNK(fst.st_mode) or not stat.S_ISREG(fst.st_mode):
        sys.exit(f"error: refusing unsafe file {existing} (symlink or non-regular)")
    if fst.st_uid != uid:
        sys.exit(f"error: refusing file {existing} owned by uid {fst.st_uid} (expected {uid})")
    if not create_b and (fst.st_mode & 0o077) != 0:
        sys.exit(f"error: refusing file {existing} with group/world permissions ({oct(fst.st_mode & 0o777)})")

if create_b:
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
        os.chmod(p, 0o700)
PY
}

# Read verified PID (requires valid <pid>:<starttime> matching /proc/<pid>/stat) and confirm process is alive.
read_verified_live_pid() {
  python3 - "$PID_FILE" <<'PY'
import os, stat, sys

def proc_starttime(pid: int) -> str:
    data = open(f"/proc/{pid}/stat", "r", encoding="utf-8").read()
    after_comm = data.rsplit(")", 1)[1].split()
    return after_comm[19]

p = sys.argv[1]
try:
    fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
except OSError:
    sys.exit(1)
try:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or (st.st_mode & 0o077) != 0:
        sys.exit(1)
    raw = os.read(fd, 128).decode("utf-8", errors="replace").strip()
finally:
    os.close(fd)

parts = raw.split(":", 1)
if len(parts) != 2 or not parts[0].isdigit() or not parts[1].strip().isdigit():
    sys.exit(1)
pid = int(parts[0])
expected_start = parts[1].strip()

try:
    os.kill(pid, 0)
    actual_start = proc_starttime(pid)
except OSError:
    sys.exit(1)

if actual_start != expected_start:
    sys.exit(1)

print(pid)
PY
}

is_running() {
  read_verified_live_pid >/dev/null 2>&1
}

# Identity-safe termination using os.pidfd_open + signal.pidfd_send_signal + /proc/<pid>/stat starttime verification.
# Exit codes:
#   0 = verified live process terminated
#   1 = no PID file, process already exited, or mismatched starttime (stale PID)
#   2 = verified process survived SIGKILL
#   3 = pidfd signaling unavailable (refuses unsafe os.kill fallback)
#   4 = malformed / legacy PID-only record lacking starttime
terminate_verified_pid_record() {
  python3 - "$PID_FILE" <<'PY'
import os, signal, stat, sys, time

def proc_starttime(pid: int) -> str:
    data = open(f"/proc/{pid}/stat", "r", encoding="utf-8").read()
    return data.rsplit(")", 1)[1].split()[19]

p = sys.argv[1]
try:
    fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
except FileNotFoundError:
    sys.exit(1)
except OSError:
    sys.exit(4)
try:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or (st.st_mode & 0o077) != 0:
        sys.exit(4)
    raw = os.read(fd, 128).decode("utf-8", errors="replace").strip()
finally:
    os.close(fd)

parts = raw.split(":", 1)
if len(parts) != 2 or not parts[0].isdigit() or not parts[1].strip().isdigit():
    sys.stderr.write(f"error: refusing PID record without valid '<pid>:<starttime>' format in {p}\n")
    sys.exit(4)

if not (hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")):
    sys.stderr.write("error: pidfd signaling (os.pidfd_open / signal.pidfd_send_signal) is unavailable; refusing unsafe PID signal\n")
    sys.exit(3)

pid = int(parts[0])
expected_start = parts[1].strip()

pidfd = None
try:
    pidfd = os.pidfd_open(pid, 0)
    actual_start = proc_starttime(pid)
except (ProcessLookupError, FileNotFoundError):
    if pidfd is not None:
        os.close(pidfd)
    sys.exit(1)
except OSError as e:
    if pidfd is not None:
        os.close(pidfd)
    sys.stderr.write(f"error: failed opening pidfd or reading /proc/{pid}/stat: {e}\n")
    sys.exit(3)

if actual_start != expected_start:
    os.close(pidfd)
    sys.exit(1)

def send_sig(sig: int) -> None:
    signal.pidfd_send_signal(pidfd, sig)

def still_alive() -> bool:
    try:
        send_sig(0)
        return os.path.exists(f"/proc/{pid}/stat") and proc_starttime(pid) == expected_start
    except OSError:
        return False

try:
    send_sig(signal.SIGTERM)
    for _ in range(20):
        if not still_alive():
            print(pid)
            sys.exit(0)
        time.sleep(0.15)
    if still_alive():
        send_sig(signal.SIGKILL)
        for _ in range(10):
            if not still_alive():
                print(pid)
                sys.exit(0)
            time.sleep(0.1)
    if still_alive():
        sys.stderr.write(f"error: dev-server PID {pid} survived SIGKILL\n")
        sys.exit(2)
    print(pid)
finally:
    os.close(pidfd)
PY
}

case "$ACTION" in
  start)
    if [ ! -x "$UVICORN_BIN" ]; then
      echo "error: uvicorn not found at $UVICORN_BIN (create .venv first)" >&2
      exit 1
    fi

    acquire_port_lock
    validate_state_dir 1

    if is_running; then
      echo "dev-server already running on ${TARGET_URL} (PID $(read_verified_live_pid))"
      exit 0
    fi

    DEV_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"

    NEW_PID="$(
      DEV_KEY="$DEV_KEY" python3 - "$UVICORN_BIN" "$PORT" "$STATE_DIR" "$LOG_FILE" "$PID_FILE" "$REPO_ROOT" <<'PY'
import os, socket, subprocess, sys

def proc_starttime(pid: int) -> str:
    data = open(f"/proc/{pid}/stat", "r", encoding="utf-8").read()
    return data.rsplit(")", 1)[1].split()[19]

uvicorn_bin, port_s, state_dir, log_file, pid_file, repo_root = sys.argv[1:7]
port = int(port_s)

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    sock.bind(("127.0.0.1", port))
    sock.listen(128)
except OSError as e:
    sock.close()
    sys.exit(f"error: port 127.0.0.1:{port} is already in use ({e})")

sock.set_inheritable(True)
log_fd = os.open(
    log_file,
    os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
    0o600,
)
os.fchmod(log_fd, 0o600)

child_env = os.environ.copy()
child_env.pop("API_KEYS", None)
child_env.update({
    "SSH_CONNECTION": "1",
    "API_KEY": os.environ["DEV_KEY"],
    "HOST": "127.0.0.1",
    "PORT": str(port),
    "DB_PATH": os.path.join(state_dir, "jobs.db"),
    "LOG_EVENTS_DB": os.path.join(state_dir, "events.db"),
    "WORK_ROOT": os.path.join(state_dir, "jobs"),
    "SESSION_WORK_ROOT": os.path.join(state_dir, "sessions"),
    "BWRAP_ENABLED": "1",
    "ALLOW_UNCONFINED": "0",
})

try:
    proc = subprocess.Popen(
        [uvicorn_bin, "app.main:app", "--fd", str(sock.fileno())],
        cwd=repo_root,
        env=child_env,
        stdin=subprocess.DEVNULL,
        stdout=log_fd,
        stderr=log_fd,
        pass_fds=(sock.fileno(),),
        start_new_session=True,
    )
finally:
    os.close(log_fd)
    sock.close()

try:
    start_ticks = proc_starttime(proc.pid)
    try:
        os.unlink(pid_file)
    except FileNotFoundError:
        pass
    pid_fd = os.open(
        pid_file,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(pid_fd, 0o600)
        os.write(pid_fd, f"{proc.pid}:{start_ticks}\n".encode("ascii"))
    finally:
        os.close(pid_fd)
except BaseException as pub_err:
    try:
        proc.terminate()
        proc.wait(timeout=3.0)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=2.0)
        except Exception:
            pass
    sys.exit(f"error: failed publishing PID file {pid_file}: {pub_err}")

print(proc.pid)
PY
    )"

    ready=0
    deadline=$((SECONDS + 10))
    while [ "$SECONDS" -lt "$deadline" ]; do
      if ! is_running; then
        break
      fi
      if curl -sf --max-time 1 -H "X-API-Key: ${DEV_KEY}" "${TARGET_URL}/v1/me" >/dev/null 2>&1; then
        if is_running; then
          ready=1
          break
        fi
      fi
      sleep 0.2
    done

    if [ "$ready" -ne 1 ]; then
      echo "error: dev-server failed to start on ${TARGET_URL}. Recent logs:" >&2
      tail -n 30 "$LOG_FILE" >&2 || true
      if terminate_verified_pid_record >/dev/null 2>&1 || ! is_running; then
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
    validate_state_dir 0
    if is_running; then
      echo "RUNNING on ${TARGET_URL} (PID $(read_verified_live_pid), dir ${STATE_DIR})"
      curl -s --max-time 2 "${TARGET_URL}/healthz" && echo
    else
      echo "STOPPED (no active dev-server for port ${PORT})"
      exit 1
    fi
    ;;

  logs)
    validate_state_dir 0
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
    validate_state_dir 0
    acquire_port_lock
    validate_state_dir 0

    if [ -x "$SESSION_CLI" ] && [ -f "$HOME/.cache/agent-api/session-cli.json" ]; then
      current_url="$(python3 -c 'import json, os; p=os.path.expanduser("~/.cache/agent-api/session-cli.json"); print(json.load(open(p)).get("url",""))' 2>/dev/null || true)"
      if [ "$current_url" = "$TARGET_URL" ]; then
        "$SESSION_CLI" --close >/dev/null 2>&1 || true
        "$SESSION_CLI" --url "http://127.0.0.1:8090" --key "" --save-config >/dev/null 2>&1 || true
      fi
    fi

    stopped_pid=""
    term_rc=0
    stopped_pid="$(terminate_verified_pid_record)" || term_rc=$?
    if [ "$term_rc" -eq 0 ]; then
      echo "Stopped dev-server PID ${stopped_pid} on ${TARGET_URL}"
    elif [ "$term_rc" -eq 1 ]; then
      echo "No running verified dev-server PID for port ${PORT}"
    else
      exit 1
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
