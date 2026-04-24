# PHASE 16 EXECUTION GUIDE — Deployment Validation & Smoke Testing

## 1. Objective

Add a **post-merge deployment validation layer**.

Current flow ends at:

```text
Release Gate approved
→ PR merged
```

Phase 16 should extend it to:

```text
Release Gate approved
→ PR merged
→ deployment validation starts
→ smoke tests run
→ deployment result stored
→ dashboard shows outcome
→ rollback/manual action guidance produced
```

Do **not** implement full production deployment automation yet. First implement **validation hooks and smoke testing**.

---

## 2. Scope

### In scope

* Repo deployment profile
* Environment-specific smoke test config
* Post-merge validation workflow
* HTTP smoke checks
* Optional command-based smoke checks
* Deployment status tracking
* GitHub status update for deployment validation
* Dashboard visibility
* Telegram notification
* Feedback/memory integration

### Out of scope

* Full blue/green deployment
* Automatic rollback
* Kubernetes/ECS deployment automation
* Terraform/CDK changes
* Cloud-provider-specific deployment orchestration
* Multi-region release management

---

## 3. New concept: Deployment Profile

Add per-repo deployment validation config.

Example:

```json
{
  "repo_slug": "suyog19/sandbox-fastapi-app",
  "environment": "dev",
  "deployment_type": "github_pages | huggingface_space | ec2_service | none",
  "base_url": "https://example.com",
  "healthcheck_path": "/healthz",
  "smoke_tests": [
    {
      "name": "healthcheck",
      "type": "http",
      "method": "GET",
      "path": "/healthz",
      "expected_status": 200,
      "expected_contains": "ok"
    }
  ],
  "enabled": true
}
```

---

## 4. Data model changes

### New table: `deployment_profiles`

```sql
id SERIAL PRIMARY KEY,
repo_slug VARCHAR(200) NOT NULL,
environment VARCHAR(50) NOT NULL,
deployment_type VARCHAR(100) NOT NULL,
base_url TEXT NULL,
healthcheck_path TEXT NULL,
smoke_tests_json TEXT NULL,
enabled BOOLEAN DEFAULT TRUE,
created_at TIMESTAMP DEFAULT NOW(),
updated_at TIMESTAMP DEFAULT NOW()
```

### New table: `deployment_validations`

