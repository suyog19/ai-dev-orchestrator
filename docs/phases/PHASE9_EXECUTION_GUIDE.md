
---

# PHASE 9 EXECUTION GUIDE — Test Quality Agent

## 1. Phase 9 Objective

Phase 8 successfully extracted the **Reviewer Agent** as an independent gate before auto-merge. The Developer Agent no longer judges its own output; Reviewer Agent now reviews story alignment, code quality, test awareness, and diff risk. 

Phase 9 extracts the next responsibility:

> **Test Quality Agent — an independent agent that decides whether the tests are good enough to trust the change.**

The Test Runner only answers:

```text
Did tests pass?
```

The Test Quality Agent answers:

```text
Do these tests meaningfully validate the Story?
```

---

## 2. Target Phase 9 Flow

```text
Story implementation completes
→ tests run
→ PR created
→ Reviewer Agent runs
→ Test Quality Agent runs
→ both verdicts are stored
→ both comments are posted to GitHub PR
→ merge gate checks:
     tests PASSED
     Reviewer Agent APPROVED_BY_AI
     Test Quality Agent TEST_QUALITY_APPROVED
→ merge or block/skip
```

---

## 3. Explicit Non-Goals

Do NOT build in Phase 9:

* Architect Agent
* Release Agent
* full multi-agent framework
* line-level GitHub review comments
* code coverage tooling
* mutation testing
* automatic test generation agent
* autonomous human approval override

This phase is only about **test adequacy judgment**.

---

## 4. Test Quality Agent Responsibilities

Evaluate:

1. **Acceptance criteria coverage**

   * Do tests map to Story acceptance criteria?
   * Are key user-visible behaviours tested?

2. **Changed behaviour coverage**

   * If code behaviour changed, were relevant tests added or updated?
   * Are new endpoints / validations / branches covered?

3. **Edge cases**

   * Negative paths
   * Empty inputs
   * invalid values
   * not-found cases
   * boundary values

4. **Test integrity**

   * Were tests weakened?
   * Were tests skipped?
   * Were assertions removed?
   * Did implementation modify tests suspiciously?

5. **Confidence**

   * Passing tests are necessary but not sufficient.
   * If tests are missing or shallow, block auto-merge.

---

## 5. Verdicts

Allowed verdicts:

```text
TEST_QUALITY_APPROVED
TESTS_WEAK
TESTS_BLOCKING
ERROR
```

Meaning:

```text
TEST_QUALITY_APPROVED
→ Tests sufficiently cover the Story for this PR.

TESTS_WEAK
→ Tests pass but coverage is shallow/incomplete. Human should review.

TESTS_BLOCKING
→ Tests are missing, suspicious, failing, skipped inappropriately, or do not cover critical behaviour.

ERROR
→ Internal orchestrator fallback if Test Quality Agent call fails.
```

Claude must return only:

```text
TEST_QUALITY_APPROVED
TESTS_WEAK
TESTS_BLOCKING
```

`ERROR` is internal only.

---

## 6. Data Model Changes

### 6.1 New table: `agent_test_quality_reviews`

Suggested schema:

```sql
id SERIAL PRIMARY KEY,
run_id INTEGER NOT NULL,
pr_number INTEGER,
pr_url TEXT,
repo_slug VARCHAR(200),
story_key VARCHAR(100),
agent_name VARCHAR(100) DEFAULT 'test_quality_agent',
quality_status VARCHAR(40) NOT NULL,
confidence_level VARCHAR(20),
summary TEXT,
coverage_findings_json TEXT,
missing_tests_json TEXT,
suspicious_tests_json TEXT,
recommendations_json TEXT,
model_used VARCHAR(100),
memory_snapshot_ids_json TEXT,
created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
```

Allowed:

```text
quality_status:
- TEST_QUALITY_APPROVED
- TESTS_WEAK
- TESTS_BLOCKING
- ERROR

confidence_level:
- LOW
- MEDIUM
- HIGH
```

### 6.2 Extend `workflow_runs`

Add:

```text
test_quality_status
test_quality_required
test_quality_completed_at
test_quality_summary
```

Defaults:

```text
test_quality_required = true
```

---

## 7. Constants

Add to `app/feedback.py` or equivalent constants module:

```python
class AgentName:
    REVIEWER_AGENT = "reviewer_agent"
    TEST_QUALITY_AGENT = "test_quality_agent"

class TestQualityStatus:
    APPROVED = "TEST_QUALITY_APPROVED"
    WEAK = "TESTS_WEAK"
    BLOCKING = "TESTS_BLOCKING"
    ERROR = "ERROR"

class TestQualityConfidence:
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
```

Config flags:

```python
TEST_QUALITY_REQUIRED = True
TEST_QUALITY_BLOCKS_MERGE = True
```

---

## 8. Test Quality Agent Input Contract

Build an input package:

