#!/usr/bin/env bash
set -e

# ==============================================================================
# Agent API Installer Script (Idempotent)
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "=== Agent API Installer ==="
echo "Repo root: ${REPO_DIR}"

# 1. Dependency Checks (no silent sudo)
MISSING_PKGS=()

if ! command -v bwrap &>/dev/null; then
    MISSING_PKGS+=("bubblewrap")
fi

if ! python3 -m venv --help &>/dev/null; then
    MISSING_PKGS+=("python3-venv")
fi

if ! command -v sqlite3 &>/dev/null; then
    MISSING_PKGS+=("sqlite3")
fi

if [ ${#MISSING_PKGS[@]} -ne 0 ]; then
    echo ""
    echo "WARNING: Missing required system dependencies: ${MISSING_PKGS[*]}"
    echo "Please install them using your system package manager before proceeding:"
    echo "  sudo apt-get update && sudo apt-get install -y ${MISSING_PKGS[*]}"
    echo ""
    if command -v bwrap &>/dev/null; then
        echo "Continuing setup for existing dependencies..."
    else
        echo "Error: bubblewrap is required for fail-closed sandbox execution."
        exit 1
    fi
else
    echo "[OK] System dependencies present (bubblewrap, python3-venv, sqlite3)."
fi

# 2. Virtual Environment Setup
VENV_DIR="${REPO_DIR}/.venv"
if [ ! -d "${VENV_DIR}" ]; then
    echo "Creating Python virtual environment in ${VENV_DIR}..."
    python3 -m venv "${VENV_DIR}"
fi

echo "Installing / updating Python dependencies inside .venv..."
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -r "${REPO_DIR}/requirements.txt"
echo "[OK] Virtual environment ready."

# 3. Environment & Key Configuration File
CONFIG_DIR="${HOME}/.config/agent-api"
ENV_FILE="${CONFIG_DIR}/env"

mkdir -p "${CONFIG_DIR}"

if [ ! -f "${ENV_FILE}" ]; then
    echo "Generating new API_KEY in ${ENV_FILE}..."
    GENERATED_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    cat << EOF > "${ENV_FILE}"
# Agent API Environment Configuration
API_KEY="${GENERATED_KEY}"
HOST="0.0.0.0"
PORT="8090"
TRUSTED_NETWORKS="192.168.87.0/24,100.64.0.0/10"
MAX_CONCURRENCY="3"
JOB_TIMEOUT="120"
LOG_LEVEL="INFO"
EOF
    chmod 0600 "${ENV_FILE}"
    echo "[OK] Generated configuration at ${ENV_FILE} (mode 0600)."
else
    echo "[OK] Configuration file already exists at ${ENV_FILE}."
fi

# 4. Systemd User Service Setup
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
SERVICE_FILE="${SYSTEMD_USER_DIR}/agent-api.service"

mkdir -p "${SYSTEMD_USER_DIR}"

echo "Installing systemd user unit to ${SERVICE_FILE}..."
cat << EOF > "${SERVICE_FILE}"
[Unit]
Description=Agent API Job Queue Service
After=network.target

[Service]
Type=simple
WorkingDirectory=${REPO_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${REPO_DIR}/.venv/bin/uvicorn app.main:app --host \${HOST} --port \${PORT}
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

echo "Reloading systemd user daemon..."
systemctl --user daemon-reload
systemctl --user enable agent-api.service
systemctl --user restart agent-api.service

# Enable linger if loginctl is available
if command -v loginctl &>/dev/null; then
    loginctl enable-linger "$(whoami)" 2>/dev/null || true
    echo "[OK] Systemd linger enabled for user $(whoami)."
fi

# 5. Verification
echo "Verifying service health..."
sleep 2

HEALTH_URL="http://127.0.0.1:8090/healthz"
if command -v curl &>/dev/null; then
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" "${HEALTH_URL}" || echo "000")
    if [ "${STATUS}" = "200" ]; then
        echo "[SUCCESS] agent-api service is running and healthy on http://127.0.0.1:8090"
    else
        echo "WARNING: Health check returned HTTP status ${STATUS}"
    fi
fi