```sql
id SERIAL PRIMARY KEY,
run_id INTEGER NOT NULL,
repo_slug VARCHAR(200) NOT NULL,
environment VARCHAR(50) NOT NULL,
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

Statuses:

```text
NOT_CONFIGURED
PENDING
RUNNING
PASSED
FAILED
ERROR
SKIPPED
```

### Extend `workflow_runs`

```text
deployment_validation_status
deployment_validation_summary
deployment_validation_completed_at
```

---

## 5. Config additions

```text
DEPLOYMENT_VALIDATION_ENABLED=true
DEPLOYMENT_VALIDATION_TIMEOUT_SECONDS=120
DEPLOYMENT_VALIDATION_RETRY_COUNT=3
DEPLOYMENT_VALIDATION_RETRY_DELAY_SECONDS=10
```

---

## 6. GitHub status context

Add one new GitHub status:

```text
orchestrator/deployment-validation
```

Mapping:

```text
PASSED         → success
FAILED         → failure
ERROR          → error
SKIPPED        → failure
NOT_CONFIGURED → pending or failure based on policy
```

Initial policy:

```text
NOT_CONFIGURED → pending
SKIPPED → failure
```

Do **not** make this required in branch protection yet. Observe first.

---

# 7. Iteration Plan

## Iteration 0 — Schema and constants

### Tasks

* Add `deployment_profiles`
* Add `deployment_validations`
* Add deployment columns to `workflow_runs`
* Add constants:

  * `DeploymentValidationStatus`
  * `DeploymentType`
  * `GitHubStatusContext.DEPLOYMENT_VALIDATION`

### Acceptance criteria

* migrations idempotent
* existing workflow unaffected
* dashboard still loads

Then STOP.

---

## Iteration 1 — Deployment profile CRUD/debug APIs

### Tasks

Add admin-protected endpoints:

```text
GET /debug/deployment-profiles
GET /debug/deployment-profiles/{repo_slug}
POST /debug/deployment-profiles
PUT /debug/deployment-profiles/{id}
```

Initial required fields:

```text
repo_slug
environment
deployment_type
base_url
healthcheck_path
enabled
smoke_tests_json
```

### Acceptance criteria

* profile can be created
* profile can be listed
* disabled profile is ignored
* invalid smoke test JSON rejected

Then STOP.

---

## Iteration 2 — Smoke test runner: HTTP checks

### Tasks

Create `app/deployment_validator.py`.

Implement:

```python
run_http_smoke_test(base_url, smoke_test, timeout_seconds) -> dict
```

Support:

```text
GET initially
expected_status
expected_contains optional
headers optional but no secrets
```

Return:

```json
{
  "name": "healthcheck",
  "status": "PASSED|FAILED|ERROR",
  "url": "...",
  "status_code": 200,
  "duration_ms": 123,
  "summary": "..."
}
```

### Acceptance criteria

* passing healthcheck works
* wrong status fails
* unreachable URL returns ERROR
* output contains no secrets

Then STOP.

---

## Iteration 3 — Deployment validation service

### Tasks

Implement:

```python
run_deployment_validation(run_id, repo_slug, environment, commit_sha=None, pr_number=None) -> dict
```

Behavior:

1. Load active `deployment_profile`
2. If none, store `NOT_CONFIGURED`
3. If disabled, store `SKIPPED`
4. Run smoke tests with retries
5. Store result in `deployment_validations`
6. Update `workflow_runs.deployment_validation_status`

### Acceptance criteria

* no profile → NOT_CONFIGURED
* disabled profile → SKIPPED
* all smoke tests pass → PASSED
* any smoke test fails after retries → FAILED
* exception → ERROR

Then STOP.

---

## Iteration 4 — Wire after successful merge

### Tasks

In story workflow:

```text
if merge_status == MERGED:
    run deployment validation
else:
    skip deployment validation
```

Important:

* Validation failure should not undo merge.
* Validation failure should be visible and loud.
* Send Telegram notification.

### Telegram examples

```text
[DEV] deployment_validation_passed
Run: 123
Repo: suyog19/sandbox-fastapi-app
Smoke tests: 2/2 passed
```

```text
[DEV] deployment_validation_failed
Run: 123
Repo: ...
Failed: healthcheck returned 500
```

### Acceptance criteria

* only merged runs trigger validation
* skipped/non-merged runs do not run validation
* Telegram notification sent

Then STOP.

---

## Iteration 5 — GitHub status publishing

### Tasks

Update GitHub status publisher to include:

```text
orchestrator/deployment-validation
```

Publish after deployment validation completes.

If validation happens after initial statuses, republish only deployment status or republish all statuses safely.

### Acceptance criteria

* PR commit shows deployment-validation status
* PASSED maps to success
* FAILED maps to failure
* status recorded in `github_status_updates`

Then STOP.

---

## Iteration 6 — Dashboard integration

### Tasks

Update admin UI.

### Run detail page

Add section:

```text
Deployment Validation
- Status
- Environment
- Base URL
- Smoke tests
- Started/completed
- Summary
```

### GitHub page

Show deployment-validation GitHub status.

### New page optional

```text
/admin/ui/deployments
```

List recent deployment validations.

### Acceptance criteria

* deployment status visible from run detail
* failed smoke tests readable
* no raw huge output displayed

Then STOP.

---

## Iteration 7 — Admin actions

### Tasks

Add endpoints:

```text
POST /debug/workflow-runs/{run_id}/run-deployment-validation
GET /debug/deployment-validations?run_id=&repo_slug=&status=
GET /debug/workflow-runs/{run_id}/deployment-validation
```

Dashboard button:

```text
Re-run Deployment Validation
```

### Acceptance criteria

* admin can rerun validation
* rerun stores a new validation row
* latest status reflected on workflow run
* admin key required

Then STOP.

---

## Iteration 8 — Profile seeding

### Tasks

Add optional config file:

```text
config/deployment_profiles.yaml
```

Example:

```yaml
profiles:
  - repo_slug: suyog19/sandbox-fastapi-app
    environment: dev
    deployment_type: ec2_service
    base_url: https://dev.example.com
    healthcheck_path: /healthz
    enabled: true
    smoke_tests:
      - name: healthcheck
        type: http
        method: GET
        path: /healthz
        expected_status: 200
