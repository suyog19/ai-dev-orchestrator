# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the App

**Local (uvicorn):**
```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**Docker (preferred for full stack):**
```bash
docker compose up -d --build          # start all services
docker compose logs -f app            # tail app logs
docker compose logs -f worker         # tail worker logs
docker compose down                   # stop everything
```

Verify with: `curl http://localhost:8000/healthz` → `{"status": "ok"}`

No test suite or linting config exists for this repo itself (the orchestrator). Tests run against the *target* sandbox repo (`suyog19/sandbox-fastapi-app`) as part of `story_implementation`.

## Environment Variables

Copy `.env.example` to `.env` and fill in secrets. All values are required unless noted.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL DSN — default: `postgresql://orchestrator:orchestrator@db:5432/orchestrator` |
| `REDIS_URL` | Redis DSN — default: `redis://redis:6379/0` |
| `ANTHROPIC_API_KEY` | Claude API key (`claude-sonnet-4-6` is the model used) |
| `GITHUB_TOKEN` | PAT with repo write, PR create/merge, label permissions |
| `JIRA_BASE_URL` | Jira instance URL (e.g. `https://yourorg.atlassian.net`) |
| `JIRA_EMAIL` | Jira API user (email) |
| `JIRA_API_TOKEN` | Jira API token |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Chat ID for notifications |
| `MAX_WORKERS` | Worker thread concurrency (default: `2`) |
| `ENV_NAME` | `DEV` or `PROD` — prepended to all Telegram messages |
| `PUBLIC_BASE_URL` | Public URL of this service (used when registering Telegram webhook) |
| `JIRA_CUSTOM_FIELD_EPIC_LINK` | Jira epic link custom field ID (default: `customfield_10014`) |

## Architecture

Python/FastAPI orchestration service. Receives Jira webhook events, persists them in PostgreSQL, dispatches workflows via a Redis-backed queue, executes them in a background worker, and notifies via Telegram.

**Key files:**
- `app/main.py` — FastAPI app, all HTTP endpoints
- `app/worker.py` — queue consumer; runs workflows in threads (MAX_WORKERS=2); recovers stale RUNNING→FAILED on startup
- `app/workflows.py` — `story_implementation` and `epic_breakdown` workflow logic
- `app/claude_client.py` — all Claude API calls (summarize, suggest, fix, plan, review, test quality review); uses `claude-sonnet-4-6` with ephemeral prompt caching on system prompts; `review_pr()` and `review_test_quality()` both use forced `tool_choice` for structured output
- `app/database.py` — all DB access; schema migrations in `init_db()`; `update_run_field()` / `update_run_step()` are the primary state-mutation functions used throughout the workflow
- `app/feedback.py` — feedback/memory constants and failure categorisation functions
- `app/dispatcher.py` — reads workflow_events and enqueues jobs onto Redis
- `app/file_modifier.py` — applies code patches returned by Claude (original → replacement matching)
- `app/repo_analysis.py` — introspects cloned repos (language detection, entry points, file counts) before Claude calls
- `app/webhooks.py` — Jira and Telegram webhook receivers
- `app/jira_client.py` — Jira REST API v3 calls; `get_issue_details()` fetches story summary + ADF-parsed description + acceptance criteria for the Reviewer Agent
- `app/github_api.py` — GitHub API calls (PR creation, labels, merge, `post_pr_comment()`)
- `app/git_ops.py` — clone, commit, push
- `app/repo_mapping.py` — CRUD for `repo_mappings` table
- `app/test_runner.py` — runs `pytest -q` in a cloned workspace
- `app/telegram.py` — `send_message(event_type, status, detail)` used by all workflow steps for Telegram notifications
- `app/queue.py` — Redis queue enqueue/dequeue

**Event flow:**
```
Jira Webhook → POST /webhooks/jira → workflow_events → Dispatcher
  → Redis Queue → Worker thread

  story_implementation:
    clone repo → analyze → summarize → suggest change (+ memory) → apply
    → run tests → [fix attempt if failed] → commit/push → PR
    → Reviewer Agent (review_pr) → store verdict → post PR comment → Telegram
    → Test Quality Agent (review_test_quality) → store verdict → post PR comment → Telegram
    → merge gate (APPROVED_BY_AI + TEST_QUALITY_APPROVED → merge
                 | BLOCKED → BLOCKED_BY_REVIEW
                 | TESTS_BLOCKING → BLOCKED_BY_TEST_QUALITY
                 | else → SKIPPED)

  epic_breakdown:
    fetch planning memory → Claude decompose (+ memory) → store proposals
    → Telegram approval gate (APPROVE / REJECT / REGENERATE)
    → create Stories in Jira → trigger story_implementation via status change

Telegram Webhook → POST /webhooks/telegram → APPROVE/REJECT/REGENERATE handler
```

