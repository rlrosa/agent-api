#!/usr/bin/env bash
set -e

WORKTREE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEMP_DB="/tmp/smoke_agent_api_$$_.db"
EVIDENCE_DIR="/tmp/prl-rondeau/members/worker/evidence"
SMOKE_OUT="$EVIDENCE_DIR/33-03-m31-smoke.txt"

mkdir -p "$EVIDENCE_DIR"
rm -f "$TEMP_DB"

# Ensure ports are free
fuser -k 18090/tcp 18091/tcp 2>/dev/null || true
sleep 0.5

echo "=== Seeding scratch database at $TEMP_DB ==="
export API_KEY="test-key"
/home/ubuntu/agent-api/.venv/bin/python3 "$WORKTREE_DIR/tests/fixtures/seed_dashboard.py" "$TEMP_DB"

UVICORN_PID=""
PROXY_PID=""

cleanup() {
    echo "=== Cleaning up processes ==="
    if [ -n "$PROXY_PID" ]; then
        kill "$PROXY_PID" 2>/dev/null || true
    fi
    if [ -n "$UVICORN_PID" ]; then
        kill "$UVICORN_PID" 2>/dev/null || true
    fi
    rm -f "$TEMP_DB"
}
trap cleanup EXIT

echo "=== Starting scratch uvicorn on 127.0.0.1:18090 ==="
export DB_PATH="$TEMP_DB"
export API_KEYS="t-jobs:key-jobs-sec:jobs,t-stats:key-stats-sec:stats,t-admin:key-admin-sec:jobs+stats+admin"
export AGY_USAGE_CMD="$WORKTREE_DIR/tests/fixtures/fake_agy_usage.sh"
export ALLOW_MOCK_AGENT="1"
export LOG_LEVEL="INFO"

/home/ubuntu/agent-api/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 18090 &
UVICORN_PID=$!

echo "Waiting for uvicorn to respond on :18090..."
for i in $(seq 1 30); do
    if curl -s http://127.0.0.1:18090/healthz >/dev/null 2>&1; then
        echo "Uvicorn is ready."
        break
    fi
    sleep 0.5
done

echo "=== Starting prefix proxy on 127.0.0.1:18091 ==="
/home/ubuntu/agent-api/.venv/bin/python3 "$WORKTREE_DIR/tests/fixtures/prefix_proxy.py" 18091 &
PROXY_PID=$!
sleep 1

echo "=== Running smoke test curl commands ===" > "$SMOKE_OUT"

echo "--- 1. /v1/me for bypass (loopback without key) ---" >> "$SMOKE_OUT"
echo "$ curl -s http://127.0.0.1:18090/v1/me" >> "$SMOKE_OUT"
curl -s http://127.0.0.1:18090/v1/me >> "$SMOKE_OUT"
echo "" >> "$SMOKE_OUT"

echo "--- 2. /v1/me for t-jobs key ---" >> "$SMOKE_OUT"
echo "$ curl -s -H 'X-API-Key: key-jobs-sec' http://127.0.0.1:18090/v1/me" >> "$SMOKE_OUT"
curl -s -H "X-API-Key: key-jobs-sec" http://127.0.0.1:18090/v1/me >> "$SMOKE_OUT"
echo "" >> "$SMOKE_OUT"

echo "--- 3. /v1/me for t-stats key ---" >> "$SMOKE_OUT"
echo "$ curl -s -H 'X-API-Key: key-stats-sec' http://127.0.0.1:18090/v1/me" >> "$SMOKE_OUT"
curl -s -H "X-API-Key: key-stats-sec" http://127.0.0.1:18090/v1/me >> "$SMOKE_OUT"
echo "" >> "$SMOKE_OUT"

echo "--- 4. /v1/me for t-admin key ---" >> "$SMOKE_OUT"
echo "$ curl -s -H 'X-API-Key: key-admin-sec' http://127.0.0.1:18090/v1/me" >> "$SMOKE_OUT"
curl -s -H "X-API-Key: key-admin-sec" http://127.0.0.1:18090/v1/me >> "$SMOKE_OUT"
echo "" >> "$SMOKE_OUT"

echo "--- 5. stats key -> /v1/stats/agy-quota (200, with buckets) ---" >> "$SMOKE_OUT"
echo "$ curl -s -H 'X-API-Key: key-stats-sec' http://127.0.0.1:18090/v1/stats/agy-quota" >> "$SMOKE_OUT"
curl -s -H "X-API-Key: key-stats-sec" http://127.0.0.1:18090/v1/stats/agy-quota >> "$SMOKE_OUT"
echo "" >> "$SMOKE_OUT"

echo "--- 6. stats key -> /v1/stats/sources?by=key ---" >> "$SMOKE_OUT"
echo "$ curl -s -H 'X-API-Key: key-stats-sec' 'http://127.0.0.1:18090/v1/stats/sources?by=key'" >> "$SMOKE_OUT"
curl -s -H "X-API-Key: key-stats-sec" "http://127.0.0.1:18090/v1/stats/sources?by=key" >> "$SMOKE_OUT"
echo "" >> "$SMOKE_OUT"

echo "--- 7. jobs key -> /v1/stats/summary (403 Forbidden) ---" >> "$SMOKE_OUT"
echo "$ curl -s -i -H 'X-API-Key: key-jobs-sec' http://127.0.0.1:18090/v1/stats/summary" >> "$SMOKE_OUT"
curl -s -i -H "X-API-Key: key-jobs-sec" http://127.0.0.1:18090/v1/stats/summary >> "$SMOKE_OUT"
echo "" >> "$SMOKE_OUT"

echo "--- 8. /dashboard with CF header and no key (401 Unauthorized) ---" >> "$SMOKE_OUT"
echo "$ curl -s -i -H 'cf-ray: test-ray-x' http://127.0.0.1:18090/dashboard" >> "$SMOKE_OUT"
curl -s -i -H "cf-ray: test-ray-x" http://127.0.0.1:18090/dashboard | head -n 12 >> "$SMOKE_OUT"
echo "" >> "$SMOKE_OUT"

echo "=== Running Playwright screenshots ==="
node "$WORKTREE_DIR/tests/fixtures/screenshot_dashboard.js"

echo "=== Scratch smoke run complete ==="
