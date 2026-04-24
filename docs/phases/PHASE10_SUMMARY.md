# Phase 10 Summary — Architecture/Impact Agent + Unified Release Gate

## Objective

Phase 10 introduces a third independent review agent (Architecture/Impact Agent) and replaces the inline merge gate with a centralized Unified Release Gate that aggregates signals from all four evaluation sources (Test Runner, Reviewer Agent, Test Quality Agent, Architecture Agent) into a single, auditable merge decision.

---

## 12 Iterations

### Iteration 0 — Schema and constants baseline

Added `agent_architecture_reviews` table and 7 new columns on `workflow_runs`:
- `architecture_status`, `architecture_required`, `architecture_completed_at`, `architecture_summary`
- `release_decision`, `release_decision_reason`, `release_decided_at`

Added constants in `app/feedback.py`:
- `ArchitectureStatus`: `ARCHITECTURE_APPROVED` | `ARCHITECTURE_NEEDS_REVIEW` | `ARCHITECTURE_BLOCKED` | `ERROR`
- `ArchitectureRiskLevel`: `LOW` | `MEDIUM` | `HIGH`
- `ReleaseDecision`: `RELEASE_APPROVED` | `RELEASE_SKIPPED` | `RELEASE_BLOCKED`
- 7 new `FeedbackType` constants: `ARCHITECTURE_STATUS`, `ARCHITECTURE_RISK_LEVEL`, `ARCHITECTURE_APPROVED`, `ARCHITECTURE_NEEDS_REVIEW`, `ARCHITECTURE_BLOCKED`, `RELEASE_DECISION`, `RELEASE_BLOCKING_GATE_COUNT`
- Config flags: `ARCHITECTURE_REVIEW_REQUIRED = True`, `ARCHITECTURE_REVIEW_BLOCKS_MERGE = True`

Migrations are idempotent (ALTER TABLE IF NOT EXISTS pattern). No behavior change.

---

### Iteration 1 — Architecture Agent client

Added `review_architecture()` in `app/claude_client.py` with:
- `ARCHITECTURE_PROMPT` system prompt covering scope discipline, API compatibility, data/model impact, dependency impact, operational impact, security impact, maintainability
- `_ARCHITECTURE_TOOL` forced tool schema (`submit_architecture_review`) requiring: `architecture_status`, `risk_level`, `summary`, `impact_areas[]` (area/risk/finding), `blocking_reasons[]`, `recommendations[]`
- Raises `RuntimeError` if Claude returns no tool_use block (same pattern as Reviewer and TQ agents)

---

### Iteration 2 — Architecture review package builders

Added helpers in `app/workflows.py`:
- `_classify_changed_files(files) -> dict` — categorizes changed files into `api`, `model`, `storage`, `config`, `test`, `doc` buckets using regex pattern lists
- `_build_architecture_review_package(...)` — assembles the structured input dict for `review_architecture()` (story_context, repo_context, pr_context, diff_context, signal_context, memory_context)
- `_format_architecture_comment(verdict) -> str` — formats PR comment with emoji (✅ APPROVED / ⚠️ NEEDS_REVIEW / 🚫 BLOCKED / ❌ ERROR), risk level, summary, impact areas table, blocking reasons, recommendations

---

### Iteration 3 — Architecture Agent wired (no merge gate change yet)

Wired Architecture Agent into `story_implementation()` after Test Quality Agent step:
- Try/except non-fatal (agent failure does not abort the run)
- Stores verdict in `agent_architecture_reviews` via `store_architecture_review()`
- Updates `workflow_runs.architecture_status` and `architecture_summary`
- Sends Telegram notification with verdict
- Merge behavior unchanged in this iteration

---

### Iteration 4 — Architecture Agent PR comment

Added PR comment posting after Architecture Agent verdict:
- Calls `post_pr_comment()` with formatted comment
- Comment failure is non-fatal (logged, does not change workflow state)
- Comment matches the DB verdict exactly (no divergence possible)

---

### Iteration 5 — Unified Release Gate function

Created `evaluate_release_decision()` as a pure function in `workflows.py` (no DB or Claude calls, fully testable in isolation):

```python
evaluate_release_decision(
    mapping, final_test_result, applied,
    review_status, test_quality_status, architecture_status
) -> {release_decision, can_auto_merge, reason, blocking_gates, warnings}
```

Gate logic:
- **Hard BLOCKED:** tests failed, reviewer `BLOCKED`, TQ `TESTS_BLOCKING`, arch `ARCHITECTURE_BLOCKED`
- **Soft SKIPPED:** auto_merge disabled, file count exceeded, reviewer `NEEDS_CHANGES`, TQ `TESTS_WEAK`, arch `ARCHITECTURE_NEEDS_REVIEW`, any agent `ERROR`, no PR created
- **APPROVED:** all gates pass

