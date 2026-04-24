I’d target **Phase 11: Control & Security Hardening Layer**.

Reason: after Phase 10, the system has multiple AI gates and a unified release decision. That is powerful, but now the main risk is **who/what can trigger, approve, inspect, or modify the system**. Phase 10 already centralized release decisions and added Architecture Agent + Release Gate observability, so security/control is now the highest-priority next layer. 

---

# PHASE 11 EXECUTION GUIDE — Control & Security Hardening Layer

## 1. Objective

Phase 11 hardens the AI Dev Orchestrator so it is safer to run continuously.

The goal is to protect:

* webhook endpoints
* debug/admin endpoints
* Telegram approval commands
* Jira/GitHub write actions
* release/merge decisions
* emergency shutdown controls
* operational recovery procedures

This phase is not about adding more AI intelligence.

It is about making the powerful system you already built safer.

---

## 2. Why Phase 11 Now

The system can now:

```text
Epic → Stories
Story → code
code → tests
tests → PR
PR → Reviewer Agent
PR → Test Quality Agent
PR → Architecture Agent
all gates → Unified Release Gate
Release Gate → merge
```

That means the orchestrator has real authority over:

* Jira issue creation
* GitHub PR creation
* GitHub merge
* Telegram approval actions
* memory and feedback state
* debug/admin operations

So before adding more autonomy, harden control surfaces.

---

# 3. Phase 11 Scope

## In scope

1. Admin/debug endpoint authentication
2. Webhook authenticity validation
3. Telegram command hardening
4. Emergency pause / kill switch
5. Role-based action policy
6. Token/secret permission audit support
7. Branch protection audit support
8. Rate limiting / dedup strengthening
9. Security event logging
10. Operational runbook endpoints / docs

## Out of scope

Do NOT build in Phase 11:

* full user management UI
* OAuth login
* enterprise RBAC
* secrets manager migration
* external SIEM integration
* full admin dashboard
* release/deploy automation for target apps
* new AI agents

---

# 4. Key Design Principles

## 4.1 Secure by default

Any endpoint that can reveal, modify, approve, retry, regenerate, merge, or mutate state must require authentication.

## 4.2 Fail closed

If auth verification fails, reject the request.

## 4.3 Preserve Jira/Telegram compatibility

Webhook and Telegram integrations must still work, but with explicit verification.

## 4.4 Emergency stop must be simple

You should be able to stop all automation without redeploying code.

## 4.5 Audit everything important

Security-relevant actions must be recorded.

---

# 5. New Concepts

## 5.1 Admin API Key

A shared admin token for debug/admin endpoints.

Header:

```http
X-Orchestrator-Admin-Key: <secret>
```

Used for:

```text
/debug/*
/admin/*
manual approval endpoints
memory mutation
mapping mutation
recompute endpoints
```

## 5.2 Environment kill switch

Env variables:

```text
ORCHESTRATOR_PAUSED=false
ALLOW_JIRA_WEBHOOKS=true
ALLOW_TELEGRAM_COMMANDS=true
ALLOW_GITHUB_WRITES=true
ALLOW_AUTO_MERGE=true
```

## 5.3 Security events

Record major security/control actions:

```text
admin_auth_failed
admin_auth_success
webhook_rejected
telegram_rejected
automation_paused
automation_resumed
github_write_blocked
auto_merge_blocked_by_policy
```

---

# 6. Data Model Changes

## 6.1 New table: `security_events`

Suggested schema:

```sql
id SERIAL PRIMARY KEY,
event_type VARCHAR(100) NOT NULL,
source VARCHAR(100) NULL,
actor VARCHAR(200) NULL,
endpoint VARCHAR(300) NULL,
method VARCHAR(20) NULL,
status VARCHAR(50) NULL,
details_json TEXT NULL,
created_at TIMESTAMP DEFAULT NOW()
```

Examples:

```text
event_type=admin_auth_failed
source=http
actor=unknown
endpoint=/debug/memory
status=REJECTED
```

```text
event_type=telegram_command_rejected
source=telegram
actor=<chat_id>
status=REJECTED
```

## 6.2 Optional table: `control_flags`

If you want runtime DB-controlled flags instead of only env vars:

```sql
key VARCHAR(100) PRIMARY KEY,
value VARCHAR(100) NOT NULL,
updated_at TIMESTAMP DEFAULT NOW()
```

Initial recommendation:

Use env vars first. Add DB flags only if needed.

---

# 7. Configuration Additions

Add to environment:

```text
ADMIN_API_KEY=<strong-random-secret>

ORCHESTRATOR_PAUSED=false
ALLOW_JIRA_WEBHOOKS=true
ALLOW_TELEGRAM_COMMANDS=true
ALLOW_GITHUB_WRITES=true
ALLOW_AUTO_MERGE=true

ENABLE_DEBUG_ENDPOINTS=true
```

Optional:

```text
ALLOWED_ADMIN_IPS=
```

Do not log secret values.

---