```json
{
  "story": {
    "key": "KAN-24",
    "summary": "...",
    "description": "...",
    "acceptance_criteria": [...]
  },
  "pr": {
    "number": 33,
    "url": "...",
    "title": "...",
    "body": "..."
  },
  "diff": {
    "full_diff": "...",
    "changed_files": [...]
  },
  "tests": {
    "status": "PASSED",
    "command": "pytest -q",
    "output_excerpt": "...",
    "test_files_changed": [...],
    "skipped_tests_detected": true_or_false
  },
  "implementation": {
    "files_changed_count": 2,
    "retry_count": 0,
    "changed_source_files": [...],
    "changed_test_files": [...]
  },
  "memory": {
    "execution_guidance": "...",
    "manual_notes": "..."
  }
}
```

---

## 9. Test Quality Agent Output Contract

Claude must return strict tool output:

```json
{
  "quality_status": "TEST_QUALITY_APPROVED",
  "confidence_level": "HIGH",
  "summary": "Tests cover the new endpoint behaviour and relevant negative path.",
  "coverage_findings": [
    {
      "criteria": "Search returns matching items",
      "status": "covered",
      "evidence": "tests/test_items.py includes test_search_items_by_name"
    }
  ],
  "missing_tests": [],
  "suspicious_tests": [],
  "recommendations": [
    "Consider adding pagination test in a future story."
  ]
}
```

Rules:

* `TESTS_BLOCKING` if tests failed.
* `TESTS_BLOCKING` if tests are NOT_RUN for a repo that should support tests.
* `TESTS_BLOCKING` if tests were removed/skipped to make the PR pass.
* `TESTS_WEAK` if tests pass but do not cover acceptance criteria.
* `TEST_QUALITY_APPROVED` only if tests meaningfully cover the Story.

---

# 10. Iteration Plan

## Iteration 0 — Schema and constants baseline

### Goal

Prepare database and constants.

### Tasks

* Add `agent_test_quality_reviews` table.
* Extend `workflow_runs` with test quality columns.
* Add constants:

  * `TEST_QUALITY_AGENT`
  * `TEST_QUALITY_APPROVED`
  * `TESTS_WEAK`
  * `TESTS_BLOCKING`
  * `ERROR`
* Add config flags:

  * `TEST_QUALITY_REQUIRED=True`
  * `TEST_QUALITY_BLOCKS_MERGE=True`

### Acceptance criteria

* migrations run cleanly
* existing story implementation pipeline still works
* no merge behaviour changed yet

Then STOP.

---

## Iteration 1 — Test Quality Agent client

### Goal

Create the agent function without wiring it into workflow.

### Tasks

Add function:

```python
review_test_quality(
    story_context,
    pr_context,
    diff_context,
    test_context,
    implementation_context,
    memory_context
) -> dict
```

Use forced tool output.

### Acceptance criteria

* static sample input returns valid structured output
* malformed/no-tool response raises controlled exception
* no DB/GitHub writes yet

Then STOP.

---

## Iteration 2 — Build test quality package

### Goal

Assemble all context needed for the agent.

### Tasks

Create `_build_test_quality_package(...)`.

It should collect:

* Story summary, description, acceptance criteria
* PR title/body/url/number
* unified diff
* changed files
* test command/status/output
* test files changed
* source files changed
* retry count
* execution memory

### Add helpers

* detect test files:

  * `tests/`
  * `test_*.py`
  * `*_test.py`
* detect skipped tests from output/diff:

  * `@pytest.mark.skip`
  * `pytest.skip`
  * `.skip(`
  * “skipped” in test output

### Acceptance criteria

* package can be logged safely
* no secrets included
* changed source/test files are correctly separated

Then STOP.

---

## Iteration 3 — Run Test Quality Agent after Reviewer Agent

### Goal

Wire agent into workflow, but do not block merge yet.

### Placement

Run after Reviewer Agent completes.

Flow:

```text
PR created
→ Reviewer Agent
→ Test Quality Agent
→ store verdict
→ Telegram notification
→ merge policy unchanged for now
```

### Tasks

* call `review_test_quality(...)`
* store row in `agent_test_quality_reviews`
* update `workflow_runs.test_quality_status`
* send Telegram event `test_quality_completed`

### Acceptance criteria

* successful PR gets a test quality row
* API/DB shows status
* Telegram shows verdict
* merge behaviour unchanged

Then STOP.

---

## Iteration 4 — Post Test Quality Agent comment to GitHub PR

### Goal

Make verdict visible on PR.

### Comment format

```md
## Test Quality Agent Verdict: ✅ TEST_QUALITY_APPROVED

**Confidence:** HIGH

### Summary
...

### Coverage Findings
- [covered] Acceptance criterion: ...

### Missing Tests
None

### Suspicious Test Changes
None

### Recommendations
- ...
```

Emoji mapping:

* ✅ `TEST_QUALITY_APPROVED`
* ⚠️ `TESTS_WEAK`
* 🚫 `TESTS_BLOCKING`
* ❌ `ERROR`

### Acceptance criteria

* PR contains Test Quality Agent comment
* comment matches DB verdict
* GitHub comment failure does not corrupt workflow state

Then STOP.

---

