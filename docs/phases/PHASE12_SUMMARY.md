# Phase 12 Summary — Clarification Loop

## Objective

Add a human clarification loop so workflows can pause, ask a precise question on Telegram, store the answer, and resume from the correct step.

---

## Iterations Completed

### Iteration 0 — Schema and constants

Added `clarification_requests` table with `PENDING / ANSWERED / CANCELLED / EXPIRED` lifecycle. Extended `workflow_runs` with `waiting_for_clarification` (BOOLEAN) and `active_clarification_id` (INTEGER). Added `ClarificationStatus`, `ClarificationContextKey`, clarification-related `FeedbackType` constants, and `CLARIFICATION_ENABLED` / `CLARIFICATION_TIMEOUT_HOURS` config in `app/feedback.py`. All migrations are idempotent (`ADD COLUMN IF NOT EXISTS`, `CREATE TABLE IF NOT EXISTS`).

---

### Iteration 1 — Clarification service layer

Added all DB helpers in `app/database.py`:

| Function | Purpose |
|---|---|
| `create_clarification_request()` | Insert row with timeout, options, context_key |
| `mark_clarification_answered()` | Set ANSWERED + answer_text + answered_at |
| `mark_clarification_cancelled()` | Set CANCELLED |
| `mark_clarification_expired()` | Set EXPIRED (used internally by expire logic) |
| `get_active_clarification()` | Return PENDING or ANSWERED row for a run |
| `get_clarification_by_id()` | Return single row by id |
| `list_pending_clarifications()` | Return all PENDING rows |
| `expire_stale_clarifications()` | Mark past-expires_at PENDING rows EXPIRED and fail their runs |
| `update_clarification_telegram_id()` | Store telegram message_id after sending |
| `list_clarifications()` | Return rows with status/run_id filter |
| `get_run_state()` | Return pr_url, working_branch, test fields for resume |

---

### Iteration 2 — Telegram question sender

Added in `app/telegram.py`:
- `send_clarification_request(clarification: dict) -> str | None` — formats and sends a structured Telegram message with question, numbered options, and `ANSWER / CANCEL / CLARIFY` reply instructions; returns message_id if available.

---

### Iteration 3 — Telegram answer parser

Added in `app/telegram.py`:
- `parse_clarification_command(text) -> tuple[str, int, str | None] | None` — parses `ANSWER <id> <text>`, `CANCEL <id>`, `CLARIFY <id>` using case-insensitive regex.

Extended `app/webhooks.py`:
- `_handle_clarification_command()` async helper — validates chat id, clarification id range, answer non-empty, status=PENDING; rejects with security events on failure; handles ANSWER (mark answered + resume), CANCEL (mark cancelled + fail run), CLARIFY (resend question).
- Clarification commands are checked **before** approval commands in the Telegram webhook.

---

### Iteration 4 — Workflow pause / resume mechanics

New file `app/clarification.py`:

| Function | Purpose |
|---|---|
| `ClarificationRequested(Exception)` | Exception caught by worker (not treated as failure); carries `clarification_id` and `question` |
| `is_clarification_enabled()` | Checks `CLARIFICATION_ENABLED` constant + `clarification_enabled` control flag |
| `pause_for_clarification()` | Creates DB row, sets `WAITING_FOR_USER_INPUT`, sends Telegram, raises `ClarificationRequested` |
| `resume_workflow_after_clarification()` | Re-enqueues same `run_id` so worker picks it up |

Updated `app/worker.py`:
- Added `except ClarificationRequested` handler (not treated as failure; logs + notifies Telegram).
- Added `expire_stale_clarifications()` call on startup.
- Added `WAITING_FOR_USER_INPUT` to terminal-status guard.

Updated `app/dispatcher.py`:
- Added `WAITING_FOR_USER_INPUT` to active-run duplicate check.
- Added `summary` column to `workflow_runs` INSERT.

---

### Iteration 5 — Epic planning clarification checkpoints

Added helpers in `app/workflows.py`:
- `_check_epic_vagueness(summary, description)` — returns a clarification question if Epic summary is < 4 words or description is absent/very short; returns `None` for well-specified Epics.

