# Phase 13 — GitHub-Native Checks & Branch Protection Enforcement

## Why this next?

Right now, your orchestrator internally knows whether a PR is safe:

```text
Tests passed?
Reviewer approved?
Test Quality approved?
Architecture approved?
Release Gate approved?
```

But GitHub itself does not yet enforce all those decisions as native required checks.

So Phase 13 should make GitHub reflect and enforce your orchestrator’s release judgment.

---

# PHASE 13 EXECUTION GUIDE — GitHub-Native Checks & Branch Protection Enforcement

## 1. Objective

Implement GitHub-native commit statuses / check runs for all major gates:

```text
tests
reviewer_agent
test_quality_agent
architecture_agent
release_gate
```

The goal:

```text
Orchestrator decision
→ GitHub check/status
→ Branch protection can require it
→ Manual merge cannot bypass AI gates accidentally
```

---

## 2. Target Flow

```text
Story implementation starts
→ PR branch created
→ tests run
→ Reviewer Agent runs
→ Test Quality Agent runs
→ Architecture Agent runs
→ Release Gate evaluates
→ orchestrator publishes GitHub checks
→ GitHub PR shows pass/fail/neutral statuses
→ branch protection can require release_gate
```

---

## 3. Why this matters

Current safety exists mostly inside orchestrator logic.

Phase 13 moves safety into GitHub too.

This protects against:

* accidental manual merge
* human ignoring PR comments
* future workflow changes bypassing release logic
* GitHub showing green when orchestrator is actually blocking

---

# 4. Scope

## In scope

1. GitHub status/check publishing abstraction
2. Per-agent GitHub checks
3. Release Gate GitHub check
4. Branch protection verification against required checks
5. Optional branch protection setup helper/documentation
6. Backfill checks for existing PRs/runs
7. Admin APIs to republish checks
8. E2E validation with real GitHub PRs

## Out of scope

Do NOT build:

* GitHub App migration
* OAuth app
* full dashboard
* deployment checks
* new AI agents
* multi-stack build matrix
* human approval UI in GitHub

---

# 5. Important Design Choice

Use **GitHub Commit Statuses first**, not full Check Runs.

Reason:

* simpler API
* works with PAT
* easier to require in branch protection
* enough for Phase 13

Later, you can upgrade to GitHub Checks API / GitHub App.

Use statuses like:

```text
orchestrator/tests
orchestrator/reviewer-agent
orchestrator/test-quality-agent
orchestrator/architecture-agent
orchestrator/release-gate
```

---

# 6. Status Mapping

GitHub commit status states:

```text
success
failure
pending
error
```

Map internal states:

## Tests

```text
PASSED  → success
FAILED  → failure
NOT_RUN → failure or error depending repo policy
ERROR   → error
```

## Reviewer Agent

```text
APPROVED_BY_AI → success
NEEDS_CHANGES  → failure
BLOCKED        → failure
ERROR          → error
```

## Test Quality Agent

```text
TEST_QUALITY_APPROVED → success
TESTS_WEAK            → failure
TESTS_BLOCKING        → failure
ERROR                 → error
```

## Architecture Agent

```text
ARCHITECTURE_APPROVED     → success
ARCHITECTURE_NEEDS_REVIEW → failure
ARCHITECTURE_BLOCKED      → failure
ERROR                     → error
```

## Release Gate

```text
RELEASE_APPROVED → success
RELEASE_SKIPPED  → failure
RELEASE_BLOCKED  → failure
ERROR/unknown    → error
```

---

# 7. Data Model Changes

## 7.1 New table: `github_status_updates`

```sql
id SERIAL PRIMARY KEY,
run_id INTEGER NOT NULL,
repo_slug VARCHAR(200) NOT NULL,
commit_sha VARCHAR(100) NOT NULL,
pr_number INTEGER NULL,
context VARCHAR(100) NOT NULL,
state VARCHAR(20) NOT NULL,
description TEXT NULL,
target_url TEXT NULL,
github_response_json TEXT NULL,
created_at TIMESTAMP DEFAULT NOW(),
updated_at TIMESTAMP DEFAULT NOW()
```

## 7.2 Extend `workflow_runs`

Add:

```text
head_sha VARCHAR(100) NULL
github_statuses_published BOOLEAN DEFAULT FALSE
github_statuses_published_at TIMESTAMP NULL
```

---

# 8. GitHub API Helper

Add function:

```python
create_commit_status(
    repo_slug: str,
    sha: str,
    state: str,
    context: str,
    description: str,
    target_url: str | None = None
) -> dict
```

GitHub endpoint:

```text
POST /repos/{owner}/{repo}/statuses/{sha}
```

Payload:

```json
{
  "state": "success",
  "context": "orchestrator/release-gate",
  "description": "Release approved: all gates passed",
  "target_url": "https://dev.orchestrator.../debug/workflow-runs/123/release-decision"
}
```

---

# 9. Status Publishing Function

Create:

```python
publish_github_statuses_for_run(run_id: int) -> dict
```

It should:

1. Load workflow run
2. Resolve repo slug
3. Resolve PR number
4. Resolve head SHA
5. Build statuses:

   * tests
   * reviewer
   * test-quality
   * architecture
   * release-gate
6. Publish each status
7. Store rows in `github_status_updates`
8. Mark `github_statuses_published=true`

---

# 10. Getting PR Head SHA

Add GitHub helper:

```python
get_pr_details(repo_slug, pr_number) -> dict
```

Return:

```json
{
  "number": 33,
  "head_sha": "...",
  "head_ref": "...",
  "base_ref": "main",
  "state": "open",
  "html_url": "..."
}
```

Store `head_sha` on `workflow_runs`.

---

# 11. Iteration Plan

## Iteration 0 — Schema and constants

### Goal

Prepare DB and constants.

### Tasks

* Add `github_status_updates`
* Add `workflow_runs.head_sha`
* Add `workflow_runs.github_statuses_published`
* Add `workflow_runs.github_statuses_published_at`
* Add constants for GitHub status contexts

Suggested constants:

```python
GITHUB_STATUS_TESTS = "orchestrator/tests"
GITHUB_STATUS_REVIEWER = "orchestrator/reviewer-agent"
GITHUB_STATUS_TEST_QUALITY = "orchestrator/test-quality-agent"
GITHUB_STATUS_ARCHITECTURE = "orchestrator/architecture-agent"
GITHUB_STATUS_RELEASE_GATE = "orchestrator/release-gate"
```

### Acceptance criteria

* migrations idempotent
* existing workflow still runs
* no status publishing yet

Then STOP.

---

## Iteration 1 — GitHub commit status API helper

### Goal

Create low-level GitHub status helper.

### Tasks

* Implement `create_commit_status(...)`
* Add error handling
* Never swallow HTTP errors silently
* Store/log response safely
* Add simple validation for allowed states

### Acceptance criteria

* static test can create a status on a known test commit
* invalid state rejected locally
* GitHub API error is readable

Then STOP.

---

## Iteration 2 — PR details / head SHA helper

### Goal

Resolve the correct commit SHA for PR status publishing.

### Tasks

* Implement `get_pr_details(repo_slug, pr_number)`
* Extract `head.sha`
* Store `head_sha` on `workflow_runs` after PR creation
* If missing during older runs, fetch on demand

### Acceptance criteria

* PR head SHA correctly fetched
* `workflow_runs.head_sha` populated
* no hardcoded SHA

Then STOP.

---

## Iteration 3 — Status mapper functions

### Goal

Map orchestrator verdicts to GitHub status states.

### Tasks

Create pure functions:

```python
map_test_status_to_github(test_status) -> dict
map_reviewer_status_to_github(review_status) -> dict
map_test_quality_status_to_github(test_quality_status) -> dict
map_architecture_status_to_github(architecture_status) -> dict
map_release_decision_to_github(release_decision) -> dict
```

Each returns:

```json
{
  "state": "success|failure|pending|error",
  "description": "...",
  "context": "orchestrator/..."
}
```

### Acceptance criteria

* unit/static tests cover all known verdict values
* unknown values map to `error`
* descriptions are short enough for GitHub

Then STOP.

---

## Iteration 4 — Publish statuses after Release Gate

### Goal

Publish all GitHub statuses after release decision.

### Tasks

* Implement `publish_github_statuses_for_run(run_id)`
* Call it after Release Gate decision is stored
* Publish:

  * tests
  * reviewer
  * test quality
  * architecture
  * release gate
* Store each result in `github_status_updates`

### Acceptance criteria

* PR commit shows all five statuses
* DB has five rows per completed run
* failure to publish status does not falsely mark run successful
* Telegram reports status publish failure if any

Recommended behavior:

* status publish failure should not change release decision
* but should be visible

Then STOP.

---

## Iteration 5 — Status update inspection APIs

### Goal

Make GitHub statuses inspectable.

Add endpoints:

```text
GET /debug/github-status-updates?run_id=...
GET /debug/workflow-runs/{run_id}/github-statuses
POST /debug/workflow-runs/{run_id}/republish-github-statuses
```

All require admin key.

### Acceptance criteria

* can inspect statuses by run
* can manually republish statuses
* republish is idempotent enough; duplicate GitHub statuses are acceptable but DB should record attempt

Then STOP.

---

## Iteration 6 — Branch protection audit upgrade

### Goal

Extend existing branch protection audit to verify required orchestrator statuses.

Current Phase 11 already added branch protection audit. Now enhance it.

Expected required contexts:

```text
orchestrator/release-gate
```

Optional required contexts:

```text
orchestrator/tests
orchestrator/reviewer-agent
orchestrator/test-quality-agent
orchestrator/architecture-agent
```

Recommendation:
Require only:

```text
orchestrator/release-gate
```

Reason:
Release Gate already aggregates all other checks.

### Tasks

