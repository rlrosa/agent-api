# Agent API Service

A lightweight, secure FastAPI HTTP service that exposes CLI-based AI agents (`agy` and `claude`) as non-interactive backend jobs for document and image processing tasks.

## Security & Sandbox Confinement
- **Fail-Closed Sandbox**: Jobs execute inside isolated `bubblewrap` (`bwrap`) mounts. If confinement is missing or unexecutable, jobs fail closed by default (`ALLOW_UNCONFINED=0`).
- **SSRF Redirect Guard**: Every HTTP redirect hop is validated against restricted loopback, private, and metadata IP ranges.
- **Named API Keys**: Supports multiple named keys (`API_KEYS=name1:key1,name2:key2`) with constant-time verification (`secrets.compare_digest`). Key names are logged; secret values are never logged.
- **Sliding-Window Rate Limit & Queue Cap**: Throttles per-caller requests (`RATE_LIMIT_PER_MIN=60`) and caps pending jobs (`MAX_PENDING_JOBS=100`).
- **Observability Dashboard**: Single-page monitoring dashboard at `http://192.168.87.132:8090/dashboard` or `https://agent-api.rela.uy/dashboard`.

### Detailed Documentation Links
- [Security Architecture](doc/security.md) — Threat model, fail-closed sandbox, SSRF protections, and residual risks.
- [Operations & Monitoring](doc/operations.md) — Service management, logs, and auth-failure watchdog timer.
- [Networking & IP Whitelisting](doc/networking.md) — Network paths and step-by-step IP whitelisting guide.

---

## Getting Started from Scratch

### Automated Installation (Recommended)
Run the idempotent installer script to verify dependencies, create `.venv`, generate an initial API key at `~/.config/agent-api/env` (mode `0600`), and set up the systemd user service:

```bash
./scripts/install.sh
```

### Manual Step-by-Step Setup

#### 1. System Prerequisites
Ensure system packages are installed. Note that Debian/Ubuntu systems do not provide a global system `pip`; use `python3-venv` to create a virtual environment and use `.venv/bin/pip`.

```bash
# Install system packages (Ubuntu/Debian)
sudo apt-get update && sudo apt-get install -y bubblewrap python3-venv sqlite3
```

#### 2. Virtual Environment & Dependencies
```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

#### 3. Configuration & Security Key Setup
Create `~/.config/agent-api/env` with mode `0600`:

```bash
mkdir -p ~/.config/agent-api
cat << 'EOF' > ~/.config/agent-api/env
API_KEY="YOUR_GENERATED_SECURE_API_KEY"
HOST="0.0.0.0"
PORT="8090"
TRUSTED_NETWORKS="127.0.0.1/32,::1/128,127.0.0.0/8"
MAX_CONCURRENCY="3"
JOB_TIMEOUT="120"
LOG_LEVEL="INFO"
EOF
chmod 0600 ~/.config/agent-api/env
```

#### 4. Persistent Service Management (`systemd --user` & Linger)
Install the systemd user unit and enable user process lingering so `agent-api` survives logouts and system reboots:

```bash
mkdir -p ~/.config/systemd/user
cat << 'EOF' > ~/.config/systemd/user/agent-api.service
[Unit]
Description=Agent API Job Queue Service
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/ubuntu/agent-api
EnvironmentFile=%h/.config/agent-api/env
ExecStart=/home/ubuntu/agent-api/.venv/bin/uvicorn app.main:app --host ${HOST} --port ${PORT}
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

# Enable linger for current user
loginctl enable-linger $(whoami)