In `epic_breakdown()`:
- **Fetch Jira details early** (`get_issue_details`) to inspect Epic description.
- **Resume detection**: `get_active_clarification(run_id)` → if ANSWERED with any context_key, extract `answer_text` and inject into `plan_memory`.
- **Vagueness checkpoint**: if no answered clarification and `_check_epic_vagueness()` returns a question → `pause_for_clarification()` with `PRE_PLANNING` context key.
- `plan_epic_breakdown()` called with `plan_memory` (includes injected answer on resume).

---

### Iteration 6 — Story implementation clarification checkpoints

Added helpers in `app/workflows.py`:
- `_check_story_ambiguity(summary, story_details)` — returns a clarification question if Story has no acceptance criteria and no description; returns `None` for well-specified Stories.

In `story_implementation()`:
- **Fetch story_details early** (before clone) for ambiguity check.
- **Resume detection**: `get_active_clarification(run_id)` → if ANSWERED, extract `answer_text` and append to `suggest_memory`.
- **Ambiguity checkpoint**: if no answered clarification and `_check_story_ambiguity()` returns a question → `pause_for_clarification()` with `PRE_SUGGEST` context key.
- `suggest_change()` called with `suggest_memory` (includes injected answer on resume).

---

### Iteration 7 — Review agent clarification support

Updated `app/claude_client.py`:
- Added optional `needs_clarification`, `clarification_question`, `clarification_context_summary`, `clarification_options` fields to `_REVIEW_TOOL` and `_ARCHITECTURE_TOOL` schemas (not in `required`; tools still always return main verdict fields).
- Updated `REVIEWER_PROMPT` and `ARCHITECTURE_PROMPT` to describe when agents may set `needs_clarification=true`.

Added to `app/github_api.py`:
- `get_pr_diff(repo_name, pr_number)` — fetches unified diff from GitHub for review-stage resume.

Updated `app/workflows.py`:
- `_story_review_and_release()` — helper that reconstructs review context from DB + GitHub diff and runs Reviewer + Architecture agents + Release Gate without re-running implementation. Used on PRE_REVIEW resume.
- Skip-to-review block at the start of `story_implementation()`: if `pr_url` is set in the run AND there's an answered PRE_REVIEW clarification → delegate to `_story_review_and_release()` and return.
- After `review_pr()` returns: if `needs_clarification=True` → `pause_for_clarification()` with `PRE_REVIEW` context key.
- After `review_architecture()` returns: same pattern.

---

### Iteration 8 — Timeout and expiry

Worker startup already calls `expire_stale_clarifications()` (Iteration 4). Added a periodic check in the worker loop (every ~720 iterations, ~1 hour at 5s poll interval) to expire stale clarifications without requiring a restart.

`expire_stale_clarifications()` marks past-`expires_at` PENDING rows as EXPIRED and fails their `workflow_runs` with `error_detail = "Clarification timed out — no answer received"`.

---

### Iteration 9 — Audit and feedback integration

Updated `record_execution_feedback()` in `app/database.py`:
- Queries `clarification_requests` for the run after writing other events.
- Emits: `clarification_count`, `clarification_requested`, `clarification_answered`, `clarification_cancelled`, `clarification_expired` feedback events as applicable.

Updated `app/webhooks.py`:
- `CANCEL` command now records a `clarification_cancelled` security event (join existing `telegram_rejected` events for invalid commands and empty answers).

---

### Iteration 10 — Inspection APIs

Added admin-protected endpoints in `app/main.py`:

| Method | Path | Purpose |
|---|---|---|
| GET | `/debug/clarifications` | List clarifications (filter: `status`, `run_id`, `limit`) |
| GET | `/debug/clarifications/{id}` | Single clarification detail |
| POST | `/debug/clarifications/{id}/answer` | Admin answer + resume workflow |
| POST | `/debug/clarifications/{id}/cancel` | Admin cancel + fail workflow |
| POST | `/debug/clarifications/{id}/resend` | Resend Telegram question |

All endpoints require `X-Orchestrator-Admin-Key` header (enforced by existing `admin_key_middleware`).

---

### Iteration 11 — End-to-end validation

All 6 scenarios validated on EC2 dev environment (38/38 tests passed):

