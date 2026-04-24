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
| `ADMIN_API_KEY` | Shared secret protecting all `/debug/*` and `/admin/*` endpoints |
| `JIRA_WEBHOOK_SECRET` | Query param token for Jira webhook validation (optional but recommended) |
| `ALLOW_GITHUB_WRITES` | `true`/`false` — global GitHub write kill switch (default: `true`) |
| `ALLOW_AUTO_MERGE` | `true`/`false` — auto-merge kill switch (default: `true`) |
| `ORCHESTRATOR_PAUSED` | `true`/`false` — bootstrap pause state (DB flag takes precedence once set) |

## Architecture

Python/FastAPI orchestration service. Receives Jira webhook events, persists them in PostgreSQL, dispatches workflows via a Redis-backed queue, executes them in a background worker, and notifies via Telegram.

**Key files:**
- `app/main.py` — FastAPI app, all HTTP endpoints
- `app/worker.py` — queue consumer; runs workflows in threads (MAX_WORKERS=2); recovers stale RUNNING→FAILED on startup
- `app/workflows.py` — `story_implementation` and `epic_breakdown` workflow logic
- `app/claude_client.py` — all Claude API calls (summarize, suggest, fix, plan, review, test quality review, architecture review); uses `claude-sonnet-4-6` with ephemeral prompt caching on system prompts; `review_pr()`, `review_test_quality()`, and `review_architecture()` all use forced `tool_choice` for structured output
- `app/database.py` — all DB access; schema migrations in `init_db()`; `update_run_field()` / `update_run_step()` are the primary state-mutation functions used throughout the workflow
- `app/feedback.py` — feedback/memory constants and failure categorisation functions
- `app/dispatcher.py` — reads workflow_events and enqueues jobs onto Redis
- `app/file_modifier.py` — applies code patches returned by Claude (original → replacement matching)
- `app/repo_analysis.py` — introspects cloned repos (language detection, entry points, file counts) before Claude calls
- `app/security.py` — admin key middleware, GitHub write guard, Redis rate limiting
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
    → Architecture Agent (review_architecture) → store verdict → post PR comment → Telegram
    → Unified Release Gate (evaluate_release_decision) → persist release_decision
      RELEASE_APPROVED → merge
      RELEASE_BLOCKED  → BLOCKED_BY_REVIEW | BLOCKED_BY_TEST_QUALITY | BLOCKED_BY_ARCHITECTURE
      RELEASE_SKIPPED  → SKIPPED

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
| `agent_architecture_reviews` | One row per Architecture Agent verdict; FK to `workflow_runs` |
| `security_events` | Append-only audit log of auth failures, webhook rejections, write blocks |
| `control_flags` | Runtime control flags (paused state); DB takes precedence over env var |

**`workflow_runs` status flow:**
```
RECEIVED → QUEUED → RUNNING → COMPLETED
                            → FAILED
                            → WAITING_FOR_APPROVAL → COMPLETED (after APPROVE)
                                                    → FAILED    (after REJECT/REGENERATE)
```

**`workflow_runs.merge_status` values:** `MERGED` | `SKIPPED` | `BLOCKED_BY_REVIEW` | `BLOCKED_BY_TEST_QUALITY` | `BLOCKED_BY_ARCHITECTURE` | `FAILED`
**`workflow_runs.review_status` values:** `APPROVED_BY_AI` | `NEEDS_CHANGES` | `BLOCKED` | `ERROR` (NULL until review completes)
**`workflow_runs.test_quality_status` values:** `TEST_QUALITY_APPROVED` | `TESTS_WEAK` | `TESTS_BLOCKING` | `ERROR` (NULL until TQ review completes)
**`workflow_runs.architecture_status` values:** `ARCHITECTURE_APPROVED` | `ARCHITECTURE_NEEDS_REVIEW` | `ARCHITECTURE_BLOCKED` | `ERROR` (NULL until arch review completes)
**`workflow_runs.release_decision` values:** `RELEASE_APPROVED` | `RELEASE_SKIPPED` | `RELEASE_BLOCKED` (set by `evaluate_release_decision()`)

