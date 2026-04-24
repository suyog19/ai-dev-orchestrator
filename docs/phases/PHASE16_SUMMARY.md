# Phase 16 Summary — Deployment Validation & Smoke Testing

## Objective

Extend the post-merge workflow to observe whether the deployed target environment still behaves correctly after the orchestrator merges a PR. This phase introduces **post-merge deployment validation** — not full deployment automation. The validation is strictly observational: a FAILED result is recorded and surfaced but never retroactively alters the `release_decision` set before merge.

---

## What Was Built

### New concept: Deployment Profile

Per-repo, per-environment configuration that controls which smoke tests to run and how to reach the deployment target.

- Stored in `deployment_profiles` table (unique on `repo_slug + environment`)
- Fields: `deployment_type` (ec2_service | github_pages | huggingface_space | none), `base_url`, `healthcheck_path`, `smoke_tests_json`, `enabled`
- Seeded idempotently on startup from `config/deployment_profiles.yaml` via `seed_deployment_profiles()`
- Manual DB changes are preserved: upsert only sets fields defined in YAML

### Smoke Test Runner (`app/deployment_validator.py`)

`run_http_smoke_test(base_url, smoke_test, timeout_seconds)` — HTTP GET/POST checks with:
- `expected_status` validation
- Optional `expected_contains` body check
- Returns: `{name, status, url, status_code, duration_ms, summary}`
- Status values: `PASSED | FAILED | ERROR`
- Response bodies capped at 500 chars; no secret headers stored

`run_deployment_validation(run_id, repo_slug, environment, ...)`:
1. Load profile; if none → `NOT_CONFIGURED`
2. If disabled → `SKIPPED`
3. If no `base_url` → `ERROR`
4. If no smoke tests → `SKIPPED`
5. Run each test with retries; aggregate → `PASSED` or `FAILED`
6. Store result in `deployment_validations`; update `workflow_runs.deployment_validation_status`

### Post-Merge Wiring (`app/workflows.py`)

`_run_post_merge_validation(run_id, issue_key, repo_slug, commit_sha, pr_number, environment)` is called after `merge_status == MERGED` in both `story_implementation` and `_story_review_and_release`. It is **non-fatal**: any exception is caught and logged; validation failure never aborts or reverses the merge.

Env var kill switch: `DEPLOYMENT_VALIDATION_ENABLED=false` skips all validation.

### Deployment Validation Policy (`_PROFILE_DEPLOYMENT_POLICY`)

Per-profile policy dict added alongside `_PROFILE_RELEASE_POLICY`. All profiles currently have `deployment_validation_required: False` — validation is observational. The policy is explicit in code so it can be promoted to `required: True` per profile when environments are stable. Exposed via `GET /debug/deployment-policy`.

### GitHub Status Publishing

Sixth GitHub commit status context added: `orchestrator/deployment-validation`

| Internal status | GitHub state |
|---|---|
| `PASSED` | `success` |
| `FAILED` | `failure` |
| `ERROR` | `error` |
| `SKIPPED` | `failure` |
| `NOT_CONFIGURED` / `None` | `pending` |

Published by `publish_deployment_validation_status()` in `app/github_status_publisher.py`. Guarded by `ensure_github_writes_allowed()`. Non-fatal. Recorded in `github_status_updates`.

### Dashboard Integration

**Run detail page** (`/admin/ui/runs/{run_id}`): Deployment Validation section shows status pill, environment, commit SHA, completed timestamp, smoke results table (name, status, HTTP code, duration_ms, summary), and "↻ Re-run Validation" button.

**New page** (`/admin/ui/deployments`): Configured profiles table + recent validations list with run links and status pills. Added "Deployments" link to sidebar.

### Admin Actions

- `GET /debug/deployment-profiles` — list profiles (filter by repo_slug)
- `GET /debug/deployment-profiles/{repo_slug}` — get by repo + environment
- `POST /debug/deployment-profiles` — create profile
- `PUT /debug/deployment-profiles/{id}` — field update
- `GET /debug/deployment-validations` — list validations (filter: run_id, repo_slug, status, limit)
- `GET /debug/workflow-runs/{run_id}/deployment-validation` — run-scoped read
- `POST /debug/workflow-runs/{run_id}/run-deployment-validation` — admin re-run (new row each time; latest status reflected on `workflow_runs`)
- `GET /debug/deployment-policy` — deployment validation policy per profile
- `POST /admin/ui/runs/{run_id}/run-deployment-validation` — CSRF-protected re-run form

### Feedback & Memory Integration

`record_execution_feedback()` now reads `deployment_validation_status` from `workflow_runs` and emits `FeedbackTypeP16` events:
- `deployment_validation_status` — raw status value
- `deployment_validation_passed` — `"true"` when PASSED
- `deployment_validation_failed` — `"true"` when FAILED
- `deployment_validation_error` — `"true"` when ERROR
- `deployment_smoke_failure_count` — count of failed smoke tests (from `deployment_validations.smoke_results_json`)

`generate_repo_memory_snapshot()` now queries deployment validation counts from `feedback_events` and adds a bullet to `execution_guidance`:

```
- Deployment validation: 4 passed, 1 failed, 0 error of 5 recent validations
```

---

## New Tables

### `deployment_profiles`

```sql
id SERIAL PRIMARY KEY,
repo_slug VARCHAR(200) NOT NULL,
environment VARCHAR(50) NOT NULL DEFAULT 'dev',
deployment_type VARCHAR(100) NOT NULL,
base_url TEXT NULL,
healthcheck_path TEXT NULL,
smoke_tests_json TEXT NULL,
enabled BOOLEAN DEFAULT TRUE,
created_at TIMESTAMP DEFAULT NOW(),
updated_at TIMESTAMP DEFAULT NOW(),
UNIQUE (repo_slug, environment)
```