| Scenario | Result |
|---|---|
| A — Epic vagueness → question → admin answer → resumed | PASS |
| B — Story ambiguity → question → answer injected into suggest context | PASS |
| C — Reviewer/Architecture tool schema has needs_clarification | PASS |
| D — Cancel via admin API → workflow marked FAILED | PASS |
| E — Timeout → expire_stale_clarifications → workflow FAILED | PASS |
| F — Invalid commands rejected (APPROVE not matched, empty answer, non-existent id, answer to EXPIRED) | PASS |

---

## Architecture Summary

### State machine

```
RUNNING
→ WAITING_FOR_USER_INPUT  (pause_for_clarification)
→ RUNNING                  (resume_workflow_after_clarification)
→ COMPLETED / FAILED

WAITING_FOR_USER_INPUT
→ FAILED                   (cancel / timeout / expiry)
```

### Clarification lifecycle

```
create_clarification_request()  →  PENDING
mark_clarification_answered()   →  ANSWERED  →  resume enqueued
mark_clarification_cancelled()  →  CANCELLED →  run FAILED
expire_stale_clarifications()   →  EXPIRED   →  run FAILED
```

### Context keys

| Key | Used when |
|---|---|
| `PRE_PLANNING` | Before `plan_epic_breakdown()` in `epic_breakdown()` |
| `PRE_SUGGEST` | Before `suggest_change()` in `story_implementation()` |
| `PRE_REVIEW` | When Reviewer or Architecture Agent requests clarification |

### Resume paths

| Context key | Resume action |
|---|---|
| `PRE_PLANNING` | Epic re-runs from start; answered clarification injected into planning memory |
| `PRE_SUGGEST` | Story re-runs from start; answered clarification injected into suggestion memory |
| `PRE_REVIEW` | Skip-to-review detected via `pr_url` in run state; `_story_review_and_release()` runs reviews + release gate from GitHub diff |

---

## Files Changed

| File | Changes |
|---|---|
| `app/feedback.py` | `ClarificationStatus`, `ClarificationContextKey`, clarification `FeedbackType` entries, config constants |
| `app/database.py` | Schema migrations, all clarification service functions, `get_run_state()`, updated `list_clarifications()`, updated `record_execution_feedback()` |
| `app/telegram.py` | `parse_clarification_command()`, `send_clarification_request()` |
| `app/clarification.py` | NEW: `ClarificationRequested`, `is_clarification_enabled()`, `pause_for_clarification()`, `resume_workflow_after_clarification()` |
| `app/webhooks.py` | `_handle_clarification_command()`, clarification commands before approval commands, rate limiting, security events |
| `app/worker.py` | `ClarificationRequested` handler, startup expiry, periodic expiry |
| `app/dispatcher.py` | `WAITING_FOR_USER_INPUT` in duplicate check, `summary` in INSERT |
| `app/workflows.py` | `_check_epic_vagueness()`, `_check_story_ambiguity()`, `_story_review_and_release()`, checkpoints in both workflows, imports, skip-to-review logic |
| `app/claude_client.py` | `needs_clarification` fields in `_REVIEW_TOOL` and `_ARCHITECTURE_TOOL`, updated prompts |
| `app/github_api.py` | `get_pr_diff()` |
| `app/main.py` | 5 clarification inspection/management endpoints |
| `docs/phases/PHASE12_EXECUTION_GUIDE.md` | Baseline document committed before implementation |

---

## Commits

- `feat: Phase 12 Iteration 0 — clarification schema and constants`
- `feat: Phase 12 Iteration 1 — clarification service layer`
- `feat: Phase 12 Iterations 2+3 — Telegram sender and parser`
- `feat: Phase 12 Iterations 3+4 — pause/resume mechanics and worker integration`
- `feat: Phase 12 Iterations 5+6 — clarification checkpoints for Epic and Story`
- `feat: Phase 12 Iteration 7 — review agent clarification support`
- `feat: Phase 12 Iteration 8 — periodic stale clarification expiry`
- `feat: Phase 12 Iteration 9 — clarification audit and feedback integration`
- `feat: Phase 12 Iteration 10 — clarification inspection and admin APIs`
