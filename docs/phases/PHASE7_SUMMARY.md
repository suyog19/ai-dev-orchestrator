# Phase 7 Summary — Feedback, Memory, and Prompt Enrichment

## Goal

Phase 6 built a complete Epic → Story → Code → PR → Merge pipeline with a human approval gate.

Phase 7 made the system learn from its own history. Every planning approval, rejection, or regeneration and every execution success, failure, or retry is now captured as a structured signal. Those signals are summarised into bounded memory snapshots that are injected into future Claude prompts — so the system gets incrementally better at scoping Stories and targeting code changes without requiring any manual tuning.

---

## What Was Built

### Signal → Memory → Prompt loop

```
Planning run completes (APPROVED / REJECTED / FAILED)
  → record_planning_feedback writes feedback_events rows
  → generate_repo_memory_snapshot refreshes planning_guidance snapshot
  → next Epic breakdown receives "Prior lessons" block in Claude prompt

Execution run completes (COMPLETED / FAILED)
  → record_execution_feedback writes feedback_events rows
  → categorize_execution_failure assigns a FailureCategory
  → generate_repo_memory_snapshot refreshes execution_guidance snapshot
  → generate_epic_outcome_rollup updates the epic-level rollup
  → next Story implementation receives "Prior lessons" block in suggest_change / fix_change
```

Memory is bounded at every injection point: **5 bullets maximum, 1 000 characters maximum**. Prompt enrichment is non-fatal — a DB failure during retrieval is logged and silently skipped; the Claude call proceeds unchanged.

### Manual override layer

Human-authored notes can be added via `POST /debug/memory`. They are tagged `source=human` and `memory_kind=manual_note`, and they are always placed first in the injected block so they are never crowded out by derived bullets.

---

## Iterations Completed

| # | Iteration | What it delivered |
|---|---|---|
| 0 | Memory baseline | DB schema (`feedback_events`, `memory_snapshots`), `app/feedback.py` constants module |
| 1 | Planning feedback capture | `record_planning_feedback` called from `epic_breakdown` and Telegram approval handler |
| 2 | Execution feedback capture | `record_execution_feedback` wired at all three terminal paths in `worker._execute` |
| 3 | Failure categorization | `categorize_execution_failure` and `categorize_planning_failure` rule-based functions |
| 4 | Repo-level memory snapshots | `generate_repo_memory_snapshot` upserts `planning_guidance` + `execution_guidance` on every feedback write |
| 5 | Epic-level outcome rollup | `generate_epic_outcome_rollup`, `POST /debug/epic-outcomes/{epic_key}`, `GET /debug/epic-outcomes/{epic_key}` |
| 6 | Prompt enrichment — planning | `get_planning_memory`, `plan_epic_breakdown(memory_context=…)` |
| 7 | Prompt enrichment — execution | `get_execution_memory`, `suggest_change(memory_context=…)`, `fix_change(memory_context=…)` |
| 8 | Manual guidance notes | `add_manual_memory`, `MemoryKind.MANUAL_NOTE`, `source` column, `POST /debug/memory`, `GET /debug/memory` |
| 9 | Feedback / memory inspection API | `GET /debug/feedback-events`, `POST /debug/memory/recompute` |
| 10 | End-to-end validation | All five Phase 7 scenarios verified against live data |

---

## New Components

### `app/feedback.py` (new file)

Central constants module. All string literals used in feedback capture, memory snapshots, and failure categorisation live here to prevent typo drift.

