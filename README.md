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

## Quick Start


### 1. Requirements & Setup
- Python 3.10+
- `bwrap` (Bubblewrap 0.6+) for OS-level sandboxing
- Configured agent binaries in PATH (`agy` and `claude`)

```bash
# Create virtual environment and install dependencies
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. Persistent Production Service Management (`systemd --user`)

The service runs persistently under `systemd --user` with `Restart=always` and `loginctl enable-linger ubuntu` (surviving reboots and logouts).

- **Environment & Key File**: Configured on the server in `~/.config/agent-api/env` (mode `0600`).
- **Service Status**: `systemctl --user status agent-api`
- **Restart Service**: `systemctl --user restart agent-api`
- **Stop Service**: `systemctl --user stop agent-api`
- **Start Service**: `systemctl --user start agent-api`
- **View Live Logs**: `journalctl --user -u agent-api -f`

To run manually in foreground (development mode):
```bash
API_KEY="your-secret-api-key" ./run.sh
```

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
| `TRUSTED_NETWORKS` | `192.168.87.0/24,100.64.0.0/10` | Trusted CIDR networks for API key auth bypass (LAN & Tailscale). |
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
  - Requests from raw peer socket IPs matching `TRUSTED_NETWORKS` (`192.168.87.0/24`, `100.64.0.0/10`) bypass API key authentication.
  - Loopback (`127.0.0.1`) is **not** trusted by default.
  - Any request with Cloudflare Tunnel headers (`CF-Connecting-IP`, `CF-Ray`, `CF-Visitor`) **always requires the API key**, preventing public internet bypass over local tunnel proxies.
- **Zero Bypass Flags**: `--dangerously-skip-permissions` is completely removed from all execution code paths.
- **Per-Job Attachment Isolation**: Attachments are stored in isolated per-job directories (`<work_root>/<job_id>/attachments/`) and passed via explicit absolute paths, preventing cross-job contamination.
- **Environment Scrubbing**: Sensitive environment variables (`API_KEY`, secret tokens, credentials) are stripped before subprocess spawning; only essential variables (`HOME`, `PATH`) pass through.

### What is NOT Confined (Explicit Owner Decisions)
- **Outbound Network Access**: Outbound network requests are permitted for `agy` and `claude` so backend model APIs function. `API_KEY` authentication (or trusted LAN peer restriction) is the primary trust boundary.
- **DNS-Rebinding TOCTOU**: Attachment URL downloads validate IP addresses against SSRF blacklists before fetching, but do not pin IPs across DNS re-resolutions.
- **Untested Workspace Writes**: The control test for writing inside the job workspace has never produced a valid verified result, because both test attempts checked for the file after the ephemeral workspace directory was already cleaned up/deleted by the runner upon job completion.