# Reload daemon and start service
systemctl --user daemon-reload
systemctl --user enable agent-api.service
systemctl --user restart agent-api.service
```

#### 5. Verify Installation
```bash
curl -s http://127.0.0.1:8090/healthz | jq .
```

Expected output:
```json
{
  "status": "ok",
  "version": "0.1.0",
  "confinement": {
    "enabled": true,
    "available": true,
    "mode": "enforced"
  }
}
```

---

## Quick Reference & Commands


### 3. Setting Up Authentication on Client Machines

To authenticate API requests from a remote client machine or local terminal, set the `AGENT_API_KEY` environment variable:

1. **On the Server Host**: Inspect your secret key:
   ```bash
   cat ~/.config/agent-api/env
   # Or load directly into shell on the server host:
   # export AGENT_API_KEY=$(grep -oP 'API_KEY=\K.*' ~/.config/agent-api/env | tr -d '"')
   ```

2. **On the Client Machine**: Export your key into your client shell:
   ```bash
   export AGENT_API_KEY="your-copied-api-key"
   ```

> [!IMPORTANT]
> Do **not** run `grep ~/.config/agent-api/env` on a remote client machine, as that file exists only on the server host. Running inline subshells on a client expands to empty (`-H "X-API-Key: "`), which strips the header and causes HTTP 401 Unauthorized errors.

### 4. Monitoring & Logs

The service is monitored by the Server Watchdog (`~/agent`) which tracks service status (`user:agent-api.service`) and verifies `http://localhost:8090/healthz`.

- **Check Service Status**:
  ```bash
  systemctl --user status agent-api
  ```
- **Follow Live Streamed Logs**:
  ```bash
  journalctl --user -u agent-api -f
  ```
- **View Recent 100 Log Lines**:
  ```bash
  journalctl --user -u agent-api -n 100
  ```
- **View Logs Since 10 Minutes Ago**:
  ```bash
  journalctl --user -u agent-api --since "10 min ago"
  ```


---

## Configuration Reference

The server is configured via environment variables.

| Environment Variable | Default | Description |
| :--- | :--- | :--- |
| `API_KEY` | *(Required)* | Secret key required in the `X-API-Key` HTTP header. |
| `HOST` | `0.0.0.0` | Server bind IP address. |
| `PORT` | `8090` | Server HTTP port. |
| `TRUSTED_NETWORKS` | `127.0.0.1/32,::1/128,127.0.0.0/8` | Trusted CIDR networks for API key auth bypass (Loopback only). Note: adding subnets here grants them full auth bypass. |
| `DB_PATH` | `./data/jobs.db` | SQLite database filepath. |
| `WORK_ROOT` | `/var/tmp/agent-api/jobs` | Directory for per-job isolated workspaces. |
| `BWRAP_ENABLED` | `1` | Enable Bubblewrap OS sandboxing for `agy` and `claude` (1=true, 0=false). |
| `JOB_TIMEOUT` | `120` | Max job execution timeout in seconds. |
| `MAX_CONCURRENT_JOBS` | `3` | Maximum worker concurrency for background jobs. |
| `PURGE_AFTER_HOURS` | `24` | Automated background purge window for old job records. |

---

## Endpoint Documentation & Usage Examples

### 1. Server Health Check
`GET /healthz` (No Authentication Required)

```bash
curl -s http://127.0.0.1:8090/healthz | jq .
```

### 2. Submit Text Job (Wait for Completion)
`POST /v1/jobs`

```bash
curl -s -X POST https://agent-api.rela.uy/v1/jobs \
  -H "X-API-Key: $AGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "agent": "agy",
    "prompt": "Reply with exactly: pong",
    "wait": 60
  }'
```

To extract **only the text answer** clean of JSON metadata, pipe to `jq -r .stdout`:

```bash
curl -s -X POST https://agent-api.rela.uy/v1/jobs \
  -H "X-API-Key: $AGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "agent": "agy",
    "prompt": "Reply with exactly: pong",
    "wait": 60
  }' | jq -r .stdout
```

> [!TIP]
> Do **not** use `curl -i` when piping output to `jq`, as HTTP status headers will break JSON parsing. To inspect HTTP response headers alongside a `jq`-parsed body, dump headers to stderr using `curl -s -D /dev/stderr ... | jq .`.

### 3. Attachment Form A: URL Download
`POST /v1/jobs`

```bash
curl -s -X POST https://agent-api.rela.uy/v1/jobs \
  -H "X-API-Key: $AGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "agent": "claude",
    "prompt": "Summarize page 1 of this PDF",
    "attachments": [
      {
        "filename": "paper.pdf",
        "url": "https://arxiv.org/pdf/1706.03762.pdf"
      }
    ],
    "wait": 60
  }' | jq -r .stdout
```

