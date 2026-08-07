# Agent API Service

A lightweight, secure FastAPI HTTP service that exposes CLI-based AI agents (`agy` and `claude`) as non-interactive backend jobs for document and image processing tasks.

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

### 2. Running the API Server
Set `API_KEY` (required) and launch via `./run.sh`:

```bash
API_KEY="your-secret-api-key" ./run.sh
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
curl -s http://127.0.0.1:8090/healthz
```

### 2. Submit Text Job (Wait for Completion)
`POST /v1/jobs`

```bash
curl -s -X POST http://127.0.0.1:8090/v1/jobs \
  -H "X-API-Key: your-secret-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "agent": "agy",
    "prompt": "Reply with exactly: pong",
    "wait": 60
  }'
```

### 3. Attachment Form A: URL Download
`POST /v1/jobs`

```bash
curl -s -X POST http://127.0.0.1:8090/v1/jobs \
  -H "X-API-Key: your-secret-api-key" \
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
  }'
```

### 4. Attachment Form B: Base64 Payload
`POST /v1/jobs`

```bash
curl -s -X POST http://127.0.0.1:8090/v1/jobs \
  -H "X-API-Key: your-secret-api-key" \
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
  }'
```

### 5. Attachment Form C: Multipart File Upload
`POST /v1/jobs`

```bash
curl -s -X POST http://127.0.0.1:8090/v1/jobs \
  -H "X-API-Key: your-secret-api-key" \
  -F "agent=agy" \
  -F "prompt=What is written on this document?" \
  -F "wait=60" \
  -F "files=@/tmp/m0/doc.pdf"
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
  }'
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


