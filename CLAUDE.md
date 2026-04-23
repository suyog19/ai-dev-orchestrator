# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the App

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Verify with: `curl http://localhost:8000/healthz` → `{"status": "ok"}`

No test suite or linting config exists for this repo itself (the orchestrator). Tests run against the *target* sandbox repo (`suyog19/sandbox-fastapi-app`) as part of `story_implementation`.

## Architecture

Python/FastAPI orchestration service. Receives Jira webhook events, persists them in PostgreSQL, dispatches workflows via a Redis-backed queue, executes them in a background worker, and notifies via Telegram.

**Key files:**
- `app/main.py` — FastAPI app, all HTTP endpoints
- `app/worker.py` — queue consumer; runs workflows in threads (MAX_WORKERS=2)
- `app/workflows.py` — `story_implementation` and `epic_breakdown` workflow logic
- `app/claude_client.py` — all Claude API calls (summarize, suggest, fix, plan)
- `app/database.py` — all DB access; schema migrations in `init_db()`
- `app/feedback.py` — feedback/memory constants and failure categorisation functions
- `app/webhooks.py` — Jira and Telegram webhook receivers
- `app/jira_client.py` — Jira REST API v3 calls
- `app/github_api.py` — GitHub API calls (PR creation, labels, merge)
- `app/git_ops.py` — clone, commit, push
- `app/repo_mapping.py` — CRUD for `repo_mappings` table
- `app/test_runner.py` — runs `pytest -q` in a cloned workspace
- `app/queue.py` — Redis queue enqueue/dequeue

**Event flow:**
```
Jira Webhook → POST /webhooks/jira → workflow_events → Dispatcher
  → Redis Queue → Worker thread

  story_implementation:
    clone repo → analyze → summarize → suggest change (+ memory) → apply
    → run tests → [fix attempt if failed] → commit/push → PR → auto-merge

  epic_breakdown:
    fetch planning memory → Claude decompose (+ memory) → store proposals
    → Telegram approval gate (APPROVE / REJECT / REGENERATE)
    → create Stories in Jira → trigger story_implementation via status change

Telegram Webhook → POST /webhooks/telegram → APPROVE/REJECT/REGENERATE handler
```

**Workflow triggers:**
| Jira status | Issue type | Workflow |
|---|---|---|
| `Ready for Dev` | Story | `story_implementation` |
| `Ready for Breakdown` | Epic | `epic_breakdown` |

## Data Model

All tables are created (and migrated) by `init_db()` in `app/database.py`.

| Table | Purpose |
|---|---|
| `workflow_events` | Raw Jira/Telegram webhook payloads |
| `workflow_runs` | One row per workflow execution; tracks status, branch, PR, test/merge results |
| `workflow_attempts` | Per-attempt records within a run (implement + optional fix) |
| `repo_mappings` | Jira project key → repo slug + branch + auto-merge policy |
| `planning_outputs` | Proposed Stories from epic_breakdown; one row per item per run |
| `feedback_events` | Atomic signals written after each run completes (append-only) |
| `memory_snapshots` | Derived and human-authored guidance; one row per (scope_type, scope_key, memory_kind) |

**`workflow_runs` status flow:**
```
RECEIVED → QUEUED → RUNNING → COMPLETED
                            → FAILED
                            → WAITING_FOR_APPROVAL → COMPLETED (after APPROVE)
                                                    → FAILED    (after REJECT/REGENERATE)
```

**`memory_snapshots` kinds:** `planning_guidance`, `execution_guidance`, `manual_note`
**`memory_snapshots` scopes:** `repo` (scope_key = repo_slug), `epic` (scope_key = epic_key)

