# Phase 14 Summary — Admin Dashboard / Operations Console

## Overview

Phase 14 built a lightweight admin dashboard for the AI Dev Orchestrator.

The dashboard allows operators to inspect system state, manage clarifications, trigger safe admin actions, and control automation — all from a browser, without needing `curl`.

**Phase completed:** 2026-04-24  
**Branch:** `dev` → merged to `main`  
**Iterations:** 13 (0–12)

---

## What Was Built

### New Files

| File | Purpose |
|---|---|
| `app/ui_auth.py` | Cookie-based session auth (itsdangerous signed tokens, CSRF helpers) |
| `app/ui.py` | FastAPI router at `/admin/ui/*` — all dashboard routes |
| `app/templates/admin/base.html` | Sidebar layout, env badge, PAUSED banner |
| `app/templates/admin/login.html` | Admin key login form |
| `app/templates/admin/overview.html` | System overview with stat cards |
| `app/templates/admin/runs.html` | Filterable workflow runs list |
| `app/templates/admin/run_detail.html` | Full run lifecycle view |
| `app/templates/admin/planning.html` | Planning runs list |
| `app/templates/admin/planning_detail.html` | Planning run detail with proposed stories |
| `app/templates/admin/clarifications.html` | Clarifications with answer/cancel/resend |
| `app/templates/admin/agents.html` | All three agent verdict types |
| `app/templates/admin/github.html` | GitHub statuses + branch protection check |
| `app/templates/admin/memory.html` | Memory snapshots + feedback events |
| `app/templates/admin/security.html` | Security events audit log |
| `app/templates/admin/control.html` | Pause/resume + control flags |
| `app/templates/admin/error.html` | Generic error page |
| `app/static/admin/admin.css` | Full design system (layout, sidebar, cards, tables, forms) |

### Modified Files

| File | Changes |
|---|---|
| `app/main.py` | Mount `/static` (StaticFiles), include `ui_router` |
| `app/security.py` | Exempt `/admin/ui/*` from header-key middleware (cookie auth used instead) |
| `app/database.py` | 7 new functions: `list_workflow_runs_for_ui`, `get_workflow_run_detail`, `list_memory_snapshots`, `list_feedback_events`, `get_overview_stats`, `list_memory_snapshots`, `list_feedback_events` |

---

## Dashboard Routes

| Path | Page |
|---|---|
| `/admin/ui/login` | Admin key login form |
| `/admin/ui/logout` | Clear session, redirect to login |
| `/admin/ui` → redirect | Redirect to overview |
| `/admin/ui/overview` | System status at a glance |
| `/admin/ui/runs` | Workflow runs list (filterable) |
| `/admin/ui/runs/{run_id}` | Full run detail with all agents and statuses |
| `/admin/ui/planning` | Planning runs list |
| `/admin/ui/planning/{run_id}` | Planning run detail with proposed stories |
| `/admin/ui/clarifications` | Clarifications (PENDING/ANSWERED/CANCELLED/EXPIRED tabs) |
| `/admin/ui/agents` | Agent reviews (reviewer/test_quality/architecture) |
| `/admin/ui/github` | GitHub statuses + branch protection check |
| `/admin/ui/memory` | Memory snapshots + feedback events + manual note form |
| `/admin/ui/security` | Security events audit log |
| `/admin/ui/control` | Pause/resume + all control flags |

---

## Iteration Log

| Iteration | What | Validation |
|---|---|---|
| 0 | Auth shell: login/logout, cookie, CSRF, security.py exemption | 17/17 passed |
| 1 | Overview page: stat cards, failed/blocked/running runs, security events | 18/18 passed |
| 2 | Workflow runs list with filters (status, type, issue_key, release, limit) | — (combined with 3) |
| 3 | Run detail: full lifecycle, all 3 agents, GitHub statuses, clarification | 22/22 passed |
| 4 | Planning list + detail with proposed stories | — (combined with 5) |
| 5 | Clarifications: answer/cancel/resend with CSRF + confirmation | 13/13 passed |
| 6 | Agent reviews (reviewer/test_quality/architecture) with filters | — (combined 6-10) |
| 7 | GitHub statuses lookup + republish + branch protection check | — (combined 6-10) |
| 8 | Memory snapshots, feedback events, manual note form | — (combined 6-10) |
| 9 | Security events with filters | — (combined 6-10) |
| 10 | Control: pause/resume with audit events + env flag display | 31/31 passed |
| 11 | Hardening: `is_paused()` Jinja2 global, `fmtts` filter, PAUSED banner | — |
| 12 | E2E: scenarios A–H covering all major paths | 37/37 passed |

