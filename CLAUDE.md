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

PostgreSQL is exposed on host port **5433** (internal 5432) when running via docker-compose. Redis on 6379. The `worker` service runs as a separate container (`python -m app.worker`) — it shares the same Dockerfile as `app`.

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
| `JIRA_WEBHOOK_SECRET` | Query param token for Jira webhook validation (optional but recommended) — when set, the Jira webhook URL **must** include `?token=<value>` or all events will be rejected 401 |
| `ALLOW_GITHUB_WRITES` | `true`/`false` — global GitHub write kill switch (default: `true`) |
| `ALLOW_AUTO_MERGE` | `true`/`false` — auto-merge kill switch (default: `true`) |
| `ORCHESTRATOR_PAUSED` | `true`/`false` — bootstrap pause state (DB flag takes precedence once set) |
| `DEPLOYMENT_VALIDATION_ENABLED` | `true`/`false` — kill switch for post-merge smoke testing (default: `true`) |
| `DEPLOYMENT_VALIDATION_TIMEOUT_SECONDS` | Per-smoke-test HTTP timeout (default: `120`) |
| `DEPLOYMENT_VALIDATION_RETRY_COUNT` | Retries per smoke test before declaring FAILED (default: `3`) |
| `DEPLOYMENT_VALIDATION_RETRY_DELAY_SECONDS` | Delay between retries (default: `10`) |
| `FIRST_USE_MODE_ENABLED` | `true`/`false` — first-use safety mode; skips auto-merge until `FIRST_USE_RUN_COUNT` runs complete (default: `true`) |
| `FIRST_USE_RUN_COUNT` | Number of completed runs before first-use mode deactivates per repo (default: `3`) |
| `ORCHESTRATOR_SELF_REPO` | `owner/repo` slug of the orchestrator's own repo — permanently blocks auto-merge for that repo (default: `suyog19/ai-dev-orchestrator`) |

## Architecture

Python/FastAPI orchestration service. Receives Jira webhook events, persists them in PostgreSQL, dispatches workflows via a Redis-backed queue, executes them in a background worker, and notifies via Telegram.

