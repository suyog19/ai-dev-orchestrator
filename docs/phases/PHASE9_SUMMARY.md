# Phase 9 — Test Quality Agent: Execution Summary

## Overview

Phase 9 introduced the **Test Quality Agent**, an independent AI reviewer that evaluates whether the tests committed alongside code changes are adequate to trust the change. It runs after the Reviewer Agent and before the auto-merge decision, adding a second independent gate to the merge pipeline.

The central design principle: the Developer Agent writes code and tests; the Test Runner checks whether tests pass; the **Test Quality Agent independently judges whether the tests are meaningful**. These three concerns are kept strictly separate.

---

## What Was Built

### New Components

**`app/claude_client.py`**
- `TEST_QUALITY_PROMPT` system prompt — instructs Claude to evaluate test adequacy, not implementation correctness
- `_TEST_QUALITY_TOOL` schema — forced tool_use `submit_test_quality_review` with fields: `quality_status`, `confidence_level`, `summary`, `coverage_findings`, `missing_tests`, `suspicious_tests`, `recommendations`
- `review_test_quality(story_context, pr_context, diff_context, test_context, implementation_context, memory_context)` — calls Claude with forced `tool_choice={"type":"tool","name":"submit_test_quality_review"}`; raises RuntimeError if no tool_use block returned

**`app/feedback.py`**
- `AgentName.TEST_QUALITY_AGENT = "test_quality_agent"`
- `TestQualityStatus` class: `APPROVED="TEST_QUALITY_APPROVED"`, `WEAK="TESTS_WEAK"`, `BLOCKING="TESTS_BLOCKING"`, `ERROR="ERROR"`
- `TestQualityConfidence` class: `LOW`, `MEDIUM`, `HIGH`
- Constants: `TEST_QUALITY_REQUIRED = True`, `TEST_QUALITY_BLOCKS_MERGE = True`
- 7 new `FeedbackType` constants: `TEST_QUALITY_STATUS`, `TEST_QUALITY_CONFIDENCE`, `TEST_QUALITY_APPROVED`, `TESTS_WEAK`, `TESTS_BLOCKING`, `MISSING_TEST_COUNT`, `SUSPICIOUS_TEST_COUNT`

**`app/database.py`**
- `init_db()`: 4 new ALTER TABLE columns on `workflow_runs` (`test_quality_status`, `test_quality_required`, `test_quality_completed_at`, `test_quality_summary`); new `agent_test_quality_reviews` table
- `store_test_quality_review(run_id, verdict, pr_number, pr_url, repo_slug, story_key, model_used)` — inserts into `agent_test_quality_reviews`, updates `workflow_runs.test_quality_status`
- `record_execution_feedback()` — extended to emit 7 new test quality feedback events including querying `agent_test_quality_reviews` for confidence and test counts
- `generate_repo_memory_snapshot()` — extended to include Reviewer Agent and Test Quality Agent signal summaries in `execution_guidance` bullets and evidence JSON
- `list_test_quality_reviews(run_id, repo_slug, quality_status, limit)` — filterable list of TQ verdicts with JSON fields decoded

**`app/workflows.py`**
- `_TEST_FILE_PATTERNS`, `_is_test_file(path)` — helper to classify test files from diff
- `_SKIP_PATTERNS`, `_detect_skipped_tests(diff, test_output)` — detects test-skip/removal patterns as a BLOCKING signal
- `_build_test_quality_package(...)` — assembles all inputs for `review_test_quality()`
- `_format_test_quality_comment(verdict)` — renders verdict as GitHub PR comment with ✅/⚠️/🚫/❌ emoji
- Full pipeline wired after Reviewer Agent step: build package → call agent → store verdict → post PR comment → send Telegram → error handling mirrors Reviewer Agent (non-fatal)
- Merge gate updated with Test Quality Agent as second gate

**`app/main.py`**
- `GET /debug/test-quality-reviews` — list verdicts, filter by `run_id`, `repo_slug`, `quality_status`, `limit`
- `GET /debug/workflow-runs/{run_id}/test-quality` — all TQ verdicts for one run; 404 if none

