
---

# PHASE 10 EXECUTION GUIDE — Architecture/Impact Agent + Unified Release Gate

## 1. Objective

Phase 10 adds a third independent agent:

```text
Architecture / Impact Agent
```

This agent checks:

* design impact
* dependency impact
* API/schema compatibility
* cross-file side effects
* risky architectural drift
* whether the change is too large for the Story

Then Phase 10 adds a central **Release Gate** that combines:

```text
Test Runner
Reviewer Agent
Test Quality Agent
Architecture Agent
```

into one final merge decision.

---

## 2. Target flow

```text
Developer Agent implements Story
→ tests run
→ PR created
→ Reviewer Agent reviews code/story alignment
→ Test Quality Agent reviews test adequacy
→ Architecture Agent reviews design/system impact
→ Unified Release Gate evaluates all signals
→ merge / skip / block with clear reason
```

---

## 3. Why Phase 10 should be bigger

You now have enough stability to stop adding isolated checks one by one.

Phase 10 should create a clearer architecture:

```text
Agents produce verdicts
Release Gate decides
```

This prevents merge logic from becoming scattered across workflow code.

---

# Part A — Architecture / Impact Agent

## 4. Agent responsibility

Architecture Agent evaluates whether a PR is safe from a design and system-impact perspective.

It should check:

### 4.1 Scope discipline

* Is the change limited to the Story?
* Did it introduce unrelated refactoring?
* Did it touch too many layers?

### 4.2 API compatibility

* Did request/response shape change?
* Did endpoint behavior change?
* Is it backward-compatible?

### 4.3 Data/model impact

* Any schema/model change?
* Any migration needed?
* Any risk to stored data?

### 4.4 Dependency impact

* New dependency added?
* Existing dependency changed?
* Version risk?

### 4.5 Operational impact

* Config changes?
* Environment variable changes?
* Deployment/runtime impact?

### 4.6 Security impact

* Auth/permission touched?
* Input validation changed?
* Sensitive data exposure risk?

### 4.7 Maintainability

* Is the design coherent?
* Any duplication?
* Any future maintenance concern?

---

## 5. Verdicts

Architecture Agent returns:

```text
ARCHITECTURE_APPROVED
ARCHITECTURE_NEEDS_REVIEW
ARCHITECTURE_BLOCKED
ERROR
```

Meaning:

```text
ARCHITECTURE_APPROVED
→ No meaningful architecture/system risk detected.

ARCHITECTURE_NEEDS_REVIEW
→ Potential design or maintainability concern; human should review before merge.

ARCHITECTURE_BLOCKED
→ High-risk design/system issue; auto-merge blocked.

ERROR
→ Internal fallback if the agent fails.
```

Claude can only return:

```text
ARCHITECTURE_APPROVED
ARCHITECTURE_NEEDS_REVIEW
ARCHITECTURE_BLOCKED
```

`ERROR` is internal only.

---

## 6. Data model

### New table: `agent_architecture_reviews`

```sql
id SERIAL PRIMARY KEY,
run_id INTEGER NOT NULL REFERENCES workflow_runs(id),
pr_number INTEGER NULL,
pr_url VARCHAR(500) NULL,
repo_slug VARCHAR(200) NULL,
story_key VARCHAR(100) NULL,
agent_name VARCHAR(100) NOT NULL DEFAULT 'architecture_agent',
architecture_status VARCHAR(50) NOT NULL,
risk_level VARCHAR(20) NULL,
summary TEXT NULL,
impact_areas_json TEXT NULL,
blocking_reasons_json TEXT NULL,
recommendations_json TEXT NULL,
model_used VARCHAR(100) NULL,
memory_snapshot_ids_json TEXT NULL,
created_at TIMESTAMP DEFAULT NOW(),
updated_at TIMESTAMP DEFAULT NOW()
```

### Extend `workflow_runs`

Add:

```text
architecture_status
architecture_required
architecture_completed_at
architecture_summary
release_decision
release_decision_reason
release_decided_at
```