**Key files:**
- `app/main.py` — FastAPI app, all HTTP endpoints
- `app/worker.py` — queue consumer; runs workflows in threads (MAX_WORKERS=2); recovers stale RUNNING→FAILED on startup; `_execute_onboarding()` handles `project_onboarding` jobs and manages `project_onboarding_runs` status independently from `workflow_runs`
- `app/workflows.py` — `story_implementation` and `epic_breakdown` workflow logic; **note**: there is a stale one-argument `_is_test_file()` near the top of the file that is dead code (overridden by the profile-aware version further down); `_TEST_FILE_PATTERNS` defined alongside it is also unused
- `app/claude_client.py` — all Claude API calls (summarize, suggest, fix, plan, review, test quality review, architecture review); uses `claude-sonnet-4-6` with ephemeral prompt caching on system prompts; `review_pr()`, `review_test_quality()`, and `review_architecture()` all use forced `tool_choice` for structured output; `generate_onboarding_architecture_summary()` and `generate_onboarding_coding_conventions()` use forced tool_use for structured onboarding snapshots
- `app/database.py` — all DB access; schema migrations in `init_db()`; `update_run_field()` / `update_run_step()` are the primary state-mutation functions used throughout the workflow
- `app/feedback.py` — feedback/memory constants and failure categorisation functions
- `app/dispatcher.py` — reads workflow_events and enqueues jobs onto Redis
- `app/file_modifier.py` — applies code patches returned by Claude (original → replacement matching)
- `app/repo_analysis.py` — introspects cloned repos (language detection, entry points, file counts) before Claude calls
- `app/security.py` — admin key middleware, GitHub write guard, Redis rate limiting; `/admin/ui/*` paths are exempt from header auth via `_UI_EXEMPT_PREFIX` so cookie auth takes over
- `app/ui.py` — FastAPI router at `/admin/ui`; all browser-facing dashboard pages (login, overview, runs, planning, clarifications, agents, GitHub, memory, security, control); uses Jinja2 templates from `app/templates/admin/` and static assets from `app/static/admin/`
- `app/ui_auth.py` — cookie auth helpers for the dashboard: `create_session_token()`, `verify_session_token()`, `csrf_token()`, `require_admin_ui()`; signed with `URLSafeTimedSerializer` keyed on ADMIN_API_KEY; 8-hour TTL
- `app/webhooks.py` — Jira and Telegram webhook receivers
- `app/jira_client.py` — Jira REST API v3 calls; `get_issue_details()` fetches story summary + ADF-parsed description + acceptance criteria for the Reviewer Agent
- `app/github_api.py` — GitHub API calls: PR creation, labels, merge, `post_pr_comment()`, `get_pr_details()` (fetches head SHA), `create_commit_status()` (publishes GitHub commit statuses), `get_branch_protection()` (includes orchestrator check audit)
- `app/git_ops.py` — clone, commit, push
- `app/repo_mapping.py` — CRUD for `repo_mappings` table; `upsert_seed_mappings()` runs on startup from `config/seed_mappings.json` (currently empty — new mappings are created via the onboarding wizard); it respects rows where `is_active=false` (will not re-create deactivated mappings); deactivate via `DELETE /debug/repo-mappings/{id}`
- `app/repo_profiler.py` — detects repo capability profile from cloned workspace (detection order: Gradle > Maven > Node > Python > Unknown); `detect_repo_capability_profile()`, `get_test/build/lint_command_for_profile()`
- `app/command_runner.py` — `run_repo_command()`: safe, injection-proof command execution using `shlex.split()` (no `shell=True`); handles timeout/FileNotFoundError/OSError; output truncated at 4000 chars
- `app/test_runner.py` — profile-aware test/build/lint runner: `run_tests()` (with dep install), `run_build()`, `run_lint()` all delegate to `run_repo_command()`
- `app/telegram.py` — `send_message(event_type, status, detail)` used by all workflow steps for Telegram notifications
- `app/queue.py` — Redis queue enqueue/dequeue
- `app/clarification.py` — clarification loop: detects vagueness/ambiguity, sends Telegram questions, suspends runs, resumes on answer
- `app/github_status_mapper.py` — pure functions mapping internal verdicts → GitHub commit status payloads (state, description, context); one function per gate
- `app/github_status_publisher.py` — `publish_github_statuses_for_run(run_id, repo_slug, pr_number)`: publishes all 5 pre-merge statuses after Release Gate, non-fatal; `publish_deployment_validation_status()` publishes the 6th (`orchestrator/deployment-validation`) after post-merge validation
- `app/deployment_validator.py` — `run_http_smoke_test()` (HTTP check with expected_status + expected_contains); `run_deployment_validation()` (loads profile, runs smoke tests with retries, stores result); status values: `NOT_CONFIGURED | SKIPPED | PASSED | FAILED | ERROR`
- `app/onboarding.py` — `run_project_onboarding(run_id, repo_slug, base_branch)`: 7-step onboarding workflow (clone → profile → command validation → structure scan → architecture summary → conventions → deployment check); workspace at `/tmp/onboarding/<run_id>/repo`
- `app/repo_scanner.py` — `scan_repo_structure(workspace_path, profile_name)`: classifies repo files into top_level, config, deploy, routing, model, service, test, doc categories; all lists capped at 20 entries; path-based only (no file content)
- `app/bootstrap.py` — `run_project_bootstrap(repo_slug, project_type, description)`: scaffolds a new GitHub repo from templates; supported types: `python_fastapi`, `static_site`; `SUPPORTED_PROJECT_TYPES` constant