---

## E2E Validation Results (Iteration 12)

### Scenario A — Login/logout
- Unauthenticated user correctly redirected to login
- Wrong admin key returns 401 with error message
- Correct admin key sets signed session cookie and redirects
- Logout clears cookie
- **37/37 tests passed**

### Scenario B — Overview
- Loads stat cards, run status breakdown, failed/blocked/running tables
- Shows real data: 70 completed, 41 failed, 1 pending clarification

### Scenario C — Run detail
- Run #118 showed all sections: Reviewer Agent, Test Quality Agent, Architecture Agent
- Run ID visible in heading, all structural elements present

### Scenario D — Clarification management
- PENDING clarification visible with answer/resend/cancel forms
- CSRF field present, ANSWERED tab visible

### Scenario E — GitHub republish
- GitHub statuses page renders correctly
- Republish form visible when run has statuses

### Scenario F — Branch protection validation
- Branch protection check form visible on GitHub page
- repo_slug and branch inputs present

### Scenario G — Pause/resume
- Pause action changes `orchestrator_paused` to `true` in DB
- Resume restores to `false`
- Security events recorded for both actions
- CSRF validation confirmed

### Scenario H — Security events
- Security page loads with event type filter
- Recent failed auth events visible
- Existing `/debug/*` API unaffected (still 403 without header key)

---

## Security

- All `/admin/ui/*` routes require a valid session cookie (`orchestrator_admin_session`)
- Cookie is signed with SHA256-keyed `URLSafeTimedSerializer` from `itsdangerous`
- Session TTL: 8 hours
- CSRF protection on every POST form (token derived from session)
- Confirmation dialogs (`onsubmit=confirm()`) on all destructive actions
- Admin key is never echoed in HTML responses
- `/admin/ui/*` exempted from header middleware; existing header auth for `/debug/*` unchanged
- Pause/resume recorded as `security_events` in DB

---

## Auth Design

```
User enters admin key in login form
→ check_admin_key() validates against ADMIN_API_KEY env
→ create_session_token() → URLSafeTimedSerializer.dumps("admin")
→ Cookie set: orchestrator_admin_session (httponly, samesite=lax, 8h TTL)

Each protected route:
→ require_admin_ui(request) → verify_session_token(cookie)
→ If invalid: raise _LoginRedirect → 302 to /admin/ui/login?next=<url>

CSRF:
→ csrf_token(session_token) = SHA256("csrf:{session_token}")[:32]
→ Embedded in each form as hidden input
→ verify_csrf() on every POST
```

---

## New DB Functions

| Function | Purpose |
|---|---|
| `list_workflow_runs_for_ui(status, workflow_type, issue_key, release_decision, limit)` | Filterable runs list with all status columns |
| `get_workflow_run_detail(run_id)` | Full run + agent reviews + clarification + GitHub statuses |
| `get_overview_stats()` | Aggregated stats in one round-trip |
| `list_memory_snapshots(scope_type, scope_key)` | Memory snapshot listing |
| `list_feedback_events(source_type, repo_slug, limit)` | Feedback event listing |

---

## Design Decisions

1. **Cookie auth over header auth for UI** — browser usability; `X-Orchestrator-Admin-Key` remains for API clients
2. **`security.py` exemption** — `/admin/ui/*` paths skip the header middleware; cookie auth is handled entirely in route handlers
3. **Jinja2 globals for `is_paused()`** — avoids passing pause state in every route context; every page picks it up automatically from the template environment
4. **No separate frontend** — server-rendered Jinja2; zero build pipeline; single CSS file
5. **Reuse of existing service functions** — clarification answer/cancel/resend calls the same DB + workflow functions as the `/debug/*` API endpoints
6. **CSRF derived from session** — `SHA256("csrf:{token}")[:32]` — bound to session without extra storage

---

## Next Steps (out of scope for Phase 14)

- WebSocket or polling for live run status updates
- PR/Jira deep links with in-page preview
- Pagination (currently limit param used instead)
- Mobile-responsive layout