**`clarification_requests.status` values:** `PENDING` | `ANSWERED` | `CANCELLED` | `EXPIRED`
**Clarification context keys:** `pre_planning` (epic), `pre_suggest` (story implementation), `pre_review` (review agents)

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
| GET | `/debug/architecture-reviews` | List Architecture Agent verdicts (filter: run_id, repo_slug, architecture_status) |
| GET | `/debug/workflow-runs/{run_id}/architecture` | All Architecture Agent verdicts for one run |
| GET | `/debug/workflow-runs/{run_id}/release-decision` | Release Gate decision + all agent statuses for one run |
| GET | `/debug/clarifications` | List clarifications (filter: status, run_id, limit) |
| GET | `/debug/clarifications/{id}` | Single clarification detail |
| POST | `/debug/clarifications/{id}/answer` | Admin answer + resume workflow |
| POST | `/debug/clarifications/{id}/cancel` | Admin cancel + fail workflow |
| POST | `/debug/clarifications/{id}/resend` | Resend Telegram question |
| GET | `/admin/security-events` | List security audit events (filter: event_type, source, status) |
| GET | `/admin/control-status` | Current runtime control flags (paused state) |
| POST | `/admin/pause` | Pause orchestrator — blocks Jira dispatch + Telegram commands |
| POST | `/admin/resume` | Resume orchestrator |
| GET | `/admin/github/branch-protection` | Audit branch protection for a repo (query: repo_slug, branch) |

## Workflow Configuration

### story_implementation

| Setting | Value |
|---|---|
| Test command | `pytest -q` (after `pip install -r requirements.txt`) |
| Max fix attempts | 1 (max 2 total coding passes per run) |
| Max changed files | 3 (enforced by Claude tool schema; auto-merge blocks if exceeded) |
| Auto-merge conditions | tests PASSED + `review_status=APPROVED_BY_AI` + `test_quality_status=TEST_QUALITY_APPROVED` + `architecture_status=ARCHITECTURE_APPROVED` + PR created + `auto_merge_enabled=true` + ≤3 files changed; all evaluated by `evaluate_release_decision()` in `workflows.py` |
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

### Clarification Loop (Phase 12)

`app/clarification.py` is the core module.

| Setting | Value |
|---|---|
| Enabled by default | `CLARIFICATION_ENABLED=True` in `app/feedback.py` |
| Timeout | `CLARIFICATION_TIMEOUT_HOURS=24` (configurable per request) |
| Control flag | `clarification_enabled` in `control_flags` table |
| Telegram commands | `ANSWER <id> <text>` / `CANCEL <id>` / `CLARIFY <id>` |
| Vagueness trigger (Epic) | Summary < 4 words OR no description OR description < 50 chars |
| Ambiguity trigger (Story) | No acceptance criteria AND no description |
| Review agent trigger | Agent returns `needs_clarification=true` in tool output |
| Periodic expiry | Worker loop: every ~720 iterations (~1 hour) + startup |

**Resume paths by context key:**
- `pre_planning`: Epic re-runs from start; clarification answer injected into planning memory
- `pre_suggest`: Story re-runs from start; clarification answer injected into suggestion memory
- `pre_review`: Skip-to-review via `_story_review_and_release()` using `pr_url` from DB + GitHub diff

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

### Architecture Agent

**`architecture_status` values:** `ARCHITECTURE_APPROVED` | `ARCHITECTURE_NEEDS_REVIEW` | `ARCHITECTURE_BLOCKED` | `ERROR`
**`risk_level` values:** `LOW` | `MEDIUM` | `HIGH`

| Setting | Value |
|---|---|
| Review required | `true` — every `story_implementation` run triggers a review |
| Review blocks merge | `true` — `ARCHITECTURE_APPROVED` required for auto-merge |
| Architecture Agent prompt | `ARCHITECTURE_PROMPT` in `app/claude_client.py` |
| Output format | Forced tool_use (`submit_architecture_review`) with architecture_status, risk_level, summary, impact_areas, blocking_reasons, recommendations |
| GitHub action | Top-level PR comment with emoji verdict summary |
| Merge on `ARCHITECTURE_NEEDS_REVIEW` | `merge_status=SKIPPED` |
| Merge on `ARCHITECTURE_BLOCKED` | `merge_status=BLOCKED_BY_ARCHITECTURE` |
| Merge on `ERROR` | `merge_status=SKIPPED` (non-fatal; run continues) |
| File classification | `_classify_changed_files()` in `workflows.py` — api, model, storage, config, test, doc |