## Iteration 5 — Add merge gate

### Goal

Make Test Quality Agent a real merge gate.

Update auto-merge condition:

```python
auto_merge_ok = (
    mapping.get("auto_merge_enabled")
    and final_test_result["status"] == "PASSED"
    and applied.get("applied", False)
    and applied.get("count", 0) <= MAX_FILES_FOR_AUTOMERGE
    and review_status == ReviewStatus.APPROVED_BY_AI
    and test_quality_status == TestQualityStatus.APPROVED
)
```

Add merge statuses:

```text
BLOCKED_BY_TEST_QUALITY
SKIPPED_TEST_QUALITY_WEAK
```

Suggested mapping:

* `TESTS_BLOCKING` → `BLOCKED_BY_TEST_QUALITY`
* `TESTS_WEAK` → `SKIPPED`
* `ERROR` → `SKIPPED`

### Acceptance criteria

* approved test quality allows merge
* weak test quality prevents auto-merge
* blocking test quality prevents auto-merge
* Telegram clearly explains reason

Then STOP.

---

## Iteration 6 — Negative-path validation

### Goal

Prove the agent catches weak tests.

Test scenarios:

### Scenario A — tests pass and cover Story

Expected:

```text
TEST_QUALITY_APPROVED
```

### Scenario B — tests pass but no relevant tests changed

Expected:

```text
TESTS_WEAK
```

### Scenario C — tests failed

Expected:

```text
TESTS_BLOCKING
```

### Scenario D — tests skipped or weakened

Expected:

```text
TESTS_BLOCKING
```

### Scenario E — tests NOT_RUN

Expected:

* If repo supports tests: `TESTS_BLOCKING`
* If repo does not support tests: `TESTS_WEAK` or `ERROR`, but never approved

### Acceptance criteria

* all scenarios behave predictably
* DB/API/Telegram agree

Then STOP.

---

## Iteration 7 — Feedback event integration

### Goal

Feed test quality outcomes into memory system.

Add feedback types:

```text
test_quality_status
test_quality_confidence
test_quality_approved
tests_weak
tests_blocking
missing_test_count
suspicious_test_count
```

Update `record_execution_feedback()`.

### Acceptance criteria

* test quality events appear in `feedback_events`
* repo memory can later learn test quality patterns
* no duplicate feedback rows for same run

Then STOP.

---

## Iteration 8 — Memory snapshot integration

### Goal

Incorporate Reviewer Agent and Test Quality Agent signals into repo memory.

Phase 8 captured review feedback but did not yet incorporate it into memory bullets. 

Update repo memory generation to include:

* review approval/block rates
* common review risk levels
* test quality approval/weak/block rates
* recurring missing test patterns if available

Example memory bullets:

```text
- Reviewer Agent blocked 2 recent PRs due to story mismatch.
- Test Quality Agent marked 3 recent PRs as weak due to missing negative-path tests.
- PRs with storage changes often require tests in tests/test_items.py.
```

### Acceptance criteria

* memory snapshots include review/test quality lessons
* prompt enrichment remains bounded
* manual notes still take priority

Then STOP.

---

## Iteration 9 — Inspection APIs

### Goal

Make test quality verdicts inspectable.

Add endpoints:

```text
GET /debug/test-quality-reviews?limit=N
GET /debug/workflow-runs/{run_id}/test-quality
```

Optional:

```text
GET /debug/test-quality-reviews/{id}
```

### Acceptance criteria

* verdicts visible without SSH
* filter by:

  * run_id
  * repo_slug
  * quality_status
* JSON fields decoded

Then STOP.

---

## Iteration 10 — End-to-end Phase 9 validation

### Required scenarios

#### Scenario A — clean success

```text
tests pass
Reviewer Agent approves
Test Quality Agent approves
auto-merge succeeds
```

#### Scenario B — weak tests

```text
tests pass
Reviewer Agent approves
Test Quality Agent = TESTS_WEAK
auto-merge skipped
PR left open
```

#### Scenario C — blocking tests

```text
tests fail or skipped suspiciously
Test Quality Agent = TESTS_BLOCKING
auto-merge blocked
```

#### Scenario D — Test Quality Agent error

```text
agent call fails
workflow completes honestly
merge skipped
status = ERROR
```

### Acceptance criteria

* Developer Agent does not judge tests
* Reviewer Agent does not own test adequacy
* Test Quality Agent independently gates merge
* GitHub PR shows both agent comments
* DB/API/Telegram agree
* memory captures test quality outcomes

Then STOP.

---

## 11. Final instruction to Claude

Build Phase 9 as an **agent extraction**, not a test runner enhancement.

The Test Runner answers:

```text
Did tests pass?
```

The Test Quality Agent answers:

```text
Are these tests meaningful enough to trust the change?
```

Optimize for:

* independent judgment
* structured verdicts
* merge safety
* clear PR comments
* feedback into memory

Do not optimize for:

* long reviews
* broad test generation
* code coverage tooling
* generic multi-agent framework

This is the second clean step toward a proper multi-agent architecture.