### 4. Attachment Form B: Base64 Payload
`POST /v1/jobs`

```bash
curl -s -X POST https://agent-api.rela.uy/v1/jobs \
  -H "X-API-Key: $AGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "agent": "agy",
    "prompt": "What text is written on this image?",
    "attachments": [
      {
        "filename": "code.png",
        "content_b64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
      }
    ],
    "wait": 60
  }' | jq -r .stdout
```

### 5. Attachment Form C: Multipart File Upload
`POST /v1/jobs`

```bash
curl -s -X POST https://agent-api.rela.uy/v1/jobs \
  -H "X-API-Key: $AGENT_API_KEY" \
  -F "agent=agy" \
  -F "prompt=What is written on this document?" \
  -F "wait=60" \
  -F "files=@/tmp/m0/doc.pdf" | jq -r .stdout
```

### 6. Trusted Network Auth Bypass (LAN / Tailscale)
`POST /v1/jobs` (No API Key Required for Local Network Callers)

Callers connecting from trusted peer socket IPs (`192.168.87.0/24` or Tailscale `100.64.0.0/10`) skip the `X-API-Key` header requirement:

```bash
curl -s -X POST http://192.168.87.132:8090/v1/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "agent": "agy",
    "prompt": "Reply with exactly: PONG_LAN_BYPASS",
    "wait": 60
  }' | jq -r .stdout
```


---

## Security Posture & Boundary Matrix

### What IS Confined and Hardened
- **Host Filesystem Read & Write Confinement**:
  - `agy` & `claude`: Executed inside unprivileged Bubblewrap (`bwrap`) OS container namespaces. The container uses explicit minimal system mounts (`/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/etc/resolv.conf`, `/etc/ssl`, `/etc/ca-certificates`, `/etc/hosts`), mounting `--tmpfs /home/ubuntu`. Sensitive host files (`~/.ssh/`, `~/.bashrc`, `/etc/passwd`, `/etc/shadow`, `~/.claude/.credentials.json`) do not exist inside the namespace.
  - Workspace attachments (`<work_root>/<job_id>/attachments/`) are explicitly bind-mounted read-write into the job container.
- **Strict Caller Request Lock**: Caller requests are validated with Pydantic `extra="forbid"`. Any payload containing `sandbox`, `tools`, `permissions`, `dangerously_skip_permissions`, or CLI flags returns **HTTP 422 Unprocessable Entity**.
- **Trusted Network Auth Bypass & Tunnel Protection**:
  - Requests from raw peer socket IPs matching `TRUSTED_NETWORKS` (`127.0.0.1/32`, `::1/128`, `127.0.0.0/8`) bypass API key authentication. Adding subnets to `TRUSTED_NETWORKS` grants those networks full auth bypass.
  - Any request carrying Cloudflare Tunnel headers (`CF-Connecting-IP`, `CF-Ray`, `CF-Visitor`) **always requires the API key**, preventing public internet bypass over local tunnel proxies even on loopback connections.
- **Zero Bypass Flags**: `--dangerously-skip-permissions` is completely removed from all execution code paths.
- **Per-Job Attachment Isolation**: Attachments are stored in isolated per-job directories (`<work_root>/<job_id>/attachments/`) and passed via explicit absolute paths, preventing cross-job contamination.
- **Environment Scrubbing**: Sensitive environment variables (`API_KEY`, secret tokens, credentials) are stripped before subprocess spawning; only essential variables (`HOME`, `PATH`) pass through.

### What is NOT Confined (Explicit Owner Decisions)
- **Outbound Network Access**: Outbound network requests are permitted for `agy` and `claude` so backend model APIs function. `API_KEY` authentication (or trusted LAN peer restriction) is the primary trust boundary.
- **DNS-Rebinding TOCTOU**: Attachment URL downloads validate IP addresses against SSRF blacklists before fetching, but do not pin IPs across DNS re-resolutions.
- **Untested Workspace Writes**: The control test for writing inside the job workspace has never produced a valid verified result, because both test attempts checked for the file after the ephemeral workspace directory was already cleaned up/deleted by the runner upon job completion.