---

## 7. Constants

Add:

```python
class AgentName:
    ARCHITECTURE_AGENT = "architecture_agent"

class ArchitectureStatus:
    APPROVED = "ARCHITECTURE_APPROVED"
    NEEDS_REVIEW = "ARCHITECTURE_NEEDS_REVIEW"
    BLOCKED = "ARCHITECTURE_BLOCKED"
    ERROR = "ERROR"

class ArchitectureRiskLevel:
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
```

Add config:

```python
ARCHITECTURE_REVIEW_REQUIRED = True
ARCHITECTURE_REVIEW_BLOCKS_MERGE = True
```

---

## 8. Architecture Agent input contract

Build package:

```json
{
  "story": {
    "key": "KAN-24",
    "summary": "...",
    "description": "...",
    "acceptance_criteria": []
  },
  "repo": {
    "repo_slug": "suyog19/sandbox-fastapi-app",
    "primary_language": "python",
    "framework": "fastapi"
  },
  "pr": {
    "number": 33,
    "url": "...",
    "title": "...",
    "body": "..."
  },
  "diff": {
    "changed_files": [],
    "full_diff": "..."
  },
  "signals": {
    "test_status": "PASSED",
    "review_status": "APPROVED_BY_AI",
    "test_quality_status": "TEST_QUALITY_APPROVED",
    "files_changed_count": 2,
    "retry_count": 0
  },
  "memory": {
    "execution_guidance": "...",
    "manual_notes": "..."
  }
}
```

---

## 9. Architecture Agent output contract

Claude must return structured output:

```json
{
  "architecture_status": "ARCHITECTURE_APPROVED",
  "risk_level": "LOW",
  "summary": "The change is scoped and does not introduce architecture risk.",
  "impact_areas": [
    {
      "area": "api",
      "risk": "LOW",
      "finding": "Adds a backward-compatible endpoint."
    }
  ],
  "blocking_reasons": [],
  "recommendations": [
    "Consider documenting the new endpoint in a future documentation story."
  ]
}
```

Rules:

* `ARCHITECTURE_BLOCKED` if auth/security/data compatibility is unsafe.
* `ARCHITECTURE_BLOCKED` if change contradicts Story or introduces large unrelated redesign.
* `ARCHITECTURE_NEEDS_REVIEW` for medium-risk design concerns.
* `ARCHITECTURE_APPROVED` only for low-risk, scoped changes.

---

# Part B — Unified Release Gate

## 10. Problem to solve

Current merge gate checks several conditions inline:

```text
tests passed
Reviewer approved
Test Quality approved
auto_merge_enabled
file count threshold
```

Phase 10 should move this into a dedicated release decision function.

---

## 11. Release decision statuses

Add:

```text
RELEASE_APPROVED
RELEASE_SKIPPED
RELEASE_BLOCKED
RELEASE_ERROR
```

Suggested merge status mapping:

```text
RELEASE_APPROVED → MERGED or READY_TO_MERGE
RELEASE_BLOCKED → BLOCKED_BY_RELEASE_GATE
RELEASE_SKIPPED → SKIPPED
RELEASE_ERROR → SKIPPED
```

---

## 12. Release Gate logic

Create function:

```python
evaluate_release_decision(run_id, mapping, final_test_result, applied, review_status, test_quality_status, architecture_status) -> dict
```

Output:

```json
{
  "release_decision": "RELEASE_APPROVED",
  "can_auto_merge": true,
  "reason": "All gates passed",
  "blocking_gates": [],
  "warnings": []
}
```

Gate rules:

```text
Block if:
- tests failed
- Reviewer Agent BLOCKED
- Test Quality Agent TESTS_BLOCKING
- Architecture Agent ARCHITECTURE_BLOCKED

Skip if:
- auto_merge disabled
- tests NOT_RUN
- Reviewer NEEDS_CHANGES
- Test Quality TESTS_WEAK
- Architecture NEEDS_REVIEW
- agent ERROR
- file count threshold exceeded

Approve if:
- auto_merge_enabled
- tests PASSED
- Reviewer APPROVED_BY_AI
- Test Quality TEST_QUALITY_APPROVED
- Architecture ARCHITECTURE_APPROVED
- changed file count <= threshold
```

