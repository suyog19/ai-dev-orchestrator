# Phase 11 — Control & Security Hardening Layer

**Completed:** 2026-04-24  
**Branch:** dev → main  
**Iterations:** 13 (0–12)

---

## Objective

Add a production-grade security and control layer to the orchestrator: admin key authentication, Jira webhook token validation, Telegram chat enforcement, GitHub write guard, runtime pause/resume via DB, Redis rate limiting, branch protection auditing, and a full security audit trail.

---

## Iterations Summary

### Iteration 0 — Endpoint Inventory Doc
Created `docs/security/endpoint-inventory.md` — a complete inventory of all HTTP endpoints with their authentication requirements, rate limits, and access notes. Baseline before any security work begins.

### Iteration 1 — `security_events` Table
Added `security_events` table to `init_db()` in `app/database.py`. Added `record_security_event()` and `list_security_events()` functions. All security events (auth failures, webhook rejections, GitHub write blocks) are recorded here as an append-only audit trail.

### Iteration 2 — Admin Key Middleware
Added `admin_key_middleware` in `app/security.py`. Registered via `BaseHTTPMiddleware` in `app/main.py`. Enforces `X-Orchestrator-Admin-Key` header on all `/debug/*` and `/admin/*` paths. Auth failures → 403 + `admin_auth_failed` security event. Successful mutating calls → `admin_auth_success` event.