Validated with 9 synthetic scenarios covering all gate combinations.

---

### Iteration 6 — Release Gate replaces inline merge gate

Replaced the inline merge condition in `story_implementation()` with a call to `evaluate_release_decision()`. The function:
1. Calls `evaluate_release_decision()` with all current signals
2. Persists `release_decision`, `release_decision_reason`, `release_decided_at` on `workflow_runs`
3. Routes to merge / specific BLOCKED status / SKIPPED based on `release_decision` and `blocking_gates`

New `merge_status` value: `BLOCKED_BY_ARCHITECTURE` (joins `BLOCKED_BY_REVIEW` and `BLOCKED_BY_TEST_QUALITY` for observability parity)

---

### Iteration 7 — Architecture Agent gates merge

Architecture verdict now participates in the Release Gate:
- `ARCHITECTURE_APPROVED` → gate passes (if other gates also pass → auto-merge)
- `ARCHITECTURE_NEEDS_REVIEW` → `RELEASE_SKIPPED` → `merge_status=SKIPPED`
- `ARCHITECTURE_BLOCKED` → `RELEASE_BLOCKED` → `merge_status=BLOCKED_BY_ARCHITECTURE`
- `ERROR` → `RELEASE_SKIPPED` → `merge_status=SKIPPED` (non-fatal)

---

### Iteration 8 — Feedback event integration

Extended `record_execution_feedback()` in `app/database.py`:
- Fetches `architecture_status` and `release_decision` from `workflow_runs`
- Emits: `architecture_status`, `architecture_approved`/`needs_review`/`blocked`, `architecture_risk_level` (from `agent_architecture_reviews`), `release_decision`

Extended `generate_repo_memory_snapshot()`:
- Architecture Agent stats query → exec_bullets: `"Architecture Agent: N approved, N needs-review, N blocked of N reviewed"`
- Release Gate stats query → exec_bullets: `"Release Gate: N approved, N blocked, N skipped of N decisions"`
- Evidence dict gains: `arch_approved`, `arch_needs_review`, `arch_blocked`, `release_approved`, `release_blocked`, `release_skipped`

---

### Iteration 9 — Inspection APIs

Added `list_architecture_reviews()` in `app/database.py` (same pattern as `list_agent_reviews` and `list_test_quality_reviews`).

Added 3 new endpoints in `app/main.py`:

| Endpoint | Purpose |
|---|---|
| `GET /debug/architecture-reviews` | List Architecture Agent verdicts; filter by run_id, repo_slug, architecture_status |
| `GET /debug/workflow-runs/{run_id}/architecture` | All Architecture Agent verdicts for a run |
| `GET /debug/workflow-runs/{run_id}/release-decision` | Release Gate decision + all agent statuses for a run |

---

### Iteration 10 — Negative-path validation

4 live Claude call scenarios validated on EC2:
- **Scenario A** (low-risk additive endpoint): → `ARCHITECTURE_APPROVED`, `LOW`
- **Scenario B** (broad multi-layer change, 5 files): → `ARCHITECTURE_NEEDS_REVIEW`, `MEDIUM`
- **Scenario C** (auth bypass — token check disabled): → `ARCHITECTURE_BLOCKED`, `HIGH`
- **Scenario D** (None inputs): raises `AttributeError` (correctly caught by workflow try/except)

---

### Iteration 11 — End-to-end validation

27-check E2E test covering all 6 gate-path scenarios:

| Scenario | Release Gate result |
|---|---|
| A — all gates pass | `RELEASE_APPROVED`, `can_auto_merge=True` |
| B — reviewer BLOCKED | `RELEASE_BLOCKED`, blocking_gates contains "reviewer blocked" |
| C — TQ TESTS_WEAK | `RELEASE_SKIPPED` |
| D — Architecture NEEDS_REVIEW | `RELEASE_SKIPPED`, warnings contains "architecture" |
| E — Architecture BLOCKED | `RELEASE_BLOCKED`, blocking_gates contains "architecture blocked" |
| F — Architecture ERROR | `RELEASE_SKIPPED` |

DB schema verified (7 workflow_runs columns, agent_architecture_reviews table). API endpoints verified (200/404 responses, correct response shape).

---

## Data Model

### `agent_architecture_reviews`

| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL PK | |
| `run_id` | INTEGER | FK → workflow_runs |
| `pr_number` | INTEGER NULL | |
| `pr_url` | VARCHAR(500) NULL | |
| `repo_slug` | VARCHAR(200) NULL | |
| `story_key` | VARCHAR(100) NULL | |
| `agent_name` | VARCHAR(100) | default `architecture_agent` |
| `architecture_status` | VARCHAR(50) | `ARCHITECTURE_APPROVED` / `ARCHITECTURE_NEEDS_REVIEW` / `ARCHITECTURE_BLOCKED` / `ERROR` |
| `risk_level` | VARCHAR(20) NULL | `LOW` / `MEDIUM` / `HIGH` |
| `summary` | TEXT NULL | |
| `impact_areas_json` | TEXT NULL | JSON array of {area, risk, finding} |
| `blocking_reasons_json` | TEXT NULL | JSON array of strings |
| `recommendations_json` | TEXT NULL | JSON array of strings |
| `model_used` | VARCHAR(100) NULL | |
| `memory_snapshot_ids_json` | TEXT NULL | |
| `created_at` | TIMESTAMP | |
| `updated_at` | TIMESTAMP | |

### New `workflow_runs` columns

| Column | Type | Notes |
|---|---|---|
| `architecture_status` | VARCHAR(50) NULL | Set after Architecture Agent completes |
| `architecture_required` | BOOLEAN | Default `true` |
| `architecture_completed_at` | TIMESTAMP NULL | |
| `architecture_summary` | TEXT NULL | Short summary from agent |
| `release_decision` | VARCHAR(50) NULL | `RELEASE_APPROVED` / `RELEASE_SKIPPED` / `RELEASE_BLOCKED` |
| `release_decision_reason` | TEXT NULL | Human-readable reason string |
| `release_decided_at` | TIMESTAMP NULL | |

---

## Architecture Agent Input/Output

**Input:** 6-key package assembled by `_build_architecture_review_package()`:
- `story_context` — key, summary, description, acceptance_criteria
- `repo_context` — repo_slug, primary_language, framework
- `pr_context` — number, url, title, body
- `diff_context` — changed_files (classified), full_diff
- `signal_context` — test_status, review_status, test_quality_status, files_changed_count, retry_count
- `memory_context` — execution_guidance, manual_notes

**Output (forced tool_use):**
```json
{
  "architecture_status": "ARCHITECTURE_APPROVED",
  "risk_level": "LOW",
  "summary": "...",
  "impact_areas": [{"area": "api", "risk": "LOW", "finding": "..."}],
  "blocking_reasons": [],
  "recommendations": []
}
```

---

## PR Comment Format

```
## Architecture Agent Verdict: ✅ ARCHITECTURE_APPROVED

**Risk:** LOW

### Summary
...

### Impact Areas
| Area | Risk | Finding |
|---|---|---|
| api | LOW | Adds backward-compatible endpoint. |

### Blocking Reasons
None

### Recommendations
- Consider documenting the new endpoint.
```

Emoji: ✅ APPROVED / ⚠️ NEEDS_REVIEW / 🚫 BLOCKED / ❌ ERROR

---

## Design Decisions

**Pure function for Release Gate** — `evaluate_release_decision()` takes no `run_id` and makes no DB or Claude calls. This makes it trivially testable with synthetic inputs and eliminates the risk of side effects changing workflow state during the decision.

**BLOCKED_BY_ARCHITECTURE merge_status** — Added as a distinct value (rather than collapsing into `BLOCKED_BY_RELEASE_GATE`) so that observability/dashboarding can distinguish which agent caused the block without querying `agent_architecture_reviews`.

**Non-fatal agent steps** — Every agent (Reviewer, TQ, Architecture) is individually try/excepted. An agent error results in `ERROR` status + `RELEASE_SKIPPED` (not workflow failure), ensuring a bad API response never aborts the entire run.

**File classification** — `_classify_changed_files()` provides context to the Architecture Agent without exposing file contents, keeping the prompt size bounded while still informing risk assessment (e.g., "5 files across api + model + storage + config" is a stronger signal than a raw file list).

---

## Commit History

| Iteration | Commit message prefix |
|---|---|
| 0 | feat: Phase 10 Iteration 0 — schema and constants baseline |
| 1 | feat: Phase 10 Iteration 1 — Architecture Agent client |
| 2 | feat: Phase 10 Iteration 2 — architecture review package helpers |
| 3-7 | feat: Iterations 3-7 — Architecture Agent wired, PR comment, evaluate_release_decision |
| 8 | feat: Phase 10 Iteration 8 — architecture and release gate feedback events |
| 9 | feat: Phase 10 Iteration 9 — architecture review and release decision inspection APIs |