| Class / constant | Contents |
|---|---|
| `FeedbackSource` | `PLANNING_RUN`, `EXECUTION_RUN` |
| `FeedbackType` | 13 signal types (e.g. `PLANNING_APPROVED`, `TEST_STATUS`, `FAILURE_CATEGORY`) |
| `FailureCategory` | 10 categories (e.g. `TEST_FAILURE`, `MERGE_FAILURE`, `WORKER_INTERRUPTED`) |
| `MemoryScope` | `RUN`, `EPIC`, `REPO`, `GLOBAL` |
| `MemoryKind` | `PLANNING_GUIDANCE`, `EXECUTION_GUIDANCE`, `MANUAL_NOTE` |
| `MEMORY_MAX_BULLETS` | `5` |
| `MEMORY_MAX_CHARS` | `1000` |
| `categorize_execution_failure(test_status, merge_status, error_detail, current_step)` | Priority-ordered rule-based categoriser |
| `categorize_planning_failure(error_detail, current_step)` | Rule-based categoriser for epic_breakdown failures |

### `app/database.py` additions

| Function | Purpose |
|---|---|
| `record_planning_feedback(run_id)` | Writes feedback_events for a finished epic_breakdown run; triggers repo snapshot refresh |
| `record_execution_feedback(run_id)` | Writes feedback_events for a finished story_implementation run; triggers repo snapshot + epic rollup refresh |
| `generate_repo_memory_snapshot(repo_slug)` | Computes and upserts `planning_guidance` + `execution_guidance` snapshots from raw feedback; handles multi-project-key repos |
| `generate_epic_outcome_rollup(epic_key)` | Aggregates all Stories ever created from an Epic and their execution results into an `execution_guidance` snapshot at `scope_type='epic'` |
| `get_planning_memory(repo_slug, epic_key=None)` | Retrieves `manual_note` + `planning_guidance` + `execution_guidance` (+ optional epic-level) as a bounded bullet block for prompt injection |
| `get_execution_memory(repo_slug)` | Retrieves `manual_note` + `execution_guidance` as a bounded bullet block for prompt injection |
| `add_manual_memory(scope_type, scope_key, content)` | Upserts a `manual_note / source=human` snapshot; sends `manual_memory_added` Telegram event on first write |

### `app/worker.py` changes

`record_execution_feedback` is called at all three terminal exit paths of `_execute`, each guarded by `workflow_type == "story_implementation"`:

1. Unhandled exception path → `fail_run` then `record_execution_feedback`
2. Handler set `FAILED` internally → `record_execution_feedback` before return
3. Normal completion path → `_update_run_status(COMPLETED)` then `record_execution_feedback`

### `app/workflows.py` changes

**`epic_breakdown`**: before the Claude decomposition step, looks up the Story-type repo mapping for the project key, calls `get_planning_memory(repo_slug, issue_key)`, logs the injection size, and passes the context to `plan_epic_breakdown`. Non-fatal.

**`story_implementation`**: immediately after the mapping lookup, calls `get_execution_memory(mapping["repo_slug"])`, logs the injection size, and passes the context to both `suggest_change` and `fix_change`. Non-fatal.

**`epic_breakdown`** (failure paths): `record_planning_feedback` is now called after `fail_run` for the duplicate-blocked path and the Claude-decomposition-failed path — in addition to the existing REJECTED/REGENERATED paths handled by the Telegram webhook.

### `app/claude_client.py` changes

| Function | Change |
|---|---|
| `plan_epic_breakdown(issue_key, summary, memory_context="")` | When `memory_context` is non-empty, inserts a `Prior lessons from this repository:` block between the Epic title and the tool instruction |
| `suggest_change(…, memory_context="")` | Inserts the memory block after the file listings, before the action instruction |
| `fix_change(…, memory_context="")` | Inserts the memory block after the test output, before the fix instruction |

---

## Data Model Changes

### New table: `feedback_events`

One row per signal per run. Atomic and append-only.

| Column | Type | Purpose |
|---|---|---|
| `id` | SERIAL | Primary key |
| `source_type` | VARCHAR(50) | `planning_run` or `execution_run` |
| `source_run_id` | INTEGER | FK to `workflow_runs` |
| `epic_key` | VARCHAR(100) | Epic context for planning signals |
| `story_key` | VARCHAR(100) | Story key for execution signals |
| `repo_slug` | VARCHAR(200) | Enables repo-scoped aggregation |
| `feedback_type` | VARCHAR(100) | Signal name from `FeedbackType` |
| `feedback_value` | VARCHAR(500) | String-serialised value |
| `details_json` | TEXT | Optional structured evidence |
| `created_at` | TIMESTAMP | Write time |