**Workflow triggers:**
| Jira status | Issue type | Workflow |
|---|---|---|
| `Ready for Dev` (case-insensitive) | Story | `story_implementation` |
| `Ready for Breakdown` (case-insensitive) | Epic | `epic_breakdown` |

## Data Model

All tables are created (and migrated) by `init_db()` in `app/database.py`. First startup also seeds `repo_mappings` from `config/seed_mappings.json`.

| Table | Purpose |
|---|---|
| `workflow_events` | Raw Jira/Telegram webhook payloads |
| `workflow_runs` | One row per workflow execution; tracks status, branch, PR, test/merge/review results |
| `workflow_attempts` | Per-attempt records within a run (implement + optional fix) |
| `repo_mappings` | Jira project key → repo slug + branch + auto-merge policy |
| `planning_outputs` | Proposed Stories from epic_breakdown; one row per item per run |
| `feedback_events` | Atomic signals written after each run completes (append-only) |
| `memory_snapshots` | Derived and human-authored guidance; one row per (scope_type, scope_key, memory_kind) |
| `agent_reviews` | One row per Reviewer Agent verdict; FK to `workflow_runs` |
| `agent_test_quality_reviews` | One row per Test Quality Agent verdict; FK to `workflow_runs` |

**`workflow_runs` status flow:**
```
RECEIVED → QUEUED → RUNNING → COMPLETED
                            → FAILED
                            → WAITING_FOR_APPROVAL → COMPLETED (after APPROVE)
                                                    → FAILED    (after REJECT/REGENERATE)
```

**`workflow_runs.merge_status` values:** `MERGED` | `SKIPPED` | `BLOCKED_BY_REVIEW` | `BLOCKED_BY_TEST_QUALITY` | `FAILED`
**`workflow_runs.review_status` values:** `APPROVED_BY_AI` | `NEEDS_CHANGES` | `BLOCKED` | `ERROR` (NULL until review completes)
**`workflow_runs.test_quality_status` values:** `TEST_QUALITY_APPROVED` | `TESTS_WEAK` | `TESTS_BLOCKING` | `ERROR` (NULL until TQ review completes)

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
| GET | `/debug/agent-reviews` | List Reviewer Agent verdicts (filter: run_id, repo_slug, review_status) |
| GET | `/debug/workflow-runs/{run_id}/reviews` | All Reviewer Agent verdicts for one run |
| GET | `/debug/test-quality-reviews` | List Test Quality Agent verdicts (filter: run_id, repo_slug, quality_status) |
| GET | `/debug/workflow-runs/{run_id}/test-quality` | All Test Quality Agent verdicts for one run |

## Workflow Configuration

### story_implementation

| Setting | Value |
|---|---|
| Test command | `pytest -q` (after `pip install -r requirements.txt`) |
| Max fix attempts | 1 (max 2 total coding passes per run) |
| Max changed files | 3 (enforced by Claude tool schema; auto-merge blocks if exceeded) |
| Auto-merge conditions | tests PASSED + `review_status=APPROVED_BY_AI` + `test_quality_status=TEST_QUALITY_APPROVED` + PR created + `auto_merge_enabled=true` + ≤3 files changed |
| Test-enabled repo | `suyog19/sandbox-fastapi-app` |
| Workspace | `/tmp/workflows/{run_id}` (cleaned up after run) |

File selection for Claude (`suggest_change`): README + top 2 keyword-scored non-test files + up to 2 Python import dependencies + best test file (max 6 files total).

### epic_breakdown

| Setting | Value |
|---|---|
| Max Stories per Epic | 8 |
| Output issue type | `Story` |
| Approval commands | `APPROVE <run_id>` / `REJECT <run_id>` / `REGENERATE <run_id>` |
| Idempotency guard | Blocks if the Epic already has Jira children |

### Memory injection

