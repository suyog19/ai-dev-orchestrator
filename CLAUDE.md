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

## Environment Model (Phase 3+)

Two separate VMs. Never share a VM between dev and prod.

| Environment | VM IP | Branch | Runner label | Domain |
|---|---|---|---|---|
| Dev | `65.2.140.4` | `dev` | `self-hosted-dev` | `dev.orchestrator.suyogjoshi.com` |
| Prod | `13.234.33.241` | `main` | `self-hosted-prod` | `orchestrator.suyogjoshi.com` |

## Two-File `.env` Rule — CRITICAL

There are two `.env` files on each VM. They serve different purposes and must both be kept in sync.

**`/home/ubuntu/.env.orchestrator`** — persistent secrets file on the VM. This is the source of truth. It survives deploys and is never overwritten by GitHub Actions.

**`<project_dir>/.env`** — the file Docker containers actually read via `env_file:` in `docker-compose.yml`. This is overwritten on every deploy by the step: `cp /home/ubuntu/.env.orchestrator .env`.

### Rules

1. To update a secret permanently: edit `/home/ubuntu/.env.orchestrator`, then redeploy (push to branch or run manually).
2. If you update `.env.orchestrator` manually mid-iteration and need containers to pick it up immediately — do NOT just run `docker compose up -d`. You must run:

```bash
cp /home/ubuntu/.env.orchestrator .env
docker compose up -d --force-recreate
```

3. `docker compose up -d` without `--force-recreate` does NOT reload environment variables into already-running containers.
4. Always verify the key landed in the container after a change:

```bash
docker exec <container-name> env | grep <VAR_NAME>
```

## Phase 5 Configuration

### Environment naming (Phase 5+)

Each VM's `/home/ubuntu/.env.orchestrator` must include `ENV_NAME`:

- Dev VM: `ENV_NAME=DEV`
- Prod VM: `ENV_NAME=PROD`

This value is prepended to every Telegram message as `[DEV]` or `[PROD]`.

### Sandbox repo policy (Phase 5+)

**Policy: Option A — Controlled merge-forward.**

The sandbox repo (`suyog19/sandbox-fastapi-app`) is long-lived. Approved PRs are merged
between sessions so the codebase gradually improves. Auto-merge is enabled for this repo
(`auto_merge_enabled: true` in `config/seed_mappings.json`) and will be enforced by code
logic starting in Phase 5 Iteration 6. Until that iteration, the flag exists in the DB
but has no runtime effect.

Auto-merge conditions (enforced from Iteration 6 onward):
- tests passed (`test_status = PASSED`)
- PR created successfully
- `auto_merge_enabled = true` on the repo mapping
- `files_changed_count` within configured threshold
- no skipped or failed tests

### Trigger definitions (Phase 5+)

| Field | Value |
|---|---|
| Jira trigger status | `Ready for Dev` (case-insensitive) |
| Supported issue type | `Story` |
| Test-enabled repo | `suyog19/sandbox-fastapi-app` |
| Test command | `pytest -q` |
| Dependency install | `pip install -r requirements.txt` (run in workspace before pytest) |
| Auto-merge enabled | `suyog19/sandbox-fastapi-app` only |
| Max fix attempts | 1 (max 2 total coding passes per workflow) |
| Max changed files | 3 |

## Phase 6 Configuration

### Jira hierarchy (Phase 6+)

**Locked decision: Epic → Story (2 levels, no Feature, no Task)**

This uses the default Jira hierarchy. Tasks are not used — Stories are the atomic unit of implementation. Features and Tasks are skipped entirely.

### Planning workflow trigger (Phase 6+)

| Field | Value |
|---|---|
| Jira trigger status | `Ready for Breakdown` (case-insensitive) |
| Supported issue type | `Epic` |
| Workflow type | `epic_breakdown` |
| Max Stories per Epic | 8 |
| Output issue type | `Story` |

### Approval gate (Phase 6+)

The orchestrator proposes a Story breakdown and sends it to Telegram awaiting approval before creating Jira children. Approval commands are sent via Telegram message to the bot:

| Command | Effect |
|---|---|
| `APPROVE <run_id>` | Accept proposed Stories and create them in Jira |
| `REJECT <run_id>` | Discard the proposal; run marked FAILED |
| `REGENERATE <run_id>` | Discard the proposal; re-run planning with a new Claude call |

The run's `approval_status` field tracks the gate: `PENDING` → `APPROVED` / `REJECTED` / `REGENERATE_REQUESTED`.

### Planning Telegram event types (Phase 6+)

| Event type | When sent |
|---|---|
| `epic_breakdown_started` | Planning workflow begins |
| `epic_breakdown_proposed` | Stories proposed, awaiting approval |
| `epic_breakdown_approved` | User approved; Jira creation begins |
| `epic_breakdown_rejected` | User rejected the proposal |
| `epic_breakdown_regenerate` | User requested regeneration |
| `epic_breakdown_complete` | All Jira Stories created successfully |
| `epic_breakdown_failed` | Unrecoverable error |
