# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the App

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Verify with: `curl http://localhost:8000/healthz` → `{"status": "ok"}`

No test suite, linting config, or Makefile exists yet — these are added in later iterations.

## Architecture

This is a Python/FastAPI orchestration service that receives Jira webhook events, persists them in PostgreSQL, dispatches stub workflows via a Redis-backed queue, and notifies the user via Telegram.

**Planned components (Phase 1):**
- `app/main.py` — FastAPI core; logging; all endpoints live here until further modularization
- PostgreSQL — `workflow_events` and `workflow_runs` tables (not yet integrated)
- Redis — workflow job queue with configurable concurrency limit (default 2 parallel workflows)
- Worker process — background executor for stub workflows
- Telegram bot — notifications on startup, webhook receipt, workflow start/complete

**Event flow (target):**
```
Jira Webhook → POST /webhooks/jira → DB → Dispatcher → Redis Queue → Worker → Telegram
```

**Workflow triggers recognized (Phase 1):**
- Epic/Feature/Story → Final status in Jira
- Only `story_implementation` workflow is mapped (stub: log, sleep, complete, notify)

## Data Model

**`workflow_events`:** `id`, `source`, `event_type`, `payload_json`, `status`, `created_at`

**`workflow_runs`:** `id`, `workflow_type`, `status` (RECEIVED | QUEUED | RUNNING | COMPLETED), `related_event_id`, `created_at`, `updated_at`

## API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/healthz` | Health check (implemented) |
| GET | `/debug/send-telegram` | Manual Telegram test (planned) |
| POST | `/webhooks/jira` | Jira event receiver (planned) |

## Telegram Message Format

```
[Orchestrator]
Event: <type>
Status: <status>
Details: <short summary>
```

## Iteration Order (STRICT — do not skip steps)

1. Project skeleton + `/healthz` ✅
2. Docker + Docker Compose
3. VM setup instructions
4. GitHub self-hosted runner
5. Dev auto-deploy workflow (`dev` branch)
6. PostgreSQL integration
7. Telegram bot integration
8. Jira webhook endpoint
9. Event persistence
10. Workflow dispatcher (stub)
11. Redis queue + worker
12. Stub workflow execution

## Working Style

Implement one iteration at a time. After each: confirm it runs, provide test steps, wait for user confirmation before moving to the next step.

When a decision affects architecture, multiple valid approaches exist, credentials are needed, or external services require setup — ask before proceeding, using this format:

```
QUESTION: <clear question>
OPTIONS:
1. Option A
2. Option B
RECOMMENDATION: <recommendation + why>
```

## Non-Goals (Phase 1)

Do not implement: real Claude-driven code generation, GitHub repo modifications, PR creation/review, full Jira automation, Epic/Feature breakdown workflows, retry/failure recovery, UI/dashboard, production security hardening.