### New table: `memory_snapshots`

One row per `(scope_type, scope_key, memory_kind)` — enforced by unique index.

| Column | Type | Purpose |
|---|---|---|
| `id` | SERIAL | Primary key |
| `scope_type` | VARCHAR(50) | `repo` or `epic` |
| `scope_key` | VARCHAR(200) | `repo_slug` or `epic_key` |
| `memory_kind` | VARCHAR(50) | `planning_guidance`, `execution_guidance`, or `manual_note` |
| `summary` | TEXT | Human-readable bullet-point text; this is what gets injected |
| `evidence_json` | TEXT | Structured evidence backing the summary (NULL for manual notes) |
| `source` | VARCHAR(20) | `derived` (auto-generated) or `human` (manual) |
| `created_at` | TIMESTAMP | First write |
| `updated_at` | TIMESTAMP | Last refresh |

### Unique index

```sql
CREATE UNIQUE INDEX idx_memory_snapshots_scope_kind
ON memory_snapshots (scope_type, scope_key, memory_kind)
```

All upserts use `ON CONFLICT (scope_type, scope_key, memory_kind) DO UPDATE`.

---

## API Endpoints Added

| Method | Path | Purpose |
|---|---|---|
| POST | `/debug/epic-outcomes/{epic_key}` | Generate (or refresh) Epic-level execution outcome rollup |
| GET | `/debug/epic-outcomes/{epic_key}` | Return stored Epic outcome snapshot |
| POST | `/debug/memory` | Create or update a human-authored memory note |
| GET | `/debug/memory` | List memory snapshots, optionally filtered by `scope_type` and/or `scope_key` |
| GET | `/debug/feedback-events` | List raw feedback_events rows (filters: `source_type`, `repo_slug`, `feedback_type`, `source_run_id`; max 100) |
| POST | `/debug/memory/recompute` | Force-refresh a derived snapshot: `scope_type=repo` → `generate_repo_memory_snapshot`; `scope_type=epic` → `generate_epic_outcome_rollup` |

---

## Key Design Decisions

**`on_write` refresh mode**
Snapshots are regenerated synchronously inside `record_planning_feedback` and `record_execution_feedback`, immediately after each feedback write. There is no background job or polling. This keeps prompts current without a scheduler.

**Feedback before memory**
Raw `feedback_events` rows are written first, then snapshot functions read them. This means snapshot logic can always be replayed from the raw events by calling `generate_repo_memory_snapshot` or the recompute endpoint.

**Non-fatal enrichment**
Memory retrieval is wrapped in `try/except` in both `epic_breakdown` and `story_implementation`. A DB failure during retrieval produces a warning log and falls back to a prompt with no memory block — it never blocks a workflow from running.

**Manual notes take priority**
`get_planning_memory` and `get_execution_memory` query `manual_note` first, then derived snapshots. This guarantees human guidance always occupies one of the first bullets and can never be displaced by derived content when the cap is reached.

**Multi-project-key repo support**
`generate_repo_memory_snapshot` resolves all `jira_project_key` values mapped to a repo via `SELECT DISTINCT`. Planning stats use `LIKE ANY(array_of_patterns)` to match events for all project keys (e.g. both `KAN-%` and `SANDBOX-%`).

**Epic rollup query**
Uses `DISTINCT ON (po.created_issue_key)` + `LEFT JOIN LATERAL` to find the most recent `workflow_run` per Story. Filters on `created_issue_key IS NOT NULL` (not `status='CREATED'`) to capture all Stories ever created, including those whose planning_outputs were later reset to `REJECTED` by a REGENERATE.

