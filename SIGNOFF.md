# Human Acceptance & Sign-off Verification Guide

This document provides a 6-step, copy-pasteable verification sequence to validate end-to-end functionality, security controls, and attachment handling of the `agent-api` server.

---

## Step 1: Start the Agent API Server
In a terminal window, set the `API_KEY` environment variable and launch the server on its default port (**8090**):

```bash
API_KEY="test-secret-key" ./run.sh
```

**Expected Output**:
```text
INFO:     Started server process
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8090 (Press CTRL+C to quit)
```

---

## Step 2: Health Check Endpoint (`GET /healthz`)
In a second terminal window, verify that the health check endpoint returns 200 OK without requiring authentication:

```bash
curl -s http://127.0.0.1:8090/healthz
```

**Expected Output**:
```json
{"version":"0.1.0","max_concurrency":3,"queue_depth":0,"running_count":0,"effective_concurrency":3,"agents":{"agy":{"available":true,"path":"/home/ubuntu/.local/bin/agy"},"claude":{"available":true,"path":"/home/ubuntu/.local/bin/claude"},"codex":{"available":false,"path":null},"mock_429":{"available":false,"path":null}}}
```

---

## Step 3: Verify API Key Authentication Boundary
Submit a job request without the `X-API-Key` header and verify that access is denied with HTTP 401 Unauthorized:

```bash
curl -s -o /dev/null -w "HTTP Status Code: %{http_code}\n" -X POST http://127.0.0.1:8090/v1/jobs \
  -H "Content-Type: application/json" \
  -d '{"agent":"agy","prompt":"hi"}'
```

**Expected Output**:
```text
HTTP Status Code: 401
```

---

## Step 4: Submit Inline Text Job with Completion Wait
Set your `AGENT_API_KEY` environment variable in your shell session, then submit a prompt to `agy` with `wait=60`:

```bash
export AGENT_API_KEY="test-secret-key"

curl -s -X POST http://127.0.0.1:8090/v1/jobs \
  -H "X-API-Key: $AGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "agent": "agy",
    "prompt": "Reply with exactly: PONG_SIGNOFF_TEST",
    "wait": 60
  }'
```

**Expected Output**:
```json
{"id":"b53eafc5-2951-4aa9-be8a-3cbf02df7d89","agent":"agy","model":null,"effort":null,"prompt":"Reply with exactly: PONG_SIGNOFF_TEST","status":"completed","attempts":1,"next_attempt_at":1786120601.7823367,"workdir":null,"exit_code":0,"stdout":"PONG_SIGNOFF_TEST\n","stderr":"","error":null,"metadata":null,"wait":60,"timeout":120,"created_at":1786120601.7823367,"started_at":1786120601.7916949,"finished_at":1786120612.298094}
```

---

## Step 5: Process PDF Attachment via URL Download
Submit a job to `claude` with an ArXiv PDF URL attachment and verify text extraction:

```bash
curl -s -X POST http://127.0.0.1:8090/v1/jobs \
  -H "X-API-Key: $AGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "agent": "claude",
    "prompt": "What is the title on page 1 of this PDF?",
    "attachments": [
      {
        "filename": "attention.pdf",
        "url": "https://arxiv.org/pdf/1706.03762.pdf"
      }
    ],
    "wait": 60
  }'
```

**Expected Output**:
```json
{"id":"7f7a8bdc-3e54-4557-aaec-acf2821e78eb","agent":"claude","model":null,"effort":null,"prompt":"What is the title on page 1 of this PDF?\n\nAttached files (read them from disk as needed):\n- /var/tmp/agent-api/jobs/7f7a8bdc-3e54-4557-aaec-acf2821e78eb/attachments/1706.03762v7.pdf","status":"completed","attempts":1,"next_attempt_at":1786120612.481767,"workdir":null,"exit_code":0,"stdout":"**\"Attention Is All You Need\"** — the 2017 NIPS paper by Vaswani et al. (arXiv:1706.03762v7) that introduced the Transformer architecture.\n","stderr":"","error":null,"metadata":null,"wait":60,"timeout":120,"created_at":1786120612.481767,"started_at":1786120612.4898074,"finished_at":1786120622.3058639}
```

---

## Step 6: Process Multipart File Upload Attachment
Generate a sample verification PDF using Python (zero external dependencies required) and submit it via multipart form upload:

```bash
python3 -c "
pdf_content = (
    b'%PDF-1.4\n'
    b'1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n'
    b'2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n'
    b'3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj\n'
    b'4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n'
    b'5 0 obj<</Length 62>>stream\n'
    b'BT /F1 24 Tf 100 700 Td (SIGNOFF VERIFICATION CODE: ALPHA-9988) Tj ET\n'
    b'endstream\n'
    b'endobj\n'
    b'xref\n'
    b'0 6\n'
    b'0000000000 65535 f \n'
    b'0000000009 00000 n \n'
    b'0000000052 00000 n \n'
    b'0000000101 00000 n \n'
    b'0000000223 00000 n \n'
    b'0000000291 00000 n \n'
    b'trailer<</Size 6/Root 1 0 R>>\n'
    b'startxref\n'
    b'403\n'
    b'%%EOF\n'
)
with open('/tmp/signoff_sample.pdf', 'wb') as f:
    f.write(pdf_content)
"

curl -s -X POST http://127.0.0.1:8090/v1/jobs \
  -H "X-API-Key: $AGENT_API_KEY" \
  -F "agent=claude" \
  -F "prompt=Read the attached signoff_sample.pdf file and state its verification code." \
  -F "wait=60" \
  -F "files=@/tmp/signoff_sample.pdf"
```

**Expected Output**:
```json
{"id":"e47b6dfa-2ee4-4859-9f42-4180f494ae44","agent":"claude","model":null,"effort":null,"prompt":"Read the attached signoff_sample.pdf file and state its verification code.\n\nAttached files (read them from disk as needed):\n- /var/tmp/agent-api/jobs/e47b6dfa-2ee4-4859-9f42-4180f494ae44/attachments/signoff_sample.pdf","status":"completed","attempts":1,"next_attempt_at":1786120622.3679147,"workdir":null,"exit_code":0,"stdout":"The verification code is **ALPHA-9988**.\n","stderr":"","error":null,"metadata":null,"wait":60,"timeout":120,"created_at":1786120622.3679147,"started_at":1786120622.3771014,"finished_at":1786120627.8902175}
```