```

Add idempotent seed function.

### Acceptance criteria

* profiles can be seeded on startup
* manual DB changes are not overwritten unless explicitly configured
* invalid config fails loudly at startup or logs clear warning

Then STOP.

---

## Iteration 9 — Deployment validation policy

### Tasks

Add policy:

```json
{
  "python_fastapi": {
    "deployment_validation_required": false
  },
  "java_maven": {
    "deployment_validation_required": false
  },
  "node_react": {
    "deployment_validation_required": false
  }
}
```

Initial recommendation:

```text
Deployment validation is optional/observational.
It should not block release gate yet.
```

Reason:

* post-merge validation happens after merge
* blocking should happen pre-merge
* deployment environments may be unavailable initially

### Acceptance criteria

* validation result is recorded
* it does not retroactively alter release decision
* future policy can promote it later

Then STOP.

---

## Iteration 10 — Feedback and memory integration

### Tasks

Add feedback events:

```text
deployment_validation_status
deployment_validation_passed
deployment_validation_failed
deployment_validation_error
deployment_smoke_failure_count
```

Memory snapshot integration:

```text
Deployment validation: 4 passed, 1 failed of 5 recent validations
```

Do not inject deployment memory into Developer Agent prompts yet unless clearly useful.

### Acceptance criteria

* feedback events recorded
* memory snapshot includes deployment summary
* prompt enrichment remains bounded

Then STOP.

---

## Iteration 11 — E2E validation

### Required scenarios

#### Scenario A — No profile

```text
merged run
→ deployment validation NOT_CONFIGURED
→ no crash
```

#### Scenario B — Disabled profile

```text
profile enabled=false
→ SKIPPED
```

#### Scenario C — Passing healthcheck

```text
health endpoint returns 200
→ PASSED
→ GitHub status success
```

#### Scenario D — Failing healthcheck

```text
endpoint returns 500 / wrong content
→ FAILED
→ Telegram alert
→ GitHub status failure
```

#### Scenario E — Rerun validation

```text
admin rerun
→ new validation row
→ latest status updated
```

#### Scenario F — Dashboard

```text
run detail shows validation result
```

### Acceptance criteria

* all scenarios pass
* merge flow not broken
* deployment failure visible in UI, Telegram, DB, GitHub status
* no automatic rollback attempted

Then STOP.

---

# 8. Security Notes

* Do not allow arbitrary smoke test commands from Jira.
* HTTP smoke tests only initially.
* If command-based smoke tests are added later, use predefined commands only.
* Do not store secret headers in DB initially.
* Do not print response bodies beyond safe excerpts.
* Admin rerun endpoint must require admin auth.
* Respect `ALLOW_GITHUB_WRITES` for GitHub status publishing.

---

# 9. Definition of Done

Phase 16 is complete when:

* deployment profiles exist
* smoke tests can be configured per repo/environment
* deployment validation runs after successful merge
* validation result is stored
* Telegram reports pass/fail
* GitHub deployment-validation status is published
* dashboard shows validation outcome
* admin can rerun validation
* feedback captures deployment outcomes
* no rollback is attempted
* no existing Phase 15 multi-stack behavior regresses

---

# 10. Final Instruction to Claude

Build Phase 16 as **post-merge validation**, not full deployment automation.

The key question:

```text
After the orchestrator merges code, can it verify that the target environment still looks healthy?
```

Optimize for:

* simple smoke checks
* clear validation status
* operator visibility
* safe reruns
* no rollback yet
* no arbitrary commands

Do not optimize for:

* complex deployment orchestration
* cloud-specific automation
* blue/green release
* Kubernetes/ECS integrations
* secret-bearing smoke requests

The standard for Phase 16:

> A merge is no longer the end of the story; the orchestrator must observe whether the deployed system still behaves correctly.