### `deployment_validations`

```sql
id SERIAL PRIMARY KEY,
run_id INTEGER NOT NULL REFERENCES workflow_runs(id),
repo_slug VARCHAR(200) NOT NULL,
environment VARCHAR(50) NOT NULL DEFAULT 'dev',
commit_sha VARCHAR(100) NULL,
pr_number INTEGER NULL,
deployment_profile_id INTEGER NULL,
status VARCHAR(50) NOT NULL,
summary TEXT NULL,
smoke_results_json TEXT NULL,
started_at TIMESTAMP NULL,
completed_at TIMESTAMP NULL,
created_at TIMESTAMP DEFAULT NOW()
```

### New `workflow_runs` columns

- `deployment_validation_status VARCHAR(50)` — latest validation status
- `deployment_validation_summary TEXT` — human-readable summary
- `deployment_validation_completed_at TIMESTAMP`

---

## New Constants (`app/feedback.py`)

```python
class GitHubStatusContext:
    DEPLOYMENT_VALIDATION = "orchestrator/deployment-validation"  # 6th context

class DeploymentType:
    GITHUB_PAGES = "github_pages"
    HUGGINGFACE  = "huggingface_space"
    EC2_SERVICE  = "ec2_service"
    NONE         = "none"

class DeploymentValidationStatus:
    NOT_CONFIGURED = "NOT_CONFIGURED"
    PENDING        = "PENDING"
    RUNNING        = "RUNNING"
    PASSED         = "PASSED"
    FAILED         = "FAILED"
    ERROR          = "ERROR"
    SKIPPED        = "SKIPPED"

class FeedbackTypeP16:
    DEPLOYMENT_VALIDATION_STATUS    = "deployment_validation_status"
    DEPLOYMENT_VALIDATION_PASSED    = "deployment_validation_passed"
    DEPLOYMENT_VALIDATION_FAILED    = "deployment_validation_failed"
    DEPLOYMENT_VALIDATION_ERROR     = "deployment_validation_error"
    DEPLOYMENT_SMOKE_FAILURE_COUNT  = "deployment_smoke_failure_count"
```

---

## New Config (`config/deployment_profiles.yaml`)

```yaml
profiles:
  - repo_slug: suyog19/sandbox-fastapi-app
    environment: dev
    deployment_type: ec2_service
    base_url: ""           # set to real URL when service is reachable from orchestrator
    healthcheck_path: /healthz
    enabled: false         # disabled until base_url is configured
    smoke_tests:
      - name: healthcheck
        type: http
        method: GET
        path: /healthz
        expected_status: 200
        expected_contains: "ok"
```

---

## New Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `DEPLOYMENT_VALIDATION_ENABLED` | `true` | Kill switch — set `false` to disable all validation |
| `DEPLOYMENT_VALIDATION_TIMEOUT_SECONDS` | `120` | Per-smoke-test HTTP timeout |
| `DEPLOYMENT_VALIDATION_RETRY_COUNT` | `3` | Retries per smoke test before declaring FAILED |
| `DEPLOYMENT_VALIDATION_RETRY_DELAY_SECONDS` | `10` | Delay between retries |

---

## Iteration Log

| Iteration | Scope | Status |
|---|---|---|
| 0 | Schema & constants (3 tables, new columns, constants) | Done |
| 1 | Deployment profile CRUD/debug APIs | Done |
| 2 | Smoke test runner — HTTP checks | Done |
| 3 | Deployment validation service | Done |
| 4 | Wire after successful merge | Done |
| 5 | GitHub status publishing | Done |
| 6 | Dashboard integration | Done |
| 7 | Admin actions (rerun, re-list, UI button) | Done |
| 8 | Profile YAML seeding | Done |
| 9 | Deployment validation policy | Done |
| 10 | Feedback & memory integration | Done |
| 11 | E2E validation (scenarios A-F) | Done |

---

## E2E Scenarios Validated

| Scenario | Description | Result |
|---|---|---|
| A | No profile → `NOT_CONFIGURED`, no crash | PASS |
| B | `enabled=false` profile → `SKIPPED` | PASS |
| C | Real `/healthz` returns 200 → `PASSED`, GitHub status success | PASS |
| D | Wrong `expected_status` → `FAILED`, Telegram alert wired | PASS |
| E | Admin rerun → new `deployment_validations` row, latest status updated | PASS |
| F | Dashboard: run detail shows validation, `/admin/ui/deployments` renders | PASS |

---

## Security Notes

- HTTP smoke tests only — no arbitrary shell commands
- No secret headers stored in DB
- Response bodies capped at 500 chars in smoke results
- Admin rerun endpoint protected by `X-Orchestrator-Admin-Key`
- `ALLOW_GITHUB_WRITES` respected before publishing deployment status
- `DEPLOYMENT_VALIDATION_ENABLED=false` globally disables all validation

---

## What Was Not Built (Out of Scope)

- Full blue/green deployment automation
- Automatic rollback on validation failure
- Kubernetes/ECS/cloud-specific orchestration
- Multi-region release management
- Command-based smoke tests (HTTP only for now)
- Promotion of `deployment_validation_required` to `True` for any profile

---

## Upgrade Notes

- Profiles in `config/deployment_profiles.yaml` are seeded idempotently — running `init_db()` twice does not duplicate rows
- The `deployment_profiles` table has a `UNIQUE(repo_slug, environment)` constraint; `ON CONFLICT DO UPDATE` is used
- The seed uses `ON CONFLICT DO UPDATE` so manual DB changes to fields NOT in the YAML are preserved
- To enable validation for `suyog19/sandbox-fastapi-app`: set `base_url` to the real URL and set `enabled: true` in the YAML, then redeploy
