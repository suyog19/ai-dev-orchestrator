# Phase 17 Summary — Real Project Onboarding & Repo Understanding

## Objective

Give the orchestrator a first-class understanding of repos it manages. Before this phase, the orchestrator treated every repo as a black box — it cloned, guessed commands from the capability profile, and hoped Claude would make reasonable changes. Phase 17 adds an explicit **project onboarding workflow** that deep-scans a repo once and stores structured knowledge that all future story_implementation and epic_breakdown runs can draw from.

---

## What Was Built

### New workflow: `project_onboarding`

`app/onboarding.py` — `run_project_onboarding(run_id, repo_slug, base_branch)`: a 7-step pipeline executed separately from story_implementation.

| Step | `current_step` | What happens |
|---|---|---|
| 1 | cloning | `git clone --depth=1` into `/tmp/onboarding/<run_id>/repo` |
| 2 | profile_detection | `detect_repo_capability_profile()` → upserts `repo_capability_profiles` |
| 3 | command_validation | Runs test/build/lint commands dry; stores `NOT_RUN` if no command |
| 4 | structure_scan | `scan_repo_structure()` → stored as `structure_scan_json` on the run |
| 5 | architecture_summary | Claude structured output → upserts `architecture` + `open_questions` snapshots |
| 6 | coding_conventions | Claude structured output → upserts `coding_conventions` snapshot |
| 7 | deployment_check | Checks or creates disabled deployment draft → upserts `deployment` snapshot |

Workspace is always cleaned up in the `finally` block.

### Repo Structure Scanner (`app/repo_scanner.py`)

`scan_repo_structure(workspace_path, profile_name) -> dict` — path-only file classification (no content reading). Produces categorized file lists (config, deploy, routing, model, service, test, doc) capped at 20 entries each. Returns total_files, source_file_count, test_file_count.

### Claude Onboarding Calls (`app/claude_client.py`)

Two new forced-tool-use functions:

**`generate_onboarding_architecture_summary(repo_path, repo_slug, structure_scan, profile)`**
- Reads up to 20 representative source files from the scan
- Tool schema: `architecture_summary`, `main_modules`, `entry_points`, `data_flow`, `test_strategy`, `deployment_notes`, `risks`, `open_questions`
- Stored as `architecture` + (if any) `open_questions` knowledge snapshots

**`generate_onboarding_coding_conventions(repo_path, repo_slug, structure_scan, profile)`**
- Reads routing, config, and test files
- Tool schema: `summary`, `naming_conventions`, `folder_organization`, `api_style`, `error_handling_style`, `test_naming_style`, `patterns_to_follow`, `patterns_to_avoid`
- Stored as `coding_conventions` knowledge snapshot

Both use ephemeral cache on the system prompt and `tool_choice={"type": "tool", "name": "..."}` for reliable structured output.

### Deployment Profile Check

`_check_deployment_profile(repo_slug, structure_scan, environment)` in `app/onboarding.py`:
- If a profile already exists → returns its status (`CONFIGURED_ENABLED` or `CONFIGURED_DISABLED`)
- If no profile → infers deployment type from file scan (Dockerfile, .github, Procfile, etc.) and creates a **disabled draft** with `upsert_deployment_profile()`
- Never auto-enables deployment validation
- Status values: `CONFIGURED_ENABLED | CONFIGURED_DISABLED | DRAFT_CREATED | NOT_CONFIGURED`

### Worker Integration

`_execute_onboarding(job)` added to `app/worker.py` — completely separate execution path from `_execute()`:
- Acquires `_semaphore` (same concurrency budget as story_implementation)
- Manages `project_onboarding_runs` status independently (`PENDING → RUNNING → COMPLETED | FAILED`)
- Main loop routes `workflow_type == "project_onboarding"` jobs here

`enqueue_onboarding_job(run_id, repo_slug, base_branch)` added to `app/queue.py`.

### New Endpoints (`app/main.py`)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/admin/project-onboarding/start` | Start onboarding for a repo |
| `GET` | `/admin/project-onboarding/runs` | List onboarding runs |
| `GET` | `/admin/project-onboarding/runs/{run_id}` | Single run detail |
| `POST` | `/admin/project-onboarding/{repo_slug}/create-jira-mapping` | Create repo mapping after onboarding |
| `GET` | `/debug/project-knowledge` | List knowledge snapshots for a repo |
| `POST` | `/debug/project-knowledge/{repo_slug}/refresh` | Placeholder for future refresh |

The `create-jira-mapping` endpoint validates that a capability profile exists and no mapping already exists before creating the `repo_mappings` row.

### Project Knowledge Injection into Workflows

`get_project_knowledge_for_prompt(repo_slug)` added to `app/database.py`:
- Fetches `architecture`, `coding_conventions`, and `deployment` snapshots
- Returns a bounded string: ≤5 bullets, ≤1200 chars total
- Returns `""` if no snapshots exist (no-op for repos without onboarding data)

Injection points — both wrapped in non-fatal try/except:
- **`story_implementation`**: appended to `suggest_memory` before `suggest_change()`
- **`epic_breakdown`**: appended to `memory_context` before epic planning Claude call

### Dashboard (`app/ui.py` + templates)

`/admin/ui/projects` — lists all repos with onboarding data (linked to detail page).

`/admin/ui/projects/{repo_slug}` — shows:
- Latest onboarding run with test/build/lint/deploy status
- Active capability profile with commands
- Architecture summary with main modules and risks
- Coding conventions with patterns to follow/avoid
- Open questions
- Deployment profile with recommendations
- Full onboarding run history table

### Schema Migration (`app/database.py`)

`structure_scan_json TEXT NULL` column added to `project_onboarding_runs` via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.

---

## Data Stored Per Repo

After a successful onboarding run, the following is persisted:

| Table | Key | What |
|---|---|---|
| `repo_capability_profiles` | repo_slug | Profile name, commands, capability flags |
| `project_knowledge_snapshots` | (repo_slug, `architecture`) | Claude architecture summary + modules/risks |
| `project_knowledge_snapshots` | (repo_slug, `open_questions`) | Unanswered architectural questions |
| `project_knowledge_snapshots` | (repo_slug, `coding_conventions`) | Patterns to follow/avoid |
| `project_knowledge_snapshots` | (repo_slug, `deployment`) | Deployment type, profile status, recommendations |
| `deployment_profiles` | (repo_slug, environment) | Disabled draft created if no profile exists |

---

## Validation

Onboarding of `suyog19/sandbox-fastapi-app` (run_id=7) confirmed:
- All 7 steps completed
- Capability profile detected: `python_fastapi`
- Architecture snapshot stored with real module names and entry points
- Coding conventions snapshot with Python/FastAPI patterns
- Deployment snapshot: `DRAFT_CREATED` (Docker detected, disabled draft created)
- Project knowledge injected into story prompts (non-fatal path confirmed working)
- Dashboard displays all sections correctly

---

## What This Enables

- Claude now has structured context about a repo's architecture and conventions when suggesting changes — reduces hallucinated file names and off-pattern code
- Onboarding is triggered once manually; future runs are enriched automatically with no extra API calls in the hot path
- The Jira mapping helper formalizes the onboarding-to-activation sequence: onboard → inspect dashboard → create mapping → start receiving Jira webhooks

---

## Deferred

- Automatic re-onboarding on significant codebase changes (no trigger exists yet)
- `POST /debug/project-knowledge/{repo_slug}/refresh` — endpoint exists as placeholder but does not re-run Claude calls
- Iteration 11–14 (Learning Platform real-world rehearsal) — requires user to provide repo slug and Jira project key