**Architecture feedback events:** `architecture_status`, `architecture_risk_level`, `architecture_approved`, `architecture_needs_review`, `architecture_blocked`

### Unified Release Gate

`evaluate_release_decision(mapping, final_test_result, applied, review_status, test_quality_status, architecture_status) -> dict` (pure function in `workflows.py`)

Returns: `{release_decision, can_auto_merge, reason, blocking_gates, warnings}`

| Gate | BLOCKED if | SKIPPED if |
|---|---|---|
| Tests | `status == "FAILED"` | `status not in ("PASSED", "FAILED")` e.g. NOT_RUN |
| Reviewer | `BLOCKED` | `NEEDS_CHANGES` or `ERROR` |
| Test Quality | `TESTS_BLOCKING` | `TESTS_WEAK` or `ERROR` |
| Architecture | `ARCHITECTURE_BLOCKED` | `ARCHITECTURE_NEEDS_REVIEW` or `ERROR` |
| Auto-merge | — | `auto_merge_enabled=False` |
| File count | — | `count > 3` |

**Release feedback events:** `release_decision`, `release_blocking_gate_count`

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

## Security Layer

**`app/security.py`** is the central security module.

### Admin Key Auth

All `/debug/*` and `/admin/*` paths are protected by `X-Orchestrator-Admin-Key` header middleware (`admin_key_middleware` registered via `BaseHTTPMiddleware`). Auth failures are recorded as `admin_auth_failed` security events. Successful mutating calls (POST/PUT/DELETE/PATCH) are recorded as `admin_auth_success`.

### GitHub Write Guard

`ensure_github_writes_allowed(action, repo_slug, run_id)` in `app/security.py` — call before any GitHub write. Raises `RuntimeError` (caught by existing workflow try/except) when:
- Orchestrator is paused (DB flag checked first, env var fallback)
- `ALLOW_GITHUB_WRITES=false`
- `ALLOW_AUTO_MERGE=false` (for `merge_pr` action only)

Wired in `app/workflows.py` before `commit_and_push` (push), `create_pull_request` (create_pr), `merge_pull_request` (merge_pr).

### Rate Limiting

Redis sliding-window rate limiting in `check_rate_limit(path, identifier)`:

| Endpoint | Limit |
|---|---|
| `/webhooks/jira` | 30 req/min (global) |
| `/webhooks/telegram` | 10 req/min (per chat_id) |
| Admin mutating calls | 20 req/min (per client IP) |

Returns `True` (allow) or `False` (deny). Fails open on Redis errors. Returns 429 for admin endpoints, 200/ok for Telegram (Telegram requires 200 on all responses).

### Control Flags

`control_flags` table in DB — `is_paused()` checks DB then env var fallback. Seeded from `ORCHESTRATOR_PAUSED` env var at startup (ON CONFLICT DO NOTHING — DB takes precedence after first set).

### Security Events

`security_events` table — append-only audit log. `record_security_event(event_type, source, actor, endpoint, method, status, details)` in `app/database.py`. Event types: `admin_auth_failed`, `admin_auth_success`, `webhook_rejected`, `telegram_rejected`, `github_write_blocked`, `automation_paused_jira_blocked`, `automation_paused_telegram_blocked`.

### Docs

- `docs/security/endpoint-inventory.md` — all endpoints with auth requirements
- `docs/security/token-permissions.md` — minimum permission scopes for all secrets
- `docs/runbooks/orchestrator-ops.md` — operational runbook (pause, rotate secrets, recover stale runs)

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

**EC2 validation script pattern:** Write scripts to `/c/tmp/`, SCP to EC2, `docker cp` into the app container, run with `docker exec ... python3`. Any script that calls DB functions (`list_*`, `record_*`, `generate_*`) must call `from app.database import init_db; init_db()` first — the connection pool is `None` in standalone scripts and `get_conn()` will raise `AttributeError` otherwise. HTTP endpoints can be hit via `http://localhost:8000` from inside the container using `urllib.request`.

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
- Multi-agent planning