---

## Data Model

### New table: `agent_test_quality_reviews`

```sql
CREATE TABLE agent_test_quality_reviews (
    id                       SERIAL PRIMARY KEY,
    run_id                   INTEGER       NOT NULL REFERENCES workflow_runs(id),
    pr_number                INTEGER       NULL,
    pr_url                   VARCHAR(500)  NULL,
    repo_slug                VARCHAR(200)  NULL,
    story_key                VARCHAR(100)  NULL,
    agent_name               VARCHAR(100)  NOT NULL DEFAULT 'test_quality_agent',
    quality_status           VARCHAR(40)   NOT NULL,
    confidence_level         VARCHAR(20)   NULL,
    summary                  TEXT          NULL,
    coverage_findings_json   TEXT          NULL,
    missing_tests_json       TEXT          NULL,
    suspicious_tests_json    TEXT          NULL,
    recommendations_json     TEXT          NULL,
    model_used               VARCHAR(100)  NULL,
    memory_snapshot_ids_json TEXT          NULL,
    created_at               TIMESTAMP     NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMP     NOT NULL DEFAULT NOW()
)
```

### New `workflow_runs` columns

| Column | Type | Purpose |
|---|---|---|
| `test_quality_status` | VARCHAR(40) | Final TQ verdict: `TEST_QUALITY_APPROVED` / `TESTS_WEAK` / `TESTS_BLOCKING` / `ERROR` |
| `test_quality_required` | BOOLEAN DEFAULT TRUE | Whether TQ review was required for this run |
| `test_quality_completed_at` | TIMESTAMP | When TQ review completed |
| `test_quality_summary` | TEXT | Short summary from TQ Agent |

---

## Merge Gate Policy

Auto-merge requires ALL of:
- `final_test_result["status"] == "PASSED"`
- `applied["applied"] == True`
- `applied["count"] <= 3`
- `review_status == ReviewStatus.APPROVED_BY_AI`
- `test_quality_status == TestQualityStatus.APPROVED`
- `mapping["auto_merge_enabled"] == True`

| TQ Status | Merge Outcome |
|---|---|
| `TEST_QUALITY_APPROVED` + Reviewer approved | `MERGED` |
| `TESTS_WEAK` | `SKIPPED` (PR left open) |
| `TESTS_BLOCKING` | `BLOCKED_BY_TEST_QUALITY` |
| `ERROR` | `SKIPPED` (non-fatal — run still completes) |

`BLOCKED_BY_TEST_QUALITY` is distinct from `BLOCKED_BY_REVIEW` so the reason is always clear from `merge_status`.

---

## PR Comment Format

```
## 🔬 Test Quality Review

**Verdict: ✅ APPROVED** | Confidence: HIGH

Good coverage across acceptance criteria.

**Recommendations:**
- Add negative-path test for 404 responses
```

Emoji key: ✅ APPROVED · ⚠️ TESTS_WEAK · 🚫 TESTS_BLOCKING · ❌ ERROR

---

## Feedback Events Emitted

After every run with a TQ verdict, `record_execution_feedback()` writes:

| Event | Value |
|---|---|
| `test_quality_status` | verdict string |
| `test_quality_confidence` | `LOW` / `MEDIUM` / `HIGH` |
| `test_quality_approved` | `"true"` (only if APPROVED) |
| `tests_weak` | `"true"` (only if TESTS_WEAK) |
| `tests_blocking` | `"true"` (only if TESTS_BLOCKING) |
| `missing_test_count` | count from `missing_tests_json` |
| `suspicious_test_count` | count from `suspicious_tests_json` |

---

## Memory Snapshot Integration

`generate_repo_memory_snapshot()` now includes two additional bullets in `execution_guidance`:

```text
- Reviewer Agent: 2 approved, 0 needs-changes, 1 blocked of 3 reviewed
- Test Quality Agent: 1 approved, 2 weak, 0 blocking of 3 reviewed
```

