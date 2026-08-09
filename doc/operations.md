# Operational Guide & Service Management

## Service Architecture
`agent-api` runs as a systemd user unit on `0.0.0.0:8090`.

- **Main Unit**: `agent-api.service` (`/home/ubuntu/.config/systemd/user/agent-api.service`)
- **Environment File**: `~/.config/agent-api/env` (permissions `0600`)
- **Database**: SQLite WAL mode (`./data/jobs.db` and `./data/events.db`)

## Service Commands

```bash
# Check service status
systemctl --user status agent-api.service --no-pager

# Restart service
systemctl --user restart agent-api.service

# View live logs
journalctl --user -u agent-api.service -f

# Check health endpoint
curl -s http://127.0.0.1:8090/healthz | jq .
```

## Security & Auth Alerting Watchdog

A dedicated watchdog timer checks authentication failure metrics every 15 minutes:

```bash
# Check timer status
systemctl --user status agent-api-auth-check.timer --no-pager

# Check watchdog script manually
/home/ubuntu/agent/check_agent_api_auth.py

# View watchdog logs
journalctl --user -u agent-api-auth-check.service --no-pager
```

## Troubleshooting & Log Inspection
- **Events Log Endpoint**: `GET /v1/logs?level=ERROR&limit=50`
- **Security Endpoint**: `GET /v1/stats/security`
- **Dashboard UI**: `http://192.168.87.132:8090/dashboard` or `https://agent-api.rela.uy/dashboard`