| Setting | Value |
|---|---|
| Max bullets injected | 5 |
| Max chars injected | 1000 |
| Scopes | `repo` (execution guidance), `epic` (planning guidance) |
| Refresh | Triggered on every feedback write (`on_write`) |

### Failure categories (defined in `app/feedback.py`)

| Category | When applied |
|---|---|
| `test_failure` | Tests ran and failed |
| `syntax_failure` | Python syntax/parse error in generated code |
| `apply_validation_failure` | File apply guard rejected the change |
| `jira_creation_failure` | Jira API error during child creation |
| `merge_failure` | PR creation or auto-merge failed |
| `duplicate_blocked` | Breakdown blocked by idempotency guard |
| `approval_rejected` | User rejected a planning proposal |
| `approval_regenerated` | User requested regeneration |
| `worker_interrupted` | Run was RUNNING when worker restarted |
| `unknown` | Error does not match any known pattern |

### Reviewer Agent

**`review_status` values:** `APPROVED_BY_AI` | `NEEDS_CHANGES` | `BLOCKED` | `ERROR`
**`risk_level` values:** `LOW` | `MEDIUM` | `HIGH`

| Setting | Value |
|---|---|
| Review required | `true` — every `story_implementation` run triggers a review |
| Review blocks merge | `true` — `APPROVED_BY_AI` required for auto-merge |
| Reviewer Agent prompt | `REVIEWER_PROMPT` in `app/claude_client.py` |
| Output format | Forced tool_use (`submit_review`) with required structured fields |
| GitHub action | Top-level PR comment with emoji verdict summary |
| Merge on `NEEDS_CHANGES` | `merge_status=SKIPPED` |
| Merge on `BLOCKED` | `merge_status=BLOCKED_BY_REVIEW` |
| Merge on `ERROR` | `merge_status=SKIPPED` (non-fatal; run continues) |

**Review feedback events:** `review_status`, `review_risk_level`, `review_approved`, `review_needs_changes`, `review_blocked`

### Test Quality Agent

**`quality_status` values:** `TEST_QUALITY_APPROVED` | `TESTS_WEAK` | `TESTS_BLOCKING` | `ERROR`
**`confidence_level` values:** `LOW` | `MEDIUM` | `HIGH`

| Setting | Value |
|---|---|
| Review required | `true` — every `story_implementation` run triggers a TQ review |
| Blocks merge | `true` — `TEST_QUALITY_APPROVED` required for auto-merge |
| Test Quality Agent prompt | `TEST_QUALITY_PROMPT` in `app/claude_client.py` |
| Output format | Forced tool_use (`submit_test_quality_review`) with required structured fields |
| GitHub action | Top-level PR comment with emoji verdict summary |
| Merge on `TESTS_WEAK` | `merge_status=SKIPPED` |
| Merge on `TESTS_BLOCKING` | `merge_status=BLOCKED_BY_TEST_QUALITY` |
| Merge on `ERROR` | `merge_status=SKIPPED` (non-fatal; run continues) |

**Test Quality feedback events:** `test_quality_status`, `test_quality_confidence`, `test_quality_approved`, `tests_weak`, `tests_blocking`, `missing_test_count`, `suspicious_test_count`

## Telegram Message Format

```
[DEV|PROD]
[Orchestrator]
Event: <type>
Status: <status>
Details: <short summary>
```

Approval commands for epic_breakdown are sent as plain text to the bot: `APPROVE <run_id>`, `REJECT <run_id>`, `REGENERATE <run_id>`.

## Environment Model

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

## Working Style

Implement one iteration at a time. After each iteration: commit, push to `dev`, wait for CI/CD (`gh run watch`), then **validate autonomously on the dev EC2 instance** (SSH to `65.2.140.4`, exec into the app container, run validation scripts) before reporting the iteration complete. Do not rely on local Docker for validation. Only ask the user to proceed once the EC2 validation passes.

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
- No code path should reference or route to a `feature_breakdown` workflow
- Global-scope memory (deferred — no cross-repo patterns exist yet)
- Run-scope memory injection (single-run signals not worth feeding back into the same run)
- Memory pruning / decay (snapshots are recomputed from raw events — no TTL needed)
- Semantic/vector search for memory retrieval (rule-based aggregation is sufficient)
- UI or dashboard
- Production security hardening
- Multi-agent planning