**Key fix:** Import is `from starlette.middleware.base import BaseHTTPMiddleware` (not `fastapi.middleware.base` which doesn't exist).

### Iteration 3 — `GET /admin/security-events`
Added admin endpoint to query the security events audit log. Supports filtering by `event_type`, `source`, `status`. Returns newest-first list.

### Iteration 4 — `control_flags` Table
Added `control_flags` table seeded from env vars at startup (`ON CONFLICT DO NOTHING` — DB takes precedence over env var after first set). Added `get_control_flag()`, `set_control_flag()`, `get_all_control_flags()`, `is_paused()` in `app/database.py`. `is_paused()` checks DB then falls back to `ORCHESTRATOR_PAUSED` env var.

### Iteration 5 — Pause/Resume Admin Endpoints
Added `GET /admin/control-status`, `POST /admin/pause`, `POST /admin/resume` to `app/main.py`. Pause/resume modifies the `paused` control flag in DB — no redeploy required.

### Iteration 6 — Pause Guard in Webhook Handlers
Wired `is_paused()` check into `app/webhooks.py`:
- Jira webhook: if paused, records `automation_paused_jira_blocked` event and returns `{"received": True, "processed": False, "reason": "orchestrator_paused"}` without dispatching.
- Telegram webhook: if paused, sends notification and rejects command without executing.

### Iteration 7 — Jira Webhook Token Validation
Added `JIRA_WEBHOOK_SECRET` query param check to `POST /webhooks/jira`. If configured, requests without a matching `?token=` value return 401 and record a `webhook_rejected` security event. Added `JIRA_WEBHOOK_SECRET` to `.env.example`.

### Iteration 8 — Telegram Chat ID Enforcement
Added wrong-chat-id detection to `POST /webhooks/telegram`. Requests from unexpected chat IDs return `{"ok": True}` (Telegram expects 200) and record a `telegram_rejected` security event. Added run_id sanity check (must be 1–10,000,000) with a `telegram_rejected` event on malformed values.

### Iteration 9 — GitHub Write Guard
Added `ensure_github_writes_allowed(action, repo_slug, run_id)` in `app/security.py`. Raises `RuntimeError` when orchestrator is paused, `ALLOW_GITHUB_WRITES=false`, or `ALLOW_AUTO_MERGE=false` (for merge_pr). Wired into `app/workflows.py` before push, PR creation, and merge. Write blocks recorded as `github_write_blocked` security events. Added `docs/security/token-permissions.md`.

### Iteration 10 — Branch Protection Audit
Added `get_branch_protection(repo_slug, branch)` in `app/github_api.py` — queries GitHub API for branch protection rules and returns structured warnings for missing protections. Added `GET /admin/github/branch-protection` endpoint. Added `docs/runbooks/orchestrator-ops.md`.

### Iteration 11 — Rate Limiting
Added Redis sliding-window rate limiting in `app/security.py`:
- `/webhooks/jira`: 30 req/min (global) → 429 on exceeded
- `/webhooks/telegram`: 10 req/min per chat_id → 200/ok (Telegram requires 200)
- Admin mutating calls: 20 req/min per client IP → 429 on exceeded

Wired into `admin_key_middleware` and both webhook handlers. Fails open on Redis errors.

### Iteration 12 — E2E Security Validation
Full end-to-end security validation covering 9 scenarios: unauthenticated debug access, authenticated debug access, admin endpoint auth, Jira token rejection, Jira token acceptance, Telegram chat rejection, pause mode blocking, security events listing, and branch protection auditing. **24/24 checks passed.**

---

## New Files

| File | Purpose |
|---|---|
| `app/security.py` | Admin key middleware, GitHub write guard, rate limiting |
| `docs/security/endpoint-inventory.md` | All endpoints with auth requirements |
| `docs/security/token-permissions.md` | Minimum permission scopes for all secrets |
| `docs/runbooks/orchestrator-ops.md` | Operational runbook (pause, rotate, recover) |

---

## Modified Files

| File | Changes |
|---|---|
| `app/database.py` | `security_events` table, `control_flags` table, 7 new functions |
| `app/main.py` | `BaseHTTPMiddleware` registration, 5 new admin endpoints |
| `app/webhooks.py` | Secret validation, pause checks, run_id sanity, rate limiting |
| `app/workflows.py` | `ensure_github_writes_allowed()` wired before push/PR/merge |
| `app/github_api.py` | `get_branch_protection()` |
| `.env.example` | 7 new security env vars |
| `CLAUDE.md` | Security Layer section, new env vars, new endpoints |

---

## Security Architecture

```
HTTP Request
    │
    ▼
admin_key_middleware (BaseHTTPMiddleware)
    ├── is_admin_protected? → check X-Orchestrator-Admin-Key
    │       no key / wrong key → 403 + record admin_auth_failed
    │       valid key + mutating → check_rate_limit → 429 if exceeded
    │       valid key + mutating → record admin_auth_success
    │
    ▼
Route Handler
    │
POST /webhooks/jira
    ├── JIRA_WEBHOOK_SECRET check → 401 + record webhook_rejected
    ├── check_rate_limit → 429 if exceeded
    ├── is_paused() → return processed=False + record blocked event
    └── dispatch()

POST /webhooks/telegram
    ├── check_rate_limit → return ok:True if exceeded
    ├── chat_id check → return ok:True + record telegram_rejected
    ├── run_id sanity → return ok:True + record telegram_rejected
    ├── is_paused() → return ok:True + record blocked event
    └── execute command

story_implementation workflow
    ├── push → ensure_github_writes_allowed("push") or RuntimeError
    ├── create_pr → ensure_github_writes_allowed("create_pr") or RuntimeError
    └── merge_pr → ensure_github_writes_allowed("merge_pr") or RuntimeError
```

---

## Validation Results

| Iteration | Checks | Result |
|---|---|---|
| 0 | doc exists, sections present | PASS |
| 1–3 | security_events table, endpoint | PASS |
| 4–5 | control_flags, pause/resume | PASS |
| 6 | pause guard in webhooks | PASS |
| 7 | Jira token validation | PASS |
| 8 | Telegram chat enforcement | PASS |
| 9 (Iter 8-9) | branch protection + token-permissions doc | PASS |
| 10 | GitHub write guard in workflows | PASS |
| 11 | Rate limiting — 8/8 checks | PASS |
| 12 | E2E security — 24/24 checks | PASS |