**`source` column distinguishability**
Manual notes carry `source='human'`; all auto-generated snapshots carry `source='derived'`. Both are visible in `GET /debug/memory`, making audit trivial.

---

## Failure Categories

Defined in `app/feedback.py:FailureCategory`. Assigned by rule-based functions at feedback-capture time.

| Category | Detection logic |
|---|---|
| `test_failure` | `test_status IN ('FAILED', 'ERROR')` |
| `syntax_failure` | `"syntax error"` or `"syntaxerror"` in `error_detail` |
| `apply_validation_failure` | `"path traversal"`, `"original text not found"`, `"no-op"`, etc. in `error_detail` |
| `merge_failure` | `merge_status='FAILED'` or `"no open pr found"` in `error_detail` |
| `worker_interrupted` | `"interrupted by worker restart"` or `"worker restarted mid-run"` in `error_detail` |
| `duplicate_blocked` | `"duplicate breakdown blocked"` in `error_detail` |
| `approval_rejected` | `"rejected by user"` in `error_detail` |
| `approval_regenerated` | `"regeneration requested"` in `error_detail` |
| `jira_creation_failure` | `"jira creation failed"` in `error_detail` or `current_step='creating_jira_issues'` |
| `unknown` | No pattern matched |

---

## Telegram Event Types Added

| Event | When |
|---|---|
| `planning_feedback_recorded` | Feedback events written for a planning run |
| `execution_feedback_recorded` | Feedback events written for an execution run |
| `memory_snapshot_updated` | A new derived snapshot created (first write only) |
| `epic_outcome_ready` | Epic-level outcome rollup created for the first time |
| `manual_memory_added` | A human-authored note stored for the first time |

---

## Validation Results (Iteration 10)

| Scenario | Evidence |
|---|---|
| **A** — Approved Epic produces feedback | Run 79 (KAN-21): `planning_approved`, `stories_proposed_count=3`, `stories_created_count=3`, `approval_latency_seconds=6`, `planning_regenerated` all recorded |
| **B** — Execution outcomes generate memory | Runs 90–91: 5 signal types each; repo snapshot derived: *4/5 completed (80%), avg 0.6 retries, most common failure: test_failure* |
| **C** — New planning run uses prior lessons | `get_planning_memory` returns 260-char / 5-bullet block including manual note; injected into `plan_epic_breakdown` user message |
| **D** — New execution run uses prior lessons | `get_execution_memory` returns 243-char / 5-bullet block; injected into both `suggest_change` and `fix_change` |
| **E** — Manual note influences prompt | `source=human` note leads both planning and execution memory blocks; confirmed first-position in both retrieval functions |

Live snapshot counts at Phase 7 close:

| Scope | Kind | Source | Content |
|---|---|---|---|
| `repo / suyog19/sandbox-fastapi-app` | `execution_guidance` | derived | 4/5 runs completed, avg 0.60 retries, avg 1.6 files, most common failure: test_failure |
| `repo / suyog19/sandbox-fastapi-app` | `planning_guidance` | derived | 6 planning runs: 1 approved, 4 rejected, 1 regenerated; avg 4.7 proposed, 3.0 created |
| `repo / suyog19/sandbox-fastapi-app` | `manual_note` | human | "Stories in this repo should stay small and avoid test edits unless explicitly requested" |
| `epic / KAN-10` | `execution_guidance` | derived | 15 Stories created; 7 executed: 5 completed, 2 failed; 5 merged; 8 not yet executed |

---

## What Phase 7 Did Not Build (by design)

- Global-scope memory (deferred — no cross-repo patterns exist yet)
- Run-scope memory (deferred — single-run signals are not worth injecting back into the same run)
- Automatic memory pruning or decay (summaries are always recomputed from raw events — no TTL needed)
- Semantic similarity / vector search for memory retrieval (rule-based aggregation is sufficient and fully auditable)
- Prompt A/B testing or memory effectiveness measurement
- Automatic rollback of memory notes that prove harmful