---

# Phase 10 Iteration Plan

## Iteration 0 — Schema and constants baseline

### Goal

Prepare database and constants.

### Tasks

* Add `agent_architecture_reviews`
* Extend `workflow_runs`
* Add architecture constants
* Add release decision constants
* Add config flags

### Acceptance criteria

* migrations idempotent
* existing story pipeline still works
* no behavior change yet

Then STOP.

---

## Iteration 1 — Architecture Agent client

### Goal

Create the Architecture Agent function without wiring.

### Tasks

Add:

```python
review_architecture(
    story_context,
    repo_context,
    pr_context,
    diff_context,
    signal_context,
    memory_context
) -> dict
```

Use forced tool output.

### Acceptance criteria

* static sample returns structured verdict
* malformed/no-tool output fails clearly
* no DB/GitHub writes yet

Then STOP.

---

## Iteration 2 — Architecture review package

### Goal

Assemble the input context.

### Tasks

Create `_build_architecture_review_package(...)`.

Collect:

* Story context
* PR context
* changed files
* diff
* test status
* Reviewer verdict
* Test Quality verdict
* retry count
* files changed count
* repo memory

Add lightweight file classification:

* API/router files
* model/schema files
* storage/db files
* config/env files
* tests
* docs

### Acceptance criteria

* package logs safely
* file categories detected correctly
* no secrets included

Then STOP.

---

## Iteration 3 — Run Architecture Agent after Test Quality Agent

### Goal

Wire the new agent into workflow but do not block merge yet.

Flow:

```text
Reviewer Agent
→ Test Quality Agent
→ Architecture Agent
```

### Tasks

* call `review_architecture(...)`
* store result in `agent_architecture_reviews`
* update `workflow_runs.architecture_status`
* send Telegram event `architecture_review_completed`

### Acceptance criteria

* every PR gets architecture review row
* Telegram shows verdict
* merge behavior unchanged

Then STOP.

---

## Iteration 4 — Post Architecture Agent comment to PR

### Goal

Make architecture verdict visible in GitHub.

Comment format:

```md
## Architecture Agent Verdict: ✅ ARCHITECTURE_APPROVED

**Risk:** LOW

### Summary
...

### Impact Areas
- API: LOW — Adds backward-compatible endpoint.

### Blocking Reasons
None

### Recommendations
- ...
```

Emoji:

* ✅ approved
* ⚠️ needs review
* 🚫 blocked
* ❌ error

### Acceptance criteria

* PR contains Architecture Agent comment
* comment matches DB verdict
* comment failure does not corrupt workflow state

Then STOP.

---

## Iteration 5 — Build Unified Release Gate function

### Goal

Centralize merge decision.

### Tasks

Create:

```python
evaluate_release_decision(...)
```

It should return:

* decision
* can_auto_merge
* reason
* blocking gates
* warnings

Do not wire it into merge yet.

### Acceptance criteria

* unit/static tests cover:

  * all approved
  * reviewer blocked
  * test quality weak
  * architecture needs review
  * tests failed
  * auto-merge disabled
  * agent error

Then STOP.

---

## Iteration 6 — Replace inline merge gate with Release Gate

### Goal

Make Release Gate authoritative.

### Tasks

* Replace current inline merge condition with `evaluate_release_decision`
* Store release decision fields on `workflow_runs`
* Keep existing merge behavior equivalent where possible
* Add `BLOCKED_BY_RELEASE_GATE` if needed

### Acceptance criteria

* previous success path still merges
* previous reviewer/test-quality blocked paths still block
* release decision reason visible in DB/logs/Telegram

Then STOP.

---

## Iteration 7 — Make Architecture Agent block merge

### Goal

Turn architecture verdict into real gate.

Rules:

* `ARCHITECTURE_APPROVED` → may merge if other gates pass
* `ARCHITECTURE_NEEDS_REVIEW` → skip auto-merge
* `ARCHITECTURE_BLOCKED` → block merge
* `ERROR` → skip auto-merge

### Acceptance criteria

* architecture approved allows merge
* needs review skips
* blocked blocks
* error skips safely

Then STOP.

---

## Iteration 8 — Feedback event integration

### Goal

Feed architecture and release decisions into memory.

Add feedback events:

```text
architecture_status
architecture_risk_level
architecture_approved
architecture_needs_review
architecture_blocked
release_decision
release_blocking_gate_count
```

Update memory snapshot summary to include:

* architecture approval/block rates
* common architecture risk areas
* release gate block reasons

### Acceptance criteria

* feedback events emitted
* memory snapshots include architecture/release signals
* prompt enrichment remains bounded

Then STOP.

---

## Iteration 9 — Inspection APIs

### Goal

Make architecture and release decisions inspectable.

Add:

```text
GET /debug/architecture-reviews?limit=N
GET /debug/workflow-runs/{run_id}/architecture
GET /debug/workflow-runs/{run_id}/release-decision
```

Optional:

```text
GET /debug/architecture-reviews/{id}
```

### Acceptance criteria

* verdicts visible without SSH
* filter by:

  * run_id
  * repo_slug
  * architecture_status
* release decision visible per run

Then STOP.

---

## Iteration 10 — Negative-path validation

### Required scenarios

#### Scenario A — Low-risk change

Expected:

```text
ARCHITECTURE_APPROVED
```

#### Scenario B — Medium-risk design concern

Example:

* touches model + storage + API
* tests pass but design is broader than Story

Expected:

```text
ARCHITECTURE_NEEDS_REVIEW
```

#### Scenario C — High-risk blocked change

Example:

* auth/security change unrelated to Story
* response contract broken
* config/env change without explanation

Expected:

```text
ARCHITECTURE_BLOCKED
```

#### Scenario D — Architecture Agent error

Expected:

```text
ERROR
release decision = RELEASE_SKIPPED
merge_status = SKIPPED
```

### Acceptance criteria

* all scenarios behave predictably
* DB/API/Telegram/GitHub agree

Then STOP.

---

## Iteration 11 — End-to-end Phase 10 validation

### Required scenarios

#### Scenario A — Clean merge

```text
Tests PASSED
Reviewer APPROVED
Test Quality APPROVED
Architecture APPROVED
Release APPROVED
Auto-merge MERGED
```

#### Scenario B — Reviewer blocks

```text
Reviewer BLOCKED
Release BLOCKED
No merge
```

#### Scenario C — Test Quality weak

```text
TQ TESTS_WEAK
Release SKIPPED
PR left open
```

#### Scenario D — Architecture needs review

```text
Architecture NEEDS_REVIEW
Release SKIPPED
PR left open
```

#### Scenario E — Architecture blocked

```text
Architecture BLOCKED
Release BLOCKED
No merge
```

#### Scenario F — Agent error

```text
Architecture ERROR
Release SKIPPED
No merge
```

### Acceptance criteria

* Architecture Agent independent
* Release Gate authoritative
* PR has three agent comments:

  * Reviewer
  * Test Quality
  * Architecture
* merge decision is centralized
* memory captures architecture/release outcomes

Then STOP.

---

# 16. Final instruction to Claude

Build Phase 10 as a **larger architecture step**, not just another review call.

The goal is:

```text
Independent agents produce verdicts.
Unified Release Gate makes final merge decision.
```

Do not scatter merge logic across workflow steps.

Optimize for:

* independent Architecture Agent judgment
* centralized release decision
* clear blocking reasons
* auditability
* future extensibility for Release Agent later

Do not optimize for:

* generic multi-agent framework
* huge architecture essays
* line-level comments
* production deployment automation

The standard for Phase 10:

> Can the system detect architecture-level risk and make one clear, centralized release decision from all agent gates?