## API Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | Health check |
| POST | `/webhooks/jira` | Jira event receiver |
| POST | `/webhooks/telegram` | Telegram approval command receiver |
| GET | `/debug/send-telegram` | Manual Telegram test |
| GET | `/debug/telegram/set-webhook` | Register Telegram bot webhook |
| GET | `/debug/repo-mappings` | List all repo mappings |
| GET | `/debug/repo-mappings/{id}` | Inspect one mapping |
| POST | `/debug/repo-mappings` | Create mapping |
| PUT | `/debug/repo-mappings/{id}` | Update mapping |
| DELETE | `/debug/repo-mappings/{id}` | Deactivate mapping |
| GET | `/debug/mapping-health` | Active mappings + fingerprint for env parity |
| GET | `/debug/planning-runs` | List recent planning runs |
| GET | `/debug/planning-runs/{run_id}` | Full planning run detail |
| POST | `/debug/planning-runs/{run_id}/approve` | HTTP APPROVE (same as Telegram command) |
| POST | `/debug/planning-runs/{run_id}/reject` | HTTP REJECT |
| GET | `/debug/workflow-runs` | List recent workflow runs |
| GET | `/debug/workflow-runs/{run_id}` | Full run detail including attempts |
| GET | `/debug/jira-events` | Last N raw Jira webhook payloads |
| POST | `/debug/epic-outcomes/{epic_key}` | Generate/refresh Epic outcome rollup |
| GET | `/debug/epic-outcomes/{epic_key}` | Return stored Epic outcome |
| POST | `/debug/memory` | Create/update a human-authored memory note |
| GET | `/debug/memory` | List memory snapshots (filter: scope_type, scope_key) |
| GET | `/debug/feedback-events` | List raw feedback events (filter: source_type, repo_slug, feedback_type, source_run_id) |
| POST | `/debug/memory/recompute` | Force-refresh a derived snapshot (scope_type=repo\|epic) |

## Telegram Message Format

```
[Orchestrator]
Event: <type>
Status: <status>
Details: <short summary>
```

## Phase Completion Status

| Phase | Description | Status |
|---|---|---|
| 1 | Project skeleton, health check, Docker, VM, CI/CD | ✅ Complete |
| 2 | PostgreSQL, Redis, worker, Telegram, Jira webhook, dispatcher | ✅ Complete |
| 3 | Dev/prod VM split, branch-based deploy, env model | ✅ Complete |
| 4 | Real Claude code generation, GitHub PR creation, apply/validate | ✅ Complete |
| 5 | Tests, fix loop, auto-merge, repo analysis, file selection | ✅ Complete |
| 6 | Epic breakdown, Claude planning, Telegram approval gate, Jira Story creation | ✅ Complete |
| 7 | Feedback capture, failure categorisation, memory snapshots, prompt enrichment, manual notes | ✅ Complete |

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

## Deferred / Out of Scope

- Feature-level Jira hierarchy (locked: Epic → Story only, no Feature or Task levels)
- Global-scope memory (deferred — no cross-repo patterns exist yet)
- Run-scope memory injection (single-run signals not worth feeding back into the same run)
- Memory pruning / decay (snapshots are recomputed from raw events — no TTL needed)
- Semantic/vector search for memory retrieval (rule-based aggregation is sufficient)
- UI or dashboard
- Production security hardening
- Multi-agent planning

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
(`auto_merge_enabled: true` in `config/seed_mappings.json`).

Auto-merge conditions:
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

## Phase 7 Configuration

### Jira hierarchy — LOCKED CONSTRAINT (Phase 7+)

**Epic → Story is the only planning hierarchy in this project. This must not change unless explicitly instructed.**

- Feature level does NOT exist and must not be added
- Task level is not used
- No code path should reference or route to a `feature_breakdown` workflow
- No planning prompt should treat Feature as an intermediate decomposition level
- Stories are the atomic unit of implementation

### Memory and feedback (Phase 7+)

| Setting | Value |
|---|---|
| Memory enabled | `true` |
| Max memory bullets injected into prompts | `5` |
| Max memory chars injected into prompts | `1000` |
| Memory scopes | `run`, `epic`, `repo` |
| Memory refresh mode | `on_write` |

### Failure categories (Phase 7+)

All failure categories are defined in `app/feedback.py` (`FailureCategory` class).

| Category | When applied |
|---|---|
| `test_failure` | Tests ran and failed |
| `syntax_failure` | Python syntax/parse error in generated code |
| `apply_validation_failure` | File apply guard rejected the change |
| `jira_creation_failure` | Jira API returned an error during child creation |
| `merge_failure` | PR creation or auto-merge failed |
| `duplicate_blocked` | Breakdown run blocked by idempotency guard |
| `approval_rejected` | User rejected a planning proposal |
| `approval_regenerated` | User requested regeneration of a proposal |
| `worker_interrupted` | Run was RUNNING when worker restarted |
| `unknown` | Error does not match any known pattern |

### Phase 7 Telegram event types

| Event type | When sent |
|---|---|
| `planning_feedback_recorded` | Feedback events written for a planning run |
| `execution_feedback_recorded` | Feedback events written for an execution run |
| `memory_snapshot_updated` | A memory snapshot was created or refreshed |
| `epic_outcome_ready` | Epic-level outcome rollup generated |
| `manual_memory_added` | A human-authored memory note was added |