**Event flow:**
```
Jira Webhook → POST /webhooks/jira → workflow_events → Dispatcher
  → Redis Queue → Worker thread

  story_implementation:
    clone repo → analyze → detect capability profile (upsert DB) → summarize → suggest change (+ memory) → apply
    → run tests (profile-aware: pip/npm install + profile test command) → [fix attempt if failed]
    → run build (if profile has build command) → run lint (if profile has lint command)
    → commit/push → PR (store head_sha)
    → Reviewer Agent (review_pr) → store verdict → post PR comment → Telegram
    → Test Quality Agent (review_test_quality) → store verdict → post PR comment → Telegram
    → Architecture Agent (review_architecture) → store verdict → post PR comment → Telegram
    → Unified Release Gate (evaluate_release_decision) → persist release_decision
    → publish GitHub commit statuses (5 contexts via publish_github_statuses_for_run)
      RELEASE_APPROVED → merge
        → post-merge deployment validation (observational; never reverts merge)
          → run_deployment_validation() → store in deployment_validations
          → publish orchestrator/deployment-validation GitHub status
          → Telegram: deployment_validation_passed | deployment_validation_failed
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

**Jira webhook filter note:** The Jira webhook registered on the dev/prod instance has a JQL filter (e.g. `project = "My Software Team"`). If a new project's key is not covered by the filter, its status-change events won't reach the orchestrator. To manually simulate a webhook for a new project while the filter is being updated:
```bash
curl -X POST "https://<orchestrator-url>/webhooks/jira?token=$JIRA_WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"webhookEvent":"jira:issue_updated","issue_event_type_name":"issue_generic","changelog":{"items":[{"field":"status","fromString":"To Do","toString":"READY FOR DEV"}]},"issue":{"id":"<id>","key":"<KEY-N>","fields":{"summary":"...","status":{"name":"READY FOR DEV"},"issuetype":{"name":"Story"},"description":null}}}'
```

## Data Model

All tables are created (and migrated) by `init_db()` in `app/database.py`. First startup calls `upsert_seed_mappings()` from `config/seed_mappings.json` (currently empty — repo mappings are managed via the onboarding wizard).

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
| `github_status_updates` | Append-only audit log of every GitHub commit status publish attempt; one row per gate per run per attempt |
| `repo_capability_profiles` | Active capability profile per repo (profile_name, commands, capabilities_json); upserted on each clone |
| `clarification_requests` | One row per clarification issued; FK to `workflow_runs`; status: PENDING / ANSWERED / CANCELLED / EXPIRED |
| `deployment_profiles` | Per-repo/environment smoke test configuration; unique on (repo_slug, environment); seeded from `config/deployment_profiles.yaml` |
| `deployment_validations` | One row per post-merge validation run (FK to workflow_runs); stores smoke_results_json, status, timing |
| `project_onboarding_runs` | One row per onboarding execution; tracks status, profile, command results, architecture_summary, structure_scan_json, deployment_profile_status |
| `project_knowledge_snapshots` | One row per (repo_slug, snapshot_kind); snapshot kinds: architecture, commands, testing, deployment, coding_conventions, open_questions, onboarding_retrospective; updated on each onboarding run |

**`project_onboarding_runs` status flow:** `PENDING → RUNNING → COMPLETED | FAILED`
**`project_onboarding_runs.deployment_profile_status` values:** `CONFIGURED_ENABLED | CONFIGURED_DISABLED | DRAFT_CREATED | NOT_CONFIGURED`

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
**`workflow_runs` extended columns:** `head_sha` (GitHub PR head SHA), `github_statuses_published` (bool), `capability_profile_name`, `build_status`, `lint_status`, `dependency_install_status`, `deployment_validation_status`, `deployment_validation_summary`, `deployment_validation_completed_at`

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
| GET | `/admin/github/branch-protection` | Audit branch protection; includes `orchestrator_check_status` (release_gate_required, missing_required) |
| GET | `/debug/github-status-updates` | List GitHub status publish history for a run (query: run_id) |
| GET | `/debug/workflow-runs/{run_id}/github-statuses` | Same, run-scoped path |
| POST | `/debug/workflow-runs/{run_id}/republish-github-statuses` | Re-publish statuses idempotently (query: repo_slug) |
| POST | `/admin/github/statuses/backfill` | Backfill statuses for recent eligible runs (body: repo_slug, limit, only_missing) |
| POST | `/admin/github/branch-protection/validate-required-checks` | Dry-run check for required `orchestrator/release-gate` context (body: repo_slug, branch) |
| GET | `/debug/repo-capability-profiles` | List all capability profiles (filter: repo_slug) |
| GET | `/debug/repo-capability-profiles/{repo_slug}` | Get active profile for one repo |
| GET | `/debug/deployment-policy` | Deployment validation policy per capability profile (all or single) |
| GET | `/debug/deployment-profiles` | List deployment profiles (filter: repo_slug) |
| POST | `/debug/deployment-profiles` | Create deployment profile |
| GET | `/debug/deployment-profiles/{repo_slug}` | Get profile for repo+env (query: environment) |
| PUT | `/debug/deployment-profiles/{id}` | Update deployment profile fields |
| GET | `/debug/deployment-validations` | List validations (filter: run_id, repo_slug, status, limit) |
| GET | `/debug/workflow-runs/{run_id}/deployment-validation` | Latest deployment validation for a run |
| POST | `/debug/workflow-runs/{run_id}/run-deployment-validation` | Admin re-run deployment validation (query: repo_slug, environment) |
| GET | `/admin/ui/login` | Dashboard login page |
| POST | `/admin/ui/login` | Submit credentials; sets `orchestrator_admin_session` cookie |
| GET | `/admin/ui/logout` | Clear session cookie |
| GET | `/admin/ui/overview` | System overview — stats, recent runs, pending clarifications |
| GET | `/admin/ui/runs` | Workflow runs list (filter: status, workflow_type, issue_key, release_decision, limit) |
| GET | `/admin/ui/runs/{run_id}` | Full run detail: all agents, GitHub statuses, active clarification, error |
| GET | `/admin/ui/planning` | Epic breakdown planning runs list |
| GET | `/admin/ui/planning/{run_id}` | Planning run detail with proposals |
| GET | `/admin/ui/clarifications` | Clarifications list (filter: status) |
| POST | `/admin/ui/clarifications/{id}/answer` | Answer clarification via UI form |
| POST | `/admin/ui/clarifications/{id}/cancel` | Cancel clarification via UI form |
| POST | `/admin/ui/clarifications/{id}/resend` | Resend Telegram question via UI form |
| GET | `/admin/ui/agents` | Agent reviews (tab: reviewer \| test_quality \| architecture; filter: run_id, repo_slug, status) |
| GET | `/admin/ui/github` | GitHub commit statuses inspector (filter by run_id) + branch protection check |
| POST | `/admin/ui/github/republish` | Republish GitHub statuses for a run |
| POST | `/admin/ui/github/validate` | Validate branch protection via UI form |
| GET | `/admin/ui/memory` | Memory snapshots (filter: scope_type, scope_key) |
| POST | `/admin/ui/memory/note` | Add/update a manual memory note |
| GET | `/admin/ui/security` | Security events log (filter: event_type, source, status) |
| GET | `/admin/ui/control` | Runtime control flags and env vars |
| POST | `/admin/ui/control/pause` | Pause orchestrator (CSRF-protected) |
| POST | `/admin/ui/control/resume` | Resume orchestrator (CSRF-protected) |
| GET | `/admin/ui/deployments` | Deployment profiles + recent validations list (filter: repo_slug, status, limit) |
| POST | `/admin/ui/runs/{run_id}/run-deployment-validation` | Re-run deployment validation for a run (CSRF-protected) |
| GET | `/admin/ui/projects` | Project onboarding list (all repos with onboarding data) |
| GET | `/admin/ui/projects/new` | Wizard: repo onboarding setup form |
| POST | `/admin/ui/projects/new` | Wizard: validate, create mapping (with duplicate guard), start onboarding, redirect to status |
| GET | `/admin/ui/projects/new/status/{run_id}` | Wizard: live progress page (auto-refreshes every 5s); shows JQL to paste into Jira webhook filter on completion |
| GET | `/admin/ui/projects/{repo_slug}` | Project detail: latest run, capability profile, all knowledge snapshots, run history |
| POST | `/admin/project-onboarding/start` | Start onboarding for a repo (body: repo_slug, base_branch) |
| GET | `/admin/project-onboarding/runs` | List onboarding runs (filter: repo_slug) |
| GET | `/admin/project-onboarding/runs/{run_id}` | Single onboarding run detail |
| POST | `/admin/project-onboarding/{repo_slug}/create-jira-mapping` | Create repo mapping after onboarding (body: jira_project_key, base_branch, environment, auto_merge_enabled) |
| GET | `/debug/project-knowledge` | List knowledge snapshots for a repo (query: repo_slug) |
| POST | `/debug/project-knowledge/{repo_slug}/refresh` | Re-run knowledge snapshots without re-detecting profile (query: base_branch); synchronous, 60-120s |
| POST | `/admin/project-onboarding/{repo_slug}/activate` | 6-step activation report: profile, commands, knowledge, deployment, mapping, first-use status |
| POST | `/admin/project-bootstrap/start` | Scaffold a new repo from template (body: repo_slug, project_type: `python_fastapi`\|`static_site`, description) |

## Workflow Configuration

### story_implementation

| Setting | Value |
|---|---|
| Test command | Profile-based (`get_test_command_for_profile()`); defaults to `pytest -q --tb=short` for Python FastAPI |
| Max fix attempts | 1 (max 2 total coding passes per run) |
| Max changed files | 3 (enforced by Claude tool schema; auto-merge blocks if exceeded) |
| Auto-merge conditions | tests PASSED + `review_status=APPROVED_BY_AI` + `test_quality_status=TEST_QUALITY_APPROVED` + `architecture_status=ARCHITECTURE_APPROVED` + PR created + `auto_merge_enabled=true` + ≤3 files changed + profile policy allows auto-merge; all evaluated by `evaluate_release_decision()` in `workflows.py` |
| Test-enabled repo | `suyog19/sandbox-fastapi-app` (Python), `suyog19/sandbox-java-maven`, `suyog19/sandbox-java-gradle`, `suyog19/sandbox-node-react` |
| Workspace | `/tmp/workflows/{run_id}` (cleaned up after run) |

File selection for Claude (`suggest_change`): README + top 2 keyword-scored non-test files + up to 2 Python import dependencies + best test file (max 6 files total).

### Capability Profiles

`app/repo_profiler.py` — detection order: Gradle > Maven > Node > Python > Unknown.

| Profile | Detected by | Test command | Auto-merge |
|---|---|---|---|
| `python_fastapi` | `requirements.txt` / `pyproject.toml` + FastAPI reference or `app/main.py` | `pytest -q --tb=short` | Allowed |
| `java_maven` | `pom.xml` | `mvn test -q` (build: `mvn package -DskipTests -q`) | Disabled |
| `java_gradle` | `build.gradle` or (`gradlew` + `settings.gradle`) | `./gradlew test` or `gradle test` | Disabled |
| `node_react` | `package.json` + (vite/next config OR react dep OR `src/`) | From `package.json` scripts | Disabled |
| `generic_unknown` | fallback | None | Disabled |

**Profile release policies** (`_PROFILE_RELEASE_POLICY` in `workflows.py`):
- `python_fastapi`: `allow_auto_merge=True`, `require_tests=True`, `require_build=False`
- `java_maven`/`java_gradle`: `allow_auto_merge=False`, `require_tests=True`, `require_build=True`
- `node_react`: `allow_auto_merge=False`, `require_tests=False`, `require_build=True`
- `generic_unknown`: `allow_auto_merge=False`, all requirements False

**Build/lint policy**: build `FAILED` = `RELEASE_BLOCKED`; lint `FAILED` = `RELEASE_SKIPPED`; `NOT_RUN` = skip only if `require_build=True` for that profile.

**File classification** (`_classify_changed_files(files, profile_name)`): Java uses controller/service/repository/entity/config groups; Node uses component/hook/route/state/api/config groups; Python uses original api/model/storage/config groups. Used in Architecture and Test Quality Agent context packages.

**Skip detection** (`_detect_skipped_tests(diff, output, profile_name)`): Python: `@pytest.mark.skip`; Java: `@Disabled`, `@Ignore`; Node: `it.skip`, `xtest`, `describe.skip`, `.todo`.

### epic_breakdown

| Setting | Value |
|---|---|
| Max Stories per Epic | 8 |
| Output issue type | `Story` |
| Approval commands | `APPROVE <run_id>` / `REJECT <run_id>` / `REGENERATE <run_id>` |
| Idempotency guard | Blocks if the Epic already has Jira children |

### Clarification Loop

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

### GitHub Status Publishing

After `evaluate_release_decision()` stores its verdict, `publish_github_statuses_for_run()` in `app/github_status_publisher.py` publishes five GitHub commit statuses using the PR's `head_sha`. Publishing is guarded by `ensure_github_writes_allowed("status", ...)` and is **non-fatal** — failures log a warning and send a Telegram alert but never abort the workflow.

**Status contexts and mapping:**

| Context | Internal field | success when | failure when |
|---|---|---|---|
| `orchestrator/tests` | `test_status` | `PASSED` | `FAILED` / `NOT_RUN` |
| `orchestrator/reviewer-agent` | `review_status` | `APPROVED_BY_AI` | `NEEDS_CHANGES` / `BLOCKED` |
| `orchestrator/test-quality-agent` | `test_quality_status` | `TEST_QUALITY_APPROVED` | `TESTS_WEAK` / `TESTS_BLOCKING` |
| `orchestrator/architecture-agent` | `architecture_status` | `ARCHITECTURE_APPROVED` | `ARCHITECTURE_NEEDS_REVIEW` / `ARCHITECTURE_BLOCKED` |
| `orchestrator/release-gate` | `release_decision` | `RELEASE_APPROVED` | `RELEASE_BLOCKED` / `RELEASE_SKIPPED` |
| `orchestrator/deployment-validation` | `deployment_validation_status` | `PASSED` | `FAILED` / `ERROR` / `SKIPPED` |

`None` values map to `pending`; unknown values map to `error`. All mapping logic lives in `app/github_status_mapper.py` as pure functions.

**Constants** in `app/feedback.py`: `GitHubStatusContext`, `GitHubState`, `GITHUB_REQUIRED_CHECK = "orchestrator/release-gate"`.

**Branch protection:** Require only `orchestrator/release-gate` — it aggregates all agent verdicts. Setup instructions: `docs/security/github-required-checks.md`.

### Unified Release Gate

`evaluate_release_decision(mapping, final_test_result, applied, review_status, test_quality_status, architecture_status, first_use_mode_active=False) -> dict` (pure function in `workflows.py`)

Returns: `{release_decision, can_auto_merge, reason, blocking_gates, warnings}`

| Gate | BLOCKED if | SKIPPED if |
|---|---|---|
| Tests | `status == "FAILED"` | `status not in ("PASSED", "FAILED")` e.g. NOT_RUN |
| Reviewer | `BLOCKED` | `NEEDS_CHANGES` or `ERROR` |
| Test Quality | `TESTS_BLOCKING` | `TESTS_WEAK` or `ERROR` |
| Architecture | `ARCHITECTURE_BLOCKED` | `ARCHITECTURE_NEEDS_REVIEW` or `ERROR` |
| Auto-merge | — | `auto_merge_enabled=False` |
| File count | — | `count > 3` |
| First-use | — | `first_use_mode_active=True` (skips merge; checked via `is_first_use_mode_active()`) |
| Self-mod guard | always SKIPPED | repo_slug matches `ORCHESTRATOR_SELF_REPO` |

**Release feedback events:** `release_decision`, `release_blocking_gate_count`

### Post-Merge Deployment Validation

`_run_post_merge_validation(run_id, issue_key, repo_slug, commit_sha, pr_number, environment)` in `workflows.py` is called after a successful merge. It is **non-fatal and observational** — a FAILED result is recorded but never retroactively alters `release_decision`.

**Policy:** `_PROFILE_DEPLOYMENT_POLICY` in `workflows.py` — all profiles have `deployment_validation_required=False`. Exposes `get_deployment_policy_for_profile(profile_name)`.

**Kill switch:** `DEPLOYMENT_VALIDATION_ENABLED=false` skips all post-merge validation.

**Profile config:** `config/deployment_profiles.yaml` seeds `deployment_profiles` table on startup via `seed_deployment_profiles()`. YAML uses `ON CONFLICT DO UPDATE` — manual DB changes outside YAML-defined fields are preserved.

**Smoke test types:** HTTP only (GET/POST). `expected_status` required; `expected_contains` optional. Response bodies capped at 500 chars; no secret headers in DB.

**Telegram events:** `deployment_validation_passed` (status=COMPLETE), `deployment_validation_failed` (status=FAILED). `NOT_CONFIGURED` and `SKIPPED` are silent.

**Feedback events (FeedbackTypeP16):** `deployment_validation_status`, `deployment_validation_passed`, `deployment_validation_failed`, `deployment_validation_error`, `deployment_smoke_failure_count`. Written by `record_execution_feedback()`. Memory snapshot includes deployment validation bullet in `execution_guidance`.

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
| Prod | `13.206.186.168` | `main` | `self-hosted-prod` | `orchestrator.suyogjoshi.com` |

> **Prod IP is not an Elastic IP** — it changes on every instance stop/start. After a stop/start: update DNS A record, re-register Telegram webhook (`GET /debug/telegram/set-webhook`), and update the Jira webhook URL in the Jira admin panel to include `?token=<JIRA_WEBHOOK_SECRET>`.

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
- `docs/security/github-required-checks.md` — how to configure branch protection to require `orchestrator/release-gate`
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

## CI/CD

Two GitHub Actions workflows (`.github/workflows/`):
- `deploy-dev.yml` — triggers on push to `dev` → copies `/home/ubuntu/.env.orchestrator` → `.env` → prunes Docker builder cache → `docker compose up -d --build` → hits `/healthz`
- `deploy-main.yml` — same but triggers on push to `main` and targets the prod self-hosted runner

Both workflows run `docker builder prune -f` before the build to prevent disk exhaustion from accumulated layer cache (typically reclaims 700MB–1.4GB per deploy). Both workflows use self-hosted runners (see `self-hosted-dev` / `self-hosted-prod` labels in the Environment Model table). The health check hitting `/healthz` is the deploy success signal — the deploy fails if the container is unhealthy.

**Key dependency versions** (from `requirements.txt`): `anthropic==0.52.0`, `fastapi==0.115.12`, `redis==5.2.1`, `psycopg2-binary==2.9.10`. The anthropic SDK version determines which API features are available — check compatibility before upgrading.

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

## Admin Dashboard

Browser-based operations console at `/admin/ui`. Served as server-rendered HTML via Jinja2; no JavaScript framework.

**Auth design:**
- Login form POSTs the `ADMIN_API_KEY` value; on success a signed session cookie (`orchestrator_admin_session`) is set (8-hour TTL)
- CSRF token derived as `SHA256("csrf:{session_token}")[:32]` — passed as a hidden form field on all mutating POST forms
- `/admin/ui/*` paths are exempt from the header-key `BaseHTTPMiddleware` (controlled by `_UI_EXEMPT_PREFIX` in `security.py`); cookie auth is handled by `require_admin_ui()` in `app/ui_auth.py`
- The existing `X-Orchestrator-Admin-Key` header auth for API clients is unaffected

**Template structure:** `app/templates/admin/base.html` is the sidebar layout; all pages `{% extends "admin/base.html" %}`. `is_paused()` is registered as a Jinja2 global so `base.html` can show the PAUSED banner without route changes. The sidebar includes a "+ Onboard Repo" sub-link under Projects (`page == 'wizard'`) pointing to `/admin/ui/projects/new`.

**Dashboard DB functions** (in `app/database.py`):
- `get_overview_stats()` — single-query aggregated dashboard stats
- `list_workflow_runs_for_ui(status, workflow_type, issue_key, release_decision, limit)` — filterable runs list
- `get_workflow_run_detail(run_id)` — full run + all agent reviews + clarification + GitHub statuses
- `list_memory_snapshots(scope_type, scope_key)` — filterable memory listing
- `list_feedback_events(source_type, repo_slug, limit)` — feedback log

## Project Onboarding

`app/onboarding.py` — `run_project_onboarding(run_id, repo_slug, base_branch)`: 7-step pipeline executed by the worker's `_execute_onboarding()` path.

| Step | current_step value | What happens |
|---|---|---|
| 1 | cloning | `git clone --depth=1` into `/tmp/onboarding/<run_id>/repo` |
| 2 | profile_detection | `detect_repo_capability_profile()` → upserts `repo_capability_profiles` |
| 3 | command_validation | Runs test/build/lint commands; stores NOT_RUN for missing commands |
| 4 | structure_scan | `scan_repo_structure()` → stored as `structure_scan_json` |
| 5 | architecture_summary | Claude (`generate_onboarding_architecture_summary()`) → upserts `architecture` + `open_questions` snapshots |
| 6 | coding_conventions | Claude (`generate_onboarding_coding_conventions()`) → upserts `coding_conventions` snapshot |
| 7 | deployment_check | `_check_deployment_profile()` → upserts `deployment` snapshot; creates disabled draft if no profile exists |

Workspace: `/tmp/onboarding/<run_id>/` — cleaned up in `finally` block regardless of outcome.

**Project knowledge injection** (non-fatal): `get_project_knowledge_for_prompt(repo_slug)` in `app/database.py` fetches `architecture` + `coding_conventions` + `deployment` snapshots, returns a bounded string (≤5 bullets, ≤1200 chars). Injected into `suggest_memory` (story_implementation) and `memory_context` (epic_breakdown) before Claude calls. Wrapped in try/except so missing data never breaks existing workflows.

**Jira mapping helper**: `POST /admin/project-onboarding/{repo_slug}/create-jira-mapping` — creates a `repo_mappings` entry after onboarding; returns error if capability profile is missing or mapping already exists.

**Onboarding wizard**: `/admin/ui/projects/new` is a two-step guided UI — step 1 collects `repo_slug`, `base_branch`, `jira_space_key`, and `auto_merge`; on POST it checks for duplicate active mappings (blocks if the same repo already maps to a different Jira key), creates the `repo_mappings` entry, enqueues an onboarding job, and redirects to the status page. Step 2 (`/admin/ui/projects/new/status/{run_id}`) auto-refreshes every 5 s while PENDING/RUNNING and shows the 7 sub-step pills; on COMPLETED it displays the webhook JQL to copy into the Jira webhook filter.

**Dashboard**: `/admin/ui/projects` lists all repos with onboarding data; `/admin/ui/projects/{repo_slug}` shows latest run, capability profile, architecture, conventions, open questions, and deployment profile in a structured view.

## Deferred / Out of Scope

- Feature-level Jira hierarchy (locked: Epic → Story only, no Feature or Task levels)
- No code path should reference or route to a `feature_breakdown` workflow
- Global-scope memory (deferred — no cross-repo patterns exist yet)
- Run-scope memory injection (single-run signals not worth feeding back into the same run)
- Memory pruning / decay (snapshots are recomputed from raw events — no TTL needed)
- Semantic/vector search for memory retrieval (rule-based aggregation is sufficient)
- Multi-agent planning
