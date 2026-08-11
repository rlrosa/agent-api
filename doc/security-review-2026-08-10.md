# Security Review — agent-api

**Date:** 2026-08-10
**Commit reviewed:** `ab250a7` (branch `main`, working tree clean, identical to `origin/main`)
**Scope:** Full repository — `app/`, `scripts/install.sh`, `run.sh`, `.env.example`, `.gitignore`,
and all six commits of git history. Not a diff review: there was no pending branch diff to review,
so the entire codebase was treated as new code.
**Type:** Review only. **No source file was modified.** All remediations below are described, not
applied.

## Method

Analysis was performed by a worker agent driving code reads and instrumented Python probes; every
finding was then independently re-verified by the reviewer against the cited lines before being
admitted to this report. Claims that could not be confirmed were dropped or moved to
[Unconfirmed](#unconfirmed). Each finding records how it was verified.

Raw evidence (exact commands, complete output, exit codes) is retained at
`/tmp/prl-agentapi-secrev/members/worker/evidence/` — 14 files. Evidence pointers below name the
file that actually demonstrates the finding.

Excluded by policy: denial of service, resource exhaustion, rate-limit strength, dependency
versions, missing hardening absent a concrete attack path, theoretical races, and issues in
documentation. Environment variables and CLI flags are treated as trusted inputs.

## Summary

| ID | Title | Severity | Location | Status |
| --- | --- | --- | --- | --- |
| F1 | Hardcoded fallback API key accepts any caller | **High** | `app/config.py:76` | **Fixed** (uncommitted) |
| F2 | Auth-bypass subnets shipped as default *and* written by the installer | **High** | `app/config.py:65`, `scripts/install.sh:70` | **Fixed** (uncommitted) |
| F5 | Host credentials mounted into the sandbox with unrestricted egress | **High** | `app/runner.py:115,121-143` | Open |
| F8 | Any caller can read and cancel any other caller's jobs | **High** | `app/main.py:395-431`, `app/db.py:309,330` | **Fixed** (uncommitted) |
| F6 | `EGRESS_RESTRICT` is a documented control that does nothing | **Medium** | `app/config.py:49,103` | Open |
| F4 | Unvalidated `model` / `effort` reach the `claude` argv | **Low** | `app/agents.py:88,90-91` | Open (assessed, see below) |
| F7 | `CLAUDE_DISALLOWED_TOOLS` is a documented control that does nothing | **Low** | `app/config.py:32,98` | Open |
| F9 | Installer writes the API key before restricting file permissions | **Low** | `scripts/install.sh:66-76` | Open |

Four High findings. F1, F2 and F8 compose: on a default install, an unauthenticated peer in a
trusted subnet can submit agent jobs *and* read every other caller's prompts and outputs.

## Remediation status (2026-08-10)

F1, F2 and F8 were fixed in the working tree after the review. **The changes are uncommitted** and
the suite is green (25 tests before, 27 after — two regression tests added; the reviewer re-ran it
independently). What changed:

* **F1** — the `or "default-secret-key"` substitution is gone; `Settings.api_key` now defaults to
  `""` so `parse_api_keys()` registers nothing when only `API_KEYS` is configured.
* **F2** — the default is loopback-only (`127.0.0.1/32,::1/128,127.0.0.0/8`) in all four places
  that shipped the risky value: `config.py:65`, the `Settings` default factory, the
  `scripts/install.sh` heredoc, and `.env.example`, with `README.md` and `doc/networking.md`
  updated to state that adding a network here grants that network full auth bypass.
* **F8** — `get_job`, `list_jobs` and `cancel_job` now take the authenticated identity and enforce
  ownership **in SQL**. A named key sees and acts on only its own jobs and receives `404` (not
  `403`) for another key's job, so job IDs are not an existence oracle. Per operator decision,
  `bypass` is unscoped and retains full visibility as the local admin identity. Legacy rows with a
  NULL `api_key_name` are attributed to the key named `default`; new rows always record an owner
  (`main.py:352`).

> **This does not secure the running deployment.** The live service reads `TRUSTED_NETWORKS` from
> `~/.config/agent-api/env`, and an explicitly set value still overrides the new default by design.
> Until that file is edited to loopback-only and the service restarted, F2 remains live on this
> host. F5, F6, F7 and F9 are untouched.

---

## F1 — Hardcoded fallback API key accepts any caller

* **Severity:** High · **Category:** `auth_bypass` / `hardcoded_credential`
* **Location:** `app/config.py:76`, reached via `app/security.py:13-14`
* **Untrusted input:** the `X-API-Key` request header, from any network location.

**Path.** `get_settings()` rejects a configuration only when *both* `API_KEY` and `API_KEYS` are
empty (`config.py:62-63`). An operator who configures multiple named keys — the documented
multi-key feature — sets only `API_KEYS` and leaves `API_KEY` unset. Line 76 then substitutes a
literal:

```python
api_key=api_key or "default-secret-key",
```

`parse_api_keys()` registers any non-empty `settings.api_key` as the key named `default`
(`security.py:13-14`), so `default-secret-key` becomes a valid credential.

**Exploit scenario.** Operator sets `API_KEYS="alice:s3cret,bob:h0nk"`. An attacker anywhere on
the internet sends `POST /v1/jobs` with `X-API-Key: default-secret-key`, is authenticated as
`default`, and executes agent CLI jobs on the host. The string is in the public source, so this
requires no guessing.

**Verified how.** Read `config.py:62-76` and `security.py:8-27` directly; confirmed by executing
the real module — `parse_api_keys()` returned `{'default': 'default-secret-key', 'alice': 'key1'}`
with `API_KEYS` set and `API_KEY` unset.

**Evidence:** `evidence/03-default-key-fallback.txt`

**Recommendation.** Do not substitute a literal. Use `api_key=api_key or ""` so no key is
registered when only `API_KEYS` is configured; `parse_api_keys()` already skips falsy values.

---

## F2 — Auth-bypass subnets shipped as default and written by the installer

* **Severity:** High · **Category:** `auth_bypass` / `insecure_default`
* **Location:** `app/config.py:65`; `scripts/install.sh:70`; bypass applied at `app/main.py:157-162`
* **Untrusted input:** a direct TCP connection from any address in the trusted ranges.

**Path.** The default `TRUSTED_NETWORKS` is
`127.0.0.1/32,::1/128,127.0.0.0/8,192.168.87.0/24,100.64.0.0/10`. A peer in any of those ranges
that presents no Cloudflare header is assigned `key_name = "bypass"` at `main.py:161-162` with no
credential check, on all 11 authenticated routes — including `POST /v1/jobs`, which executes an
agent CLI, and `DELETE /v1/jobs/{job_id}`.

This is not merely an unused fallback. `scripts/install.sh:70` writes
`TRUSTED_NETWORKS="192.168.87.0/24,100.64.0.0/10"` into `~/.config/agent-api/env` on every fresh
install, and `HOST` defaults to `0.0.0.0` (`config.py:9,77`), so the port is bound on every
interface. `100.64.0.0/10` is shared CGNAT space — roughly 4.2M addresses that are not under the
operator's control if the host has a CGNAT WAN address.

**Exploit scenario.** An attacker sharing the LAN, the tailnet, or the ISP's CGNAT block connects
directly to `http://<host>:8090/v1/jobs` with no `X-API-Key` and submits a job. Chained with F8,
the same unauthenticated peer reads every other caller's prompts and outputs.

**Verified how.** Read `config.py:65` and `main.py:97-109,152-162`; read the installer heredoc at
`install.sh:66-76`; confirmed CIDR matching by calling the real `is_trusted_peer` —
`100.64.1.50 → True`, `192.168.87.50 → True`, `8.8.8.8 → False`.

**Evidence:** `evidence/04-trusted-networks-defaults.txt`

**Recommendation.** Restrict the shipped default and the installer-written value to loopback only.
Require a deliberate opt-in (e.g. `ALLOW_TRUSTED_NETWORK_BYPASS=1`) before any non-loopback range
grants a credential-free identity, and document that any range added here is equivalent to
publishing an API key to that range.

---

## F5 — Host credentials mounted into the sandbox with unrestricted egress

* **Severity:** High · **Category:** `credential_exposure` / `overprivileged_sandbox_mount`
* **Location:** `app/runner.py:115` (`--share-net`), `:121-129` (agy), `:130-143` (claude)
* **Untrusted input:** the job `prompt`, chosen by the HTTP caller.

**Path.** `wrap_cmd_with_bwrap` masks `/home/ubuntu` with a tmpfs, then re-exposes credential
material read-only inside the sandbox:

* **agy (the default agent, `DEFAULT_AGENT=agy`):** `--ro-bind /home/ubuntu/.gemini` mounts the
  directory *whole*. Only four `antigravity-cli` subdirectories (`brain`, `conversations`,
  `cache`, `log`) are masked. `antigravity-cli/antigravity-oauth-token` (mode 0600, OAuth access
  and refresh token) sits directly in the mounted tree and is **not** masked. `history.jsonl`
  (mode 0600, prompt history) is likewise readable.
* **claude:** `--ro-bind` of `.claude/.credentials.json`, `.claude/settings.json` and
  `/home/ubuntu/.claude.json`.

`--share-net` is added unconditionally at line 115, so the confined process has unrestricted
outbound network.

**Exploit scenario.** A caller submits a prompt directing the agent to read its own credential
file and POST the contents to an attacker-controlled host. The agent has read access to the token
and an open network path out. The result is theft of a host OAuth credential, not merely of job
data. The `agy` path is the default, so no unusual request is needed.

**Verified how.** Read `runner.py:112-143` directly and confirmed `--share-net` is unconditional;
confirmed the mount list by calling the real `wrap_cmd_with_bwrap`, whose output contains
`--ro-bind /home/ubuntu/.claude/.credentials.json` and `--ro-bind /home/ubuntu/.gemini`; confirmed
the token file's presence and mode by directory listing (contents deliberately not captured).

**Evidence:** `evidence/06-bwrap-argv.txt` (real constructed argv),
`evidence/10-gemini-credentials-mount.txt` (file listing, no secret values)

**Recommendation.** Mount only what each CLI needs to authenticate, never a whole credential
directory — bind the specific token file, or better, hand the agent a short-lived scoped
credential rather than the host's own. `doc/security.md:38-40` already accepts prompt-injection
exfiltration as a residual risk, but that acceptance was written against *job data*; exposure of
the host's own OAuth tokens is a materially larger blast radius and should be re-accepted
explicitly or removed. See also F6 — the control that document names as the mitigation does not
exist.

---

## F8 — Any caller can read and cancel any other caller's jobs

* **Severity:** High · **Category:** `broken_object_level_authorization`
* **Location:** `app/main.py:395-431`; `app/db.py:309`, `:330`, `:283`
* **Untrusted input:** the `job_id` path parameter; or no input at all for the listing endpoint.

**Path.** The three job endpoints declare `api_key: str = Depends(verify_api_key)` and then never
reference the value. Authentication happens; authorization does not.

```python
# app/db.py:309
cur = conn.execute("SELECT * FROM jobs WHERE id = ?;", (job_id,))
# app/db.py:330
"SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?;"
```

Neither query has an owner predicate. `GET /v1/jobs` returns every job in the system, `SELECT *`,
which includes the `prompt` column. `cancel_job` (`db.py:283`) is unscoped the same way.

The ownership data exists and is simply never consulted: the schema defines `api_key_name`
(`db.py:84,98`) and it is populated on insert (`db.py:149,163-167`).

**Exploit scenario.** Key `alice` calls `GET /v1/jobs`, reads the prompts and outputs of jobs
submitted by key `bob`, then calls `DELETE /v1/jobs/{bob_job_id}` to terminate a running job.
Because `bypass` is also a caller identity, an unauthenticated peer in a trusted subnet (F2) can
do the same without any key.

**Verified how.** Read `main.py:395-431` and confirmed `api_key` is bound and unused in all three
handlers; read the queries at `db.py:283,309,330` and confirmed no owner predicate; confirmed via
`grep` that `api_key_name` is written at insert but appears in no `WHERE` clause.

**Evidence:** verified directly by the reviewer against the cited lines. The worker's original
pointer (`evidence/01-routes.txt`) does not demonstrate this finding.

**Recommendation.** Scope `get_job`, `list_jobs` and `cancel_job` by the authenticated
`api_key_name`, and decide deliberately what `bypass` should see — most likely its own jobs only,
or nothing, rather than everything. If an administrative view is wanted, make it a distinct
explicitly-privileged key rather than the default behaviour of every key.

---

## F6 — `EGRESS_RESTRICT` is a documented control that does nothing

* **Severity:** Medium · **Category:** `dead_security_control`
* **Location:** defined `app/config.py:49,103`; documented `doc/security.md:40`

`egress_restrict` is parsed into settings and read nowhere in `app/`. `--share-net` is added
unconditionally at `runner.py:115`. `doc/security.md:38-40` names this setting as the mitigation
for the project's own top accepted residual risk (prompt-injection egress exfiltration), so an
operator who sets `EGRESS_RESTRICT=1` believes egress is restricted when it is entirely open. This
is the missing control that makes F5 exploitable end to end.

**Verified how.** `grep -rn egress_restrict app/` returns only the two `config.py` lines; read
`runner.py:112-119` and confirmed `--share-net` is unconditional.

**Evidence:** `evidence/11-dead-security-settings.txt`

**Recommendation.** Either implement it — drop `--share-net` and supply an explicit egress
allowlist when set — or remove the setting and correct `doc/security.md`. A security control that
silently does nothing is worse than an absent one, because it is relied upon.

---

## F4 — Unvalidated `model` / `effort` reach the `claude` argv

* **Severity:** Low · **Category:** `argument_injection`
* **Location:** `app/agents.py:88` and `:90-91`; validation gap at `:23-30`

`validate_agent_model` only has an `if agent == "agy"` branch, so for `claude` it validates
nothing — while `main.py:299` calls it, which makes the path look validated. `effort` is a free
string (`models.py:33`) passed straight through for `claude`, whereas the agy builder normalises it
to `low|medium|high` (`agents.py:42-44`). Two caller-controlled values therefore land in argv.

**Why this is Low, not High.** The injected values occupy the *value* position of `--model` and
`--effort`, and the hardcoded restrictions appear later in argv:

```
['claude','-p','--model','--permission-mode','--effort','--allowed-tools',
 '--allowed-tools','View,Read','--permission-mode','dontAsk','--output-format','json']
```

A CLI probe confirms the parser consumes the flag-shaped token as a model *name* and errors out
rather than treating it as a new flag, so `--allowed-tools View,Read` and `--permission-mode
dontAsk` survive intact. The initial hypothesis that this could lift the tool restrictions did not
survive testing. Impact is a failed job.

**Verified how.** Read `agents.py:23-30,73-98`; reviewed the real constructed argv and the CLI
probe output in the evidence file.

**Evidence:** `evidence/09-claude-argv-parsing.txt`

**Recommendation.** Validate `model` against an allowlist for every agent, not only `agy`, and
normalise `effort` in `build_claude_argv` as the agy builder already does. Defence in depth: the
argument's safety currently depends on a third-party CLI's parser behaviour, which can change.

**Follow-up assessment — should these become enumerated options rather than free strings?**
Considered and **not recommended as a nearest-match scheme**. On the `agy` path the caller's
`model` is already discarded: `build_agy_argv` always constructs `gemini-3.6-flash-{effort}`
(`agents.py:47-55`), and an unknown agy model is already rejected with a 400. Only the `claude`
path forwards the caller's string. A fuzzy nearest-match mapper would add a real hazard of its
own — a caller requesting a cheap model could be silently served an expensive one without being
told. If this is hardened, the right shape is an **explicit allowlist per agent that rejects
unknown values with HTTP 400**, covering `effort` as well as `model`, rather than any silent
coercion.

---

## F7 — `CLAUDE_DISALLOWED_TOOLS` is a documented control that does nothing

* **Severity:** Low · **Category:** `dead_security_control`
* **Location:** defined `app/config.py:32,98`; documented `.env.example:88`

Nothing reads the setting; `build_claude_argv` hardcodes `--allowed-tools View,Read`
(`agents.py:94`). Rated Low rather than Medium because the effective behaviour is *more*
restrictive than the setting implies, so no escalation follows — the risk is an operator believing
they have configured something.

**Verified how.** `grep -rn claude_disallowed_tools app/` returns only the two `config.py` lines;
read `agents.py:93-95`.

**Evidence:** `evidence/11-dead-security-settings.txt`

**Recommendation.** Honour the setting in `build_claude_argv`, or delete the field and its
`.env.example` entry.

---

## F9 — Installer writes the API key before restricting file permissions

* **Severity:** Low · **Category:** `insecure_file_creation`
* **Location:** `scripts/install.sh:66-76`

`cat << EOF > "${ENV_FILE}"` creates the file under the prevailing umask (commonly `0644`) and
writes the generated key into it; `chmod 0600` runs afterwards at line 76. Between the two, the
secret is readable by other local users on a multi-user host. Key generation itself is sound:
`secrets.token_urlsafe(32)` — a CSPRNG with 256 bits of entropy.

**Verified how.** Read `scripts/install.sh:60-80` directly.

**Evidence:** verified by the reviewer against the cited lines. The worker's original pointer
(`evidence/14-git-history-secrets-scan.txt`) does not demonstrate this finding.

**Recommendation.** `umask 077` before the heredoc, or `touch` + `chmod 0600` before writing.

---

## Checked and found clean

Each item below was examined and no exploitable issue was found. Listed so the review is
falsifiable rather than a bare list of hits.

| Area | Result | How verified |
| --- | --- | --- |
| Shell / command injection | No shell is ever used. `asyncio.create_subprocess_exec(*argv)` only; no `shell=True`, no `os.system`. | Read `runner.py:230-238`; grepped for shell spawn patterns |
| SSRF redirect guard | Genuinely sound. `follow_redirects=False`, every hop re-validated *before* the request is sent, relative `Location` resolved via `urljoin`, 5-hop cap, `http`/`https` only. | Read `attachments.py:113-139` and `13-57` directly |
| Attachment size limits | Enforced per 8 KiB chunk during streaming, not from `Content-Length` — a lying server is aborted mid-stream. | Read `attachments.py:154-167` |
| Filename / path traversal | `../../etc/passwd` → `passwd`; `/etc/shadow` → `shadow`; null bytes stripped; final `abspath` containment check raises on escape. | Real hostile inputs run against `sanitize_filename` / `get_unique_filepath` (`evidence/12`) |
| SQL injection | All queries parameterised with `?`. The two f-string sites build `WHERE` fragments from internal literals, not caller input. | Read `db.py` and `stats.py` query sites |
| Child-process environment | Only `PATH` and `HOME` by default; `API_KEY`/`API_KEYS` do not reach the agent unless explicitly named in `PASSTHROUGH_ENV`. | Read `runner.py:46-64`; `evidence/08` |
| Sandbox fail-closed | With `BWRAP_ENABLED=1`, a missing `bwrap` and `ALLOW_UNCONFINED=0` raises and the job fails without spawning. | Read `runner.py:80-91` and the handler at `:198-206` |
| Timeout cleanup | `start_new_session=True` plus `os.killpg(SIGTERM→SIGKILL)` kills the whole process group. | Read `runner.py:230-238`, `:292-323` |
| Workspace paths | `job_id` is server-side `uuid.uuid4()`; the caller cannot influence the workspace path. Only the current job's directory is bind-mounted. | Read `main.py:307`, `runner.py:164-167`, mount list in `evidence/06` |
| Dashboard XSS | Clean. Every caller-controlled string — including log lines carrying prompt text — uses `innerText`. The only template-literal `innerHTML` sites take a socket-derived IP, an internal enum, and hardcoded taxonomy keys. | Reviewer grepped **every** `innerHTML`/`innerText`/`insertAdjacent`/`document.write` sink in `dashboard.html` |
| API key comparison | `secrets.compare_digest`; the key map is built only from server-side env, never from request data. | Read `security.py:8-38` |
| Secrets in git history | None. All six commits scanned; only `.env.example` with placeholders was ever committed; no `.pem`/`.key`/`.env`. | Reviewer ran an independent credential-pattern scan over `git log --all -p` plus an added-files scan |
| Secrets in logs | Only key *names* are logged, never the key value. | Read `main.py:168,173`; grepped log call sites |
| Secrets on the command line | Supplied via `EnvironmentFile` / environment, not argv; not visible in `ps`. | Read `run.sh:16` and the unit in `README.md` |
| CORS | No CORS middleware registered; same-origin policy applies. | Grepped for `CORSMiddleware` / `add_middleware` |
| `.gitignore` | Covers `.env` (151), `.venv` (153) and `data/` (221); no database or env file is tracked. | Read `.gitignore`; `git ls-files` confirms nothing under `data/` is tracked |

---

## Residual risks and design notes

Not findings, but worth recording.

1. **Loopback is deliberately trusted, and the Cloudflare ingress terminates on loopback.**
   `doc/networking.md:9,13,17` documents this. Public traffic arrives at `127.0.0.1:8090`, which is
   in `TRUSTED_NETWORKS`, and the sole control forcing those requests to authenticate is the
   `has_cf_header` presence check at `main.py:152-157`. That check does hold for the documented
   ingress — Cloudflare injects and overwrites `CF-*` headers on HTTP traffic — so this is not a
   finding. But the entire internet-facing authentication boundary rests on one header-presence
   test, and any future ingress that does not inject those headers (a plain reverse proxy, an SSH
   tunnel, a local SSRF pivot) silently converts to unauthenticated access. Consider making the
   bypass opt-in per interface rather than inferred from a third party's headers.
2. **DNS-rebinding TOCTOU in the SSRF guard** — see [Unconfirmed](#unconfirmed).
3. **`BWRAP_ENABLED=0` silently overrides `ALLOW_UNCONFINED=0`.** `runner.py:82-83` returns the
   unwrapped command before the fail-closed check at `:86-90` is ever reached. Both are
   operator-set (trusted) values, so this is not an attacker path, but the README's "fail closed by
   default" guarantee is not true under that combination, and nothing warns the operator.
4. **`GET /healthz` is unauthenticated** and returns the version, queue depth, absolute paths of
   the agent binaries, and the confinement mode. Minor reconnaissance value; notable mainly because
   it tells an attacker whether confinement is enforced before they try anything.
5. **A key literally named `bypass` inherits admin visibility (introduced by the F8 fix).** The
   scoping added for F8 treats the identity string `"bypass"` as "see everything", and
   `parse_api_keys` places no restriction on key names — so `API_KEYS="bypass:somesecret"` creates
   a named, remotely-usable key that is exempt from job scoping. This is operator-configured and
   therefore not an attacker path, but it is a footgun worth closing with a one-line guard that
   rejects (or renames) a configured key called `bypass`. Verified by calling the real `get_job`
   with `api_key_name="bypass"` and observing unscoped results.
6. **`dashboard.html:376` builds a table row with `innerHTML` from database values.** Safe today —
   the fields are a socket-derived IP and an internal enum — but it is the one HTML sink fed from
   stored data, so it is where a future field would become stored XSS on an unauthenticated page.

## Unconfirmed

* **DNS-rebinding TOCTOU in the SSRF guard.** `validate_url_ssrf` resolves the hostname with
  `socket.getaddrinfo` and checks the resulting IPs (`attachments.py:42-57`); `httpx` then performs
  its own independent resolution when the request is sent (`:128-129`). An attacker controlling a
  low-TTL authoritative DNS server could in principle return a public address to the check and a
  loopback or private address to the fetch. Whether this is reachable in practice depends on
  resolver caching behaviour on the host and was not demonstrated either way. Worth a bounded test
  before relying on the guard against internal-network targets; the robust fix is to resolve once
  and connect to the validated IP with the original `Host` header.

## Notes on the review process

* Four findings raised by the worker agent were rejected or amended during verification: one
  (`F3`, a network-trust claim) was argued from a hypothetical redeployment rather than the shipped
  one and was closed; the argument-injection severity was revised down from a working hypothesis of
  High to Low once the CLI parser was actually probed; F5's scope was widened after the reviewer
  found the agy OAuth token was also exposed; F8's severity detail was corrected upward.
* Three findings arrived pointing at evidence files that did not demonstrate them; those were
  re-verified from source by the reviewer, and the discrepancy is noted inline above.
* One instruction breach: the worker issued `curl http://127.0.0.1:8090/dashboard` against the
  running service during round 2, which the review brief prohibited. The request was a read-only
  `GET` and caused no change, but it confirms the service was live on the host during the review.