* Update `/admin/github/branch-protection`
* Add `expected_required_contexts`
* Add warnings when `orchestrator/release-gate` is not required
* Document recommended GitHub setting

### Acceptance criteria

* audit detects missing release-gate required check
* audit passes when configured correctly

Then STOP.

---

## Iteration 7 — Branch protection setup guide

### Goal

Document how to configure GitHub branch protection.

Create:

```text
docs/security/github-required-checks.md
```

Include:

* recommended required status check:

  * `orchestrator/release-gate`
* optional individual statuses
* how to configure in GitHub UI
* how to verify via admin endpoint
* warning about manual merge bypass if not configured

### Acceptance criteria

* clear doc committed
* no secret values
* specific context names included

Then STOP.

---

## Iteration 8 — Optional branch protection enforcement endpoint

### Goal

Add read-only first, mutation optional.

Since GitHub branch protection mutation can be risky, start with **dry-run only**.

Add endpoint:

```text
POST /admin/github/branch-protection/validate-required-checks
```

Payload:

```json
{
  "repo_slug": "owner/repo",
  "branch": "main"
}
```

Returns:

```json
{
  "valid": false,
  "missing_required_contexts": ["orchestrator/release-gate"],
  "recommendations": [...]
}
```

### Acceptance criteria

* no mutation
* clear recommendations
* protected by admin key

Then STOP.

---

## Iteration 9 — Backfill statuses for recent runs

### Goal

Allow existing Phase 10/12 runs to publish GitHub statuses.

Add endpoint:

```text
POST /admin/github/statuses/backfill
```

Options:

```json
{
  "repo_slug": "owner/repo",
  "limit": 20,
  "only_missing": true
}
```

Behavior:

* find runs with PR URL and release decision
* publish statuses if missing
* skip runs without PR/head SHA

### Acceptance criteria

* recent eligible runs get statuses
* ineligible runs are reported as skipped with reason
* safe to rerun

Then STOP.

---

## Iteration 10 — E2E validation

### Required scenarios

#### Scenario A — all gates pass

Expected:

```text
all individual statuses = success
release-gate = success
```

#### Scenario B — Reviewer blocked

Expected:

```text
reviewer-agent = failure
release-gate = failure
```

#### Scenario C — Test Quality weak

Expected:

```text
test-quality-agent = failure
release-gate = failure
```

#### Scenario D — Architecture needs review

Expected:

```text
architecture-agent = failure
release-gate = failure
```

#### Scenario E — Agent error

Expected:

```text
specific agent = error
release-gate = failure or error
```

#### Scenario F — Manual republish

Expected:

```text
statuses republished
DB records attempt
PR status remains correct
```

#### Scenario G — Branch protection audit

Expected:

```text
missing release-gate required check produces warning
configured branch passes audit
```

### Acceptance criteria

* GitHub PR UI shows statuses
* branch protection audit detects required release-gate check
* release decision and GitHub status agree
* no existing Phase 12 clarification flow breaks

Then STOP.

---

# 12. Release Gate Status Description Rules

GitHub descriptions are short. Keep under 140 chars.

Examples:

```text
Tests passed: pytest -q
Reviewer approved PR
Test quality approved
Architecture approved
Release approved: all gates passed
Release blocked: architecture blocked
Release skipped: test quality weak
```

---

# 13. Security Notes

* All new admin endpoints require `X-Orchestrator-Admin-Key`
* Status publishing is a GitHub write action
* Use existing `ensure_github_writes_allowed("status", repo_slug, run_id)`
* Add `"status"` as allowed GitHub write action
* If `ALLOW_GITHUB_WRITES=false`, status publishing must be blocked and logged

---

# 14. Feedback / Memory Integration

Add feedback events:

```text
github_statuses_published
github_status_publish_failed
github_required_check_missing
```

Do not overuse in prompt memory yet.

This is operational signal, not coding guidance.

---

# 15. Definition of Done

Phase 13 is complete when:

* GitHub commit statuses are published for:

  * tests
  * Reviewer Agent
  * Test Quality Agent
  * Architecture Agent
  * Release Gate
* Release Gate status accurately reflects final orchestrator decision
* PR UI shows orchestrator statuses
* branch protection audit checks for required `orchestrator/release-gate`
* admin can republish statuses
* recent runs can be backfilled
* GitHub write guard applies to status publishing
* E2E scenarios pass
* existing clarification loop, review agents, and release gate remain intact

---

# 16. Final Instruction to Claude

Build Phase 13 as **GitHub-native enforcement**, not another internal review feature.

The orchestrator already knows the truth.

Now GitHub must see that truth.

Optimize for:

* required-check compatibility
* simple commit statuses
* branch protection audit
* release-gate as the single required context
* safe republish/backfill

Do not optimize for:

* GitHub App migration
* full Checks API
* fancy annotations
* line-level comments
* dashboard UI

The standard for Phase 13:

> If GitHub shows a PR as mergeable, it must be because the orchestrator’s Release Gate says it is safe.
