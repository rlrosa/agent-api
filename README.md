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
| `HOST` | `127.0.0.1` | Server bind IP address. |
| `PORT` | `8090` | Server HTTP port. |
| `DB_PATH` | `./data/jobs.db` | SQLite database filepath. |
| `WORK_ROOT` | `/var/tmp/agent-api/jobs` | Directory for per-job isolated workspaces. |
| `BWRAP_ENABLED` | `1` | Enable Bubblewrap OS sandboxing for `agy` (1=true, 0=false). |
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



---

## Security Posture & Boundary Matrix

### What IS Confined and Hardened
- **Host Filesystem Write Protection**:
  - `agy`: Run inside an unprivileged Bubblewrap (`bwrap`) OS container namespace (`--tmpfs /home/ubuntu`). Attempts to write to `$HOME`, `~/.ssh/`, or `~/.bashrc` are intercepted at the OS level and fail silently without modifying the host.
  - `claude`: Confined via CLI tool permission flags (`--allowed-tools View,Read --permission-mode dontAsk`). Writes and shell invocations outside the allowed set are rejected outright.
- **Strict Caller Request Lock**: Caller requests are validated with Pydantic `extra="forbid"`. Any payload containing `sandbox`, `tools`, `permissions`, `dangerously_skip_permissions`, or CLI flags returns **HTTP 422 Unprocessable Entity**.
- **Zero Bypass Flags**: `--dangerously-skip-permissions` is completely removed from all execution code paths.
- **Per-Job Attachment Isolation**: Attachments are stored in isolated per-job directories (`<work_root>/<job_id>/attachments/`) and passed via explicit absolute paths, preventing cross-job contamination.
- **Environment Scrubbing**: Sensitive environment variables (`API_KEY`, secret tokens, credentials) are stripped before subprocess spawning; only essential variables (`HOME`, `PATH`) pass through.

### What is NOT Confined (Explicit Owner Decisions)
- **Unrestricted Host Reads**: Filesystem reads (`/etc/passwd`, local files) are permitted so agents can process attached files. Treat any file readable by the server user as visible to jobs.
- **Outbound Network Access**: Outbound network requests are permitted for `agy` so backend Gemini model APIs function. `API_KEY` authentication is the primary trust boundary.
- **DNS-Rebinding TOCTOU**: Attachment URL downloads validate IP addresses against SSRF blacklists before fetching, but do not pin IPs across DNS re-resolutions.
- **Untested Workspace Writes**: The control test for writing inside the job workspace has never produced a valid verified result, because both test attempts checked for the file after the ephemeral workspace directory was already cleaned up/deleted by the runner upon job completion.