# 8. Iteration Plan

## Iteration 0 — Security baseline and inventory

### Goal

Document all exposed endpoints and classify them.

### Tasks

Create endpoint inventory:

| Category          | Examples                                | Auth required               |
| ----------------- | --------------------------------------- | --------------------------- |
| Health            | `/healthz`                              | No                          |
| External webhooks | `/webhooks/jira`, `/webhooks/telegram`  | Signature/source validation |
| Debug read-only   | `/debug/workflow-runs`                  | Yes                         |
| Debug mutating    | `/debug/memory`, `/debug/repo-mappings` | Yes                         |
| Admin control     | `/admin/pause`, `/admin/resume`         | Yes                         |

### Acceptance criteria

* endpoint inventory committed to docs
* each endpoint classified
* no code behavior changed yet

Then STOP.

---

## Iteration 1 — Admin API key middleware

### Goal

Protect debug/admin endpoints.

### Tasks

* Add middleware/dependency to check `X-Orchestrator-Admin-Key`
* Protect all `/debug/*` endpoints except intentionally public ones, if any
* Return `401` or `403` on missing/invalid key
* Never log the key
* Add helper:

```python
require_admin(request)
```

### Acceptance criteria

* `/healthz` works without key
* `/debug/workflow-runs` fails without key
* `/debug/workflow-runs` works with correct key
* failed attempts are logged

Then STOP.

---

## Iteration 2 — Security event logging

### Goal

Persist important security actions.

### Tasks

* Add `security_events` table
* Add helper:

```python
record_security_event(event_type, source, actor, endpoint, method, status, details)
```

* Log:

  * failed admin auth
  * successful admin auth for mutating endpoints
  * rejected webhook
  * rejected Telegram command

### Acceptance criteria

* failed admin request creates `security_events` row
* successful mutating admin request creates row
* events inspectable via DB initially

Then STOP.

---

## Iteration 3 — Security events inspection API

### Goal

Make security audit visible.

### Add endpoint:

```text
GET /admin/security-events?limit=N
```

Protected by admin key.

Filters:

```text
event_type
source
status
```

### Acceptance criteria

* security events visible over HTTP with admin key
* endpoint blocked without admin key
* JSON output is readable

Then STOP.

---

## Iteration 4 — Emergency pause switch

### Goal

Allow safe shutdown of automation without redeploy.

### Tasks

Add pause checks before:

* dispatching Jira workflow
* processing Telegram approval commands
* GitHub writes
* auto-merge

Initial env-based behavior:

```text
ORCHESTRATOR_PAUSED=true
```

When paused:

* `/healthz` still works
* webhooks return 200 but do not enqueue workflows
* Telegram commands are acknowledged but not executed
* no GitHub writes
* no auto-merges

### Add admin endpoints

```text
GET /admin/control-status
POST /admin/pause
POST /admin/resume
```

If using env vars only, pause/resume can update DB `control_flags`.

Recommended:

* implement DB-backed runtime control flags
* env var provides startup default

### Acceptance criteria

* pause blocks new Jira workflows
* pause blocks Telegram approve/reject/regenerate
* existing running workflows either continue or are allowed to finish; document chosen behavior
* resume allows new workflows again

Recommended initial behavior:

```text
pause blocks new work only; does not kill running jobs
```

Then STOP.

---

## Iteration 5 — Telegram command hardening

### Goal

Make Telegram command handling safer.

### Tasks

Enhance `/webhooks/telegram`:

* verify `chat_id == TELEGRAM_CHAT_ID`
* reject commands from unknown chats
* reject unknown commands
* reject malformed run IDs
* reject approval commands when paused
* add security event for rejected command
* add optional command prefix:

```text
APPROVE <run_id>
REJECT <run_id>
REGENERATE <run_id>
```

Keep existing commands backward compatible.

### Acceptance criteria

* correct chat works
* wrong chat silently ignored or rejected with security event
* malformed command does not alter DB
* paused mode blocks command execution

Then STOP.

---

## Iteration 6 — Jira webhook validation

### Goal

Reduce risk of arbitrary POSTs triggering workflows.

### Options

Jira Cloud does not always give simple signed webhooks by default. Use one or more:

1. Secret token in webhook URL:

```text
/webhooks/jira?token=<JIRA_WEBHOOK_SECRET>
```

2. Custom header if configured through automation/web request
3. IP allowlist if feasible
4. Shared secret in payload if using Jira Automation web request

Recommended initial implementation:

```text
JIRA_WEBHOOK_SECRET
```

Validate:

```text
query param token == JIRA_WEBHOOK_SECRET
```

### Behavior

Invalid token:

* return 401/403
* no workflow event inserted
* record security event

### Acceptance criteria

* valid Jira webhook accepted
* missing/invalid token rejected
* manual curl without token cannot trigger workflow

Then STOP.

---

## Iteration 7 — GitHub write guard

### Goal

Centralize protection before any GitHub write action.

GitHub writes include:

* push branch
* create PR
* post PR comment
* add label
* merge PR

### Tasks

Add helper:

```python
ensure_github_writes_allowed(action, repo_slug, run_id)
```

Checks:

```text
ORCHESTRATOR_PAUSED
ALLOW_GITHUB_WRITES
specific action allowed
```

For auto-merge also check:

```text
ALLOW_AUTO_MERGE
```

### Acceptance criteria

* when `ALLOW_GITHUB_WRITES=false`, branch push/PR creation is blocked safely
* when `ALLOW_AUTO_MERGE=false`, PR can be created but merge is skipped
* security event recorded

Then STOP.

---

## Iteration 8 — Branch protection audit helper

### Goal

Verify repository branch protection matches expectations.

### Add endpoint:

```text
GET /admin/github/branch-protection?repo_slug=owner/repo&branch=main
```

Protected by admin key.

It should fetch/check:

* whether branch protection exists
* required PR reviews
* required status checks
* direct push restrictions if available
* whether force push is disabled
* whether deletion is disabled

### Output:

```json
{
  "repo_slug": "...",
  "branch": "main",
  "protected": true,
  "required_reviews": true,
  "required_status_checks": [...],
  "warnings": []
}
```

### Acceptance criteria

* endpoint works for sandbox repo
* missing protection produces warnings
* no mutation yet

Then STOP.

---

## Iteration 9 — Token permission audit document

### Goal

Document least-privilege expectations.

### Tasks

Create `docs/security/token-permissions.md`.

Include:

## GitHub token

Minimum required:

* contents read/write
* pull requests read/write
* issues read/write for comments/labels
* metadata read

Avoid if possible:

* admin repo
* org-wide access

## Jira token

Minimum required:

* browse project
* create issues
* read issues
* transition issues if used

## Telegram token

* bot token stored only in env
* chat ID locked

## Anthropic key

* API call only
* never logged

### Acceptance criteria

* document committed
* no secrets included
* current env vars listed by name only

Then STOP.

---

## Iteration 10 — Admin runbook

### Goal

Create operational runbook.

Create:

```text
docs/runbooks/orchestrator-ops.md
```

Include:

* how to pause automation
* how to resume automation
* how to rotate secrets
* how to disable Jira webhook
* how to stop auto-merge only
* how to recover stale runs
* how to inspect security events
* how to handle bad auto-merge
* how to disable Telegram commands
* how to verify branch protection
* how to redeploy dev/prod

### Acceptance criteria

* runbook is specific, not generic
* includes actual endpoint names and env vars
* usable during incident

Then STOP.

---

## Iteration 11 — Rate limiting and duplicate abuse protection

### Goal

Prevent accidental or malicious event floods.

### Tasks

Add simple in-memory or Redis-backed rate limits for:

```text
/webhooks/jira
/webhooks/telegram
/debug/admin mutating endpoints
```

Suggested simple Redis key:

```text
rate:<source>:<identifier>:<window>
```

Initial limits:

```text
Jira webhook: 30/min
Telegram commands: 10/min per chat
Admin mutating endpoints: 20/min
```

### Acceptance criteria

* normal flows unaffected
* burst requests get 429 or safe ignored response
* security event recorded

Then STOP.

---

## Iteration 12 — End-to-end security validation

### Required scenarios

#### Scenario A — Debug endpoint without key

Expected:

```text
403
security event recorded
```

#### Scenario B — Debug endpoint with key

Expected:

```text
200
```

#### Scenario C — Jira webhook invalid token

Expected:

```text
401/403
no workflow created
security event recorded
```

#### Scenario D — Jira webhook valid token

Expected:

```text
workflow accepted normally
```

#### Scenario E — Telegram wrong chat

Expected:

```text
ignored/rejected
no DB mutation
security event recorded
```

#### Scenario F — Pause mode

Expected:

```text
new Jira workflows blocked
Telegram approvals blocked
health still works
```

#### Scenario G — GitHub writes disabled

Expected:

```text
no push/PR/merge
workflow ends safely or fails clearly according to chosen policy
```

#### Scenario H — Auto-merge disabled

Expected:

```text
PR created
agents run
release approved
merge skipped due to policy
```

#### Scenario I — Rate limit exceeded

Expected:

```text
429 or safe ignore
security event recorded
```

### Acceptance criteria

* all scenarios pass
* no existing Phase 10 happy path broken
* security events provide enough audit detail
* runbook reflects final behavior

Then STOP.

---

# 9. Final Instruction to Claude

Build Phase 11 as a **control and security hardening layer**, not as another feature expansion.

The system is now powerful enough to write, review, test, and merge code.

So the key question is:

> Can we safely control who triggers it, who approves it, what it can write, and how we stop it?

Optimize for:

* secure defaults
* explicit authorization
* emergency controls
* auditability
* least privilege
* operational clarity

Do not optimize for:

* fancy UI
* enterprise auth
* broad IAM integration
* more AI calls
* more agents

This phase is about making the system safer before making it smarter.
