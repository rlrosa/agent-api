# AGENTS.md

Working notes for agents (and humans) making changes to `agent-api`.

## What this service is

A FastAPI job queue that exposes CLI AI agents (`agy`, `claude`) as HTTP-triggered background
jobs. A caller POSTs a prompt; a dispatcher claims the job and runs the agent as a subprocess
under `bubblewrap` confinement; output is stored on the job row and returned to the caller.

It is reachable from the internet through a Cloudflare tunnel that terminates on `127.0.0.1:8090`,
and it is bound to `0.0.0.0`, so it is also reachable on the LAN. **Treat every request as
untrusted.**

## Layout

| Path | What lives there |
| --- | --- |
| `app/main.py` | HTTP surface — every route, and `verify_api_key` (the auth boundary) |
| `app/security.py` | API-key parsing and constant-time comparison; the per-caller limiter |
| `app/config.py` | All settings. `get_settings()` reads env; see the shadowing trap below |
| `app/agents.py` | Per-agent argv construction and the model/effort resolvers |
| `app/runner.py` | Subprocess spawn, the `bwrap` sandbox, env scrubbing, timeout/kill, workspace lifecycle and the orphan reaper |
| `app/ratelimit.py` | The 5-layer rate-limit classifier and AIMD concurrency manager |
| `app/dispatcher.py` | The claim/run loop; drives AIMD backoff/recovery |
| `app/attachments.py` | URL fetching, the SSRF guard, filename sanitisation |
| `app/db.py` | SQLite access. Every job query must be scoped — see Invariants |
| `app/static/dashboard.html` | Unauthenticated monitoring page |
| `doc/security-review-2026-08-10.md` | Full security review: findings, evidence, what's fixed |

## Running it

```bash
API_KEY=dev .venv/bin/pytest -q            # 37 tests. API_KEY is REQUIRED: without it collection fails
API_KEY=dev ./run.sh                       # local uvicorn
systemctl --user restart agent-api.service # the deployed unit (reads ~/.config/agent-api/env)
```

The deployed service reads its config from `~/.config/agent-api/env`, **not** from anything in
this repo. An explicitly set env var always beats the code default, so changing a default here
does not change the running service until that file is edited and the unit restarted.

## Rate-limit classification (`app/ratelimit.py`)

`is_rate_limit_error()` decides whether a failed job should be retried with backoff. It is a
5-layer classifier, and the ordering is load-bearing:

1. `exit_code == 0` with non-empty stdout is **success** — never rate-limited, whatever the text says.
2. If stdout parses as a JSON dict with `status == "SUCCESS"`, it is not rate-limited.
3. Otherwise strip the `response` key from the envelope before any pattern matching.
4. Apply the regex patterns to **stderr, error, and envelope metadata only**.
5. Only if stdout was unparseable *and* `exit_code != 0`, fall back to scanning raw stdout.

**Never regex-scan the `response` payload.** It holds agent output — OCR text, prices like
`$ 429,00`, addresses like `AV ITALIA 4429` — and matching against it turns the agent's own
output into a control-flow signal, causing endless retries of successful jobs. The bare `429`
pattern is word-bounded for the same reason. This is the same principle as the argv rule below:
untrusted content is data, never a control signal.

## Workspace lifecycle

Job workspaces live under `WORK_ROOT` (`/var/tmp/agent-api/jobs/<job_id>`). Deletion is
**deferred while a job still has retries left**, so evidence survives for diagnosis, and
`sweep_orphaned_workspaces()` reaps directories with no live job on startup and on a retention
sweep (`min_age_seconds` guards against racing a job that is mid-setup). Consequence worth
holding in mind: caller-supplied attachments persist on disk longer than one job run.

## Security invariants — do not regress these

These are the properties the 2026-08 review established. Each was a real finding; breaking one
re-opens it.

1. **No caller string reaches a subprocess argv.** `model` and `effort` are resolved against the
   literal sets in `app/agents.py` and only the matched literal is used. Never interpolate request
   data into argv, and never build a command string for a shell — spawning is
   `create_subprocess_exec(*argv)` and must stay that way.
2. **Job queries are scoped to the caller.** `get_job`, `list_jobs` and `cancel_job` take
   `api_key_name` and enforce ownership *in SQL*. A named key must only see its own jobs and must
   get `404` (never `403`) for someone else's, so job IDs are not an existence oracle. `bypass` is
   deliberately unscoped — it is the local admin identity.
3. **No credential is ever a default.** `get_settings()` must not substitute a literal key when
   none is configured.
4. **`TRUSTED_NETWORKS` defaults to loopback only.** Adding a network here is equivalent to
   publishing an API key to that network. The default, the installer heredoc, `.env.example` and
   the docs must agree.
5. **Confinement fails closed.** With `BWRAP_ENABLED=1` and `ALLOW_UNCONFINED=0`, a missing
   `bwrap` must raise and abort the job before any process is spawned.
6. **Only key *names* are logged, never key values.**
7. **The SSRF guard validates every redirect hop** before the request is sent, and attachment size
   limits are enforced per-chunk while streaming — never from a `Content-Length` the remote
   supplied.
8. **Agent output is data, never a control signal** — see the classifier rule above.

## Traps this codebase has bitten people with

- **`config.py` has two defaults per setting and the second one wins.** The `Settings` field
  default (top of file) is shadowed by the explicit `os.environ.get(NAME, fallback)` in
  `get_settings()`. Editing the field default alone changes nothing at runtime — and leaves the
  two disagreeing, which is worse than either. Change both, or neither.
- **Settings that nothing reads.** `EGRESS_RESTRICT` and `CLAUDE_DISALLOWED_TOOLS` are defined and
  documented but never consulted (F6/F7). Before relying on a setting, grep for it in `app/` — a
  control that silently does nothing is worse than an absent one. Don't add another.
- **Resolved values are persisted, then re-fed to the builder.** `main.py` stores the resolved
  `model`/`effort` on the job row and `run_job` passes that row back into the argv builder. Store a
  default where the caller supplied nothing and you pin every job to it.
- **The job's own output is an exfiltration channel.** `stdout`/`stderr` are columns on the job row
  and are returned to the caller. Anything the agent can read, it can return. Egress filtering does
  not close that path.

## Open findings

Tracked in `doc/security-review-2026-08-10.md`. Fixed: F1, F2, F4, F8. Still open:

- **F5 (High, risk accepted)** — the host's `~/.gemini` is bind-mounted read-only into the sandbox,
  so `antigravity-oauth-token` *and* `history.jsonl` are readable by a prompt-injected job on the
  default `agy` path, which also has outbound network and whose output is returned to the caller.
  The operator accepted this (2026-09-06) on the basis that the token is subscription-scoped, and
  added `--dangerously-skip-permissions` to the `agy_sandbox_flags` field default. **That flag is
  inert as committed** — `get_settings()` shadows the field default. Do not "fix" the shadowing
  without realising it switches the flag on for every agy job.
- **F6 (Medium)** — `EGRESS_RESTRICT` is documented as the mitigation for the project's own top
  accepted risk and is read nowhere; `--share-net` is unconditional.
- **F7 (Low)** — `CLAUDE_DISALLOWED_TOOLS` is ignored; `build_claude_argv` hardcodes
  `--allowed-tools View,Read`.
- **F9 (Low)** — `scripts/install.sh` writes the generated key before `chmod 0600`.

## Conventions

- Match the surrounding style; no drive-by refactors in a fix.
- Security-relevant changes get a regression test (see `tests/test_api.py`).
- Claim a behaviour only with evidence: the exact command, its complete output, and its exit code.
  "Tests pass" is not evidence; the run output is.