These are derived from `feedback_events` counts and written to both the `summary` text and `evidence_json`.

---

## Verdict Logic Rules (encoded in TEST_QUALITY_PROMPT)

| Condition | Verdict |
|---|---|
| Tests pass, cover all acceptance criteria | `TEST_QUALITY_APPROVED` |
| Tests pass but edge cases missing | `TESTS_WEAK` |
| Tests fail | `TESTS_BLOCKING` |
| Tests skipped/removed to force pass | `TESTS_BLOCKING` |
| `NOT_RUN` on a repo that supports tests | `TESTS_BLOCKING` |
| No test files changed for code change | `TESTS_WEAK` |

---

## Iterations Completed

| # | Description | Validation |
|---|---|---|
| 0 | DB schema — new table + workflow_runs columns | EC2: `init_db()` idempotency check |
| 1 | `review_test_quality()` in claude_client.py | EC2: forced tool_use schema |
| 2 | Helper functions in workflows.py | EC2: `_build_test_quality_package`, `_detect_skipped_tests` |
| 3 | Pipeline wiring — full TQ step in story_implementation | EC2: end-to-end step execution |
| 4 | PR comment rendering | EC2: `_format_test_quality_comment` all 4 verdicts |
| 5 | Merge gate integration | EC2: all 4 merge paths (MERGED/SKIPPED/BLOCKED/non-fatal) |
| 6 | Negative-path scenarios (5 scenarios) | EC2: tq_val6.py — 5/5 passed |
| 7 | Feedback event integration | EC2: tq_val7.py — 7/7 passed |
| 8 | Memory snapshot integration | EC2: tq_val8.py — 6/6 passed |
| 9 | Inspection APIs | EC2: HTTP endpoints live — filtering + 404 verified |
| 10 | End-to-end validation (4 scenarios) | EC2: tq_val10.py — 31/31 passed |

---

## API Endpoints Added

| Method | Path | Description |
|---|---|---|
| GET | `/debug/test-quality-reviews` | List TQ verdicts; filter by `run_id`, `repo_slug`, `quality_status` |
| GET | `/debug/workflow-runs/{run_id}/test-quality` | All TQ verdicts for a run; 404 if none |

---

## Design Decisions

**Why a separate agent instead of extending the Reviewer Agent?**
The Reviewer Agent evaluates whether the change matches the story. The Test Quality Agent evaluates whether the tests are adequate. These are orthogonal concerns. Merging them would produce a single verdict that collapses two independent failure modes, making it impossible to know which was responsible.

**Why `BLOCKED_BY_TEST_QUALITY` as a distinct merge_status?**
Post-incident, knowing whether a blocked PR was due to Reviewer Agent or Test Quality Agent matters for diagnosis. A single `BLOCKED` status would require joining to `agent_reviews` and `agent_test_quality_reviews` to know the cause.

**Why `TESTS_WEAK` → SKIPPED (not a distinct merge_status)?**
`TESTS_WEAK` means the tests are insufficient but not deceptive. The PR should remain open for the developer to improve. Using `SKIPPED` rather than a new `BLOCKED_BY_WEAK_TESTS` status keeps `merge_status` legible — SKIPPED always means "safe to merge manually if you choose."

**Why ERROR is non-fatal?**
Agent failures (network timeouts, malformed responses) are transient. A workflow run that completes with `test_quality_status=ERROR` still has a Reviewer Agent verdict and a test pass/fail result — enough signal for a human to decide. Treating agent errors as fatal would turn infrastructure flakiness into workflow failures.

**Skipped-test detection via diff heuristics**
`_detect_skipped_tests()` scans for `@pytest.mark.skip`, `pytest.skip(`, and `skipTest(` in the diff and test output. This is intentionally lightweight — the Test Quality Agent itself is responsible for the full semantic analysis. The heuristic is only a contextual signal injected into the prompt, not a hard gate.
