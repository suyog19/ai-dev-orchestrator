# Phase 8 Summary — Reviewer Agent

## Goal

Phase 7 made the system learn from its history. The Developer Agent still made the final call on whether a PR was safe to merge, using test pass status as the primary gate.

Phase 8 extracts review judgment into an independent **Reviewer Agent**. The Reviewer Agent receives the full story context, diff, test result, and repo memory — and produces a structured verdict that gates auto-merge. The Developer Agent no longer decides whether its own output is good enough to ship.

---

## What Was Built

### Reviewer Agent pipeline step

```
story_implementation completes implementation + tests
  → commit + push
  → PR created on GitHub
  → _build_review_package() assembles context
  → review_pr() calls Reviewer Agent (Claude, forced tool_use)
  → store_agent_review() persists verdict + updates workflow_runs.review_status
  → _format_review_comment() formats verdict as GitHub markdown
  → post_pr_comment() posts comment to GitHub PR
  → send_message() sends Telegram notification
  → auto-merge gate checks review_status == APPROVED_BY_AI
```

The Reviewer Agent is independent: it receives no reference to intermediate Developer Agent state. It reasons only from the assembled package — story intent, diff, test output, and memory guidance.

### Structured verdict via forced tool_use

Claude is called with `tool_choice={"type": "tool", "name": "submit_review"}`. If Claude does not return a `submit_review` tool block, `review_pr()` raises a `RuntimeError`. The tool schema enforces:

- `review_status`: enum `APPROVED_BY_AI | NEEDS_CHANGES | BLOCKED`
- `risk_level`: enum `LOW | MEDIUM | HIGH`
- `summary`: string (1–2 sentence verdict narrative)
- `findings`: array of `{severity, category, message}` objects
- `blocking_reasons`: array of strings (required when `BLOCKED`)
- `recommendations`: array of strings

There is no `ERROR` verdict from Claude — `ERROR` is assigned internally when the Claude call fails or returns no tool block.

### Non-fatal error handling

Every step of the review pipeline is individually guarded:

| Step | On failure |
|---|---|
| `review_pr()` call fails | `verdict = {review_status: ERROR, ...}`; workflow continues |
| `store_agent_review()` fails | Logged as WARNING; workflow continues |
| `post_pr_comment()` fails | Logged as WARNING; workflow continues |
| Telegram send fails | Logged as WARNING; workflow continues |

A review failure never fails the workflow run. The run completes with `review_status=ERROR` and `merge_status=SKIPPED`.

---

## Iterations Completed

| # | Iteration | What it delivered |
|---|---|---|
| 0 | Schema and constants baseline | `agent_reviews` table; four review columns on `workflow_runs`; `ReviewStatus`, `ReviewRiskLevel`, `AgentName` constants; `REVIEW_REQUIRED=True`, `REVIEW_BLOCKS_MERGE=True` config |
| 1 | Reviewer Agent client function | `review_pr(story_context, pr_context, diff, test_result, memory_context)` in `claude_client.py`; `REVIEWER_PROMPT` system prompt; `_REVIEW_TOOL` schema with forced `tool_choice` |
| 2 | Review input package | `_build_review_package()` in `workflows.py`; `get_issue_details()` Jira fetch; `_extract_text_from_adf()` + `_parse_acceptance_criteria()` in `jira_client.py` |
| 3 | Wired into workflow | `review_pr()` called after PR creation; verdict stored via `store_agent_review()`; `workflow_runs.review_status` updated; Telegram `review_completed` sent |
| 4 | GitHub PR comment | `post_pr_comment()` in `github_api.py`; `_format_review_comment()` in `workflows.py` with emoji verdict, risk, findings, blocking reasons, recommendations |
| 5 | Merge gate | `review_status == APPROVED_BY_AI` added to auto-merge condition; `BLOCKED` → `merge_status=BLOCKED_BY_REVIEW`; `NEEDS_CHANGES` and `ERROR` → `merge_status=SKIPPED` with reason logged |
| 6 | Negative-path validation | Live Claude API calls on dev EC2 confirmed: `FAILED` tests → `BLOCKED`; story mismatch → `BLOCKED`; `NOT_RUN` tests → `NEEDS_CHANGES` (not `APPROVED_BY_AI`) |
| 7 | Feedback event integration | `record_execution_feedback()` extended to emit `review_status`, `review_risk_level`, `review_approved`, `review_needs_changes`, `review_blocked` into `feedback_events` |
| 8 | Review inspection API | `GET /debug/agent-reviews` (filterable); `GET /debug/workflow-runs/{run_id}/reviews`; `list_agent_reviews()` in `database.py` |
| 9 | End-to-end validation | 18/18 integration checks pass across all four verdict scenarios on dev EC2 |

---

## New and Changed Components

### `app/feedback.py` additions

| Class / constant | Contents added |
|---|---|
| `AgentName` | `REVIEWER_AGENT = "reviewer_agent"` |
| `ReviewStatus` | `APPROVED_BY_AI`, `NEEDS_CHANGES`, `BLOCKED`, `ERROR` |
| `ReviewRiskLevel` | `LOW`, `MEDIUM`, `HIGH` |
| `REVIEW_REQUIRED` | `True` — every `story_implementation` run triggers a review |
| `REVIEW_BLOCKS_MERGE` | `True` — `APPROVED_BY_AI` required for auto-merge |
| `FeedbackType` | Added: `REVIEW_STATUS`, `REVIEW_RISK_LEVEL`, `REVIEW_APPROVED`, `REVIEW_NEEDS_CHANGES`, `REVIEW_BLOCKED` |

### `app/database.py` additions

| Function / migration | Purpose |
|---|---|
| Migration: 4 new columns on `workflow_runs` | `review_status VARCHAR(30) NULL`, `review_required BOOLEAN NOT NULL DEFAULT TRUE`, `review_completed_at TIMESTAMP NULL`, `review_summary TEXT NULL` |
| Migration: `agent_reviews` table | Full schema — see Data Model section below |
| `store_agent_review(run_id, verdict, pr_number, pr_url, repo_slug, story_key, model_used)` | Inserts `agent_reviews` row and updates `workflow_runs.review_status` + `review_completed_at` in one transaction; returns new review `id` |
| `list_agent_reviews(run_id, repo_slug, review_status, limit)` | Returns filtered `agent_reviews` rows with JSON fields decoded; used by inspection API |
| `record_execution_feedback()` extended | Now reads `review_status` from `workflow_runs` + `risk_level` from `agent_reviews`; emits up to 4 additional feedback events per run |

### `app/claude_client.py` additions

| Addition | Detail |
|---|---|
| `REVIEWER_PROMPT` | System prompt instructing Claude to review across 4 dimensions: story alignment, code quality, test awareness, diff risk. Strict verdict rules: `BLOCKED` if tests failed or diff contradicts story; `NEEDS_CHANGES` if plausible but risky; `APPROVED_BY_AI` only when all dimensions are acceptable |
| `_REVIEW_TOOL` | `submit_review` tool schema with all required fields and enums. Diff truncated to 8 000 chars. Test output excerpt: last 30 lines |
| `review_pr(story_context, pr_context, diff, test_result, memory_context)` | Builds user message from the five input sections; calls Claude with `tool_choice=submit_review`; raises `RuntimeError` if no tool block returned; returns parsed verdict dict |

### `app/jira_client.py` additions

| Addition | Detail |
|---|---|
| `_extract_text_from_adf(node)` | Recursive Atlassian Document Format text extractor; handles nested `content` arrays |
| `_AC_STOP_HEADERS` | `{"rationale", "dependencies", "risks", "generated by"}` — stops AC parsing at next section |
| `_parse_acceptance_criteria(text_lines)` | Extracts bullet items following an `Acceptance Criteria` heading; stops at any known section header |
| `get_issue_details(issue_key)` | Fetches `summary`, `description` (plain text), and `acceptance_criteria` (list) from Jira REST API v3. Non-fatal — returns empty fallback dict if any env var is missing or fetch fails |

### `app/github_api.py` additions

| Function | Detail |
|---|---|
| `post_pr_comment(repo_name, pr_number, body)` | `POST /repos/{slug}/issues/{pr_number}/comments`; returns `{id, html_url}`; raises on HTTP errors |

### `app/workflows.py` additions

| Addition | Detail |
|---|---|
| `_format_review_comment(verdict)` | Renders verdict as GitHub markdown. Header includes emoji (✅/⚠️/🚫/❌) + verdict + risk. Sections: Summary, Findings (with severity prefix), Blocking Reasons, Recommendations, footer line |
| `_build_review_package(...)` | Calls `get_issue_details()`, assembles 5-key dict: `story_context`, `pr_context`, `diff`, `test_result`, `memory_context`. Output maps directly to `review_pr(**pkg)` kwargs |
| Review pipeline in `story_implementation` | After PR creation: step `building_review_package` → step `reviewing` (calls `review_pr`, stores, posts comment, sends Telegram) |
| Updated merge gate | `auto_merge_ok` now requires `review_status == ReviewStatus.APPROVED_BY_AI`. Separate `elif` branch for `BLOCKED` writes `merge_status=BLOCKED_BY_REVIEW`. All other non-approved statuses write `merge_status=SKIPPED` with a reason string |

---

## Data Model Changes

### New table: `agent_reviews`

One row per Reviewer Agent verdict. FK to `workflow_runs`.

| Column | Type | Purpose |
|---|---|---|
| `id` | SERIAL | Primary key |
| `run_id` | INTEGER | FK to `workflow_runs(id)` |
| `pr_number` | INTEGER | GitHub PR number |
| `pr_url` | TEXT | GitHub PR URL |
| `repo_slug` | VARCHAR(200) | `owner/repo` |
| `story_key` | VARCHAR(100) | Jira Story key |
| `agent_name` | VARCHAR(100) | Always `reviewer_agent` (default) |
| `review_status` | VARCHAR(30) NOT NULL | `APPROVED_BY_AI` \| `NEEDS_CHANGES` \| `BLOCKED` \| `ERROR` |
| `risk_level` | VARCHAR(20) | `LOW` \| `MEDIUM` \| `HIGH` |
| `summary` | TEXT | 1–2 sentence verdict narrative |
| `findings_json` | TEXT | JSON array of `{severity, category, message}` |
| `recommendations_json` | TEXT | JSON array of recommendation strings |
| `blocking_reasons_json` | TEXT | JSON array of blocking reason strings |
| `model_used` | VARCHAR(100) | Model ID used for this verdict |
| `memory_snapshot_ids_json` | TEXT | Reserved for future memory provenance tracking |
| `created_at` | TIMESTAMP | Write time |
| `updated_at` | TIMESTAMP | Last update |

### Extended `workflow_runs` columns

| Column | Type | Purpose |
|---|---|---|
| `review_status` | VARCHAR(30) NULL | Mirrors the latest `agent_reviews.review_status` for this run |
| `review_required` | BOOLEAN NOT NULL DEFAULT TRUE | Config flag; always true in Phase 8 |
| `review_completed_at` | TIMESTAMP NULL | Set atomically with `agent_reviews` insert |
| `review_summary` | TEXT NULL | Reserved for a short verdict summary on the run row |

### Updated `merge_status` values

| Value | When set |
|---|---|
| `MERGED` | Auto-merge succeeded (`APPROVED_BY_AI` + tests passed + ≤3 files) |
| `SKIPPED` | Auto-merge disabled, tests failed, `NEEDS_CHANGES`, or `ERROR` verdict |
| `BLOCKED_BY_REVIEW` | Reviewer Agent returned `BLOCKED` |
| `FAILED` | PR creation or GitHub merge API call failed |

---

## API Endpoints Added

| Method | Path | Purpose |
|---|---|---|
| GET | `/debug/agent-reviews` | List `agent_reviews` rows. Query params: `run_id`, `repo_slug`, `review_status`, `limit` (default 20) |
| GET | `/debug/workflow-runs/{run_id}/reviews` | All Reviewer Agent verdicts for one run. Returns 404 if no review row exists for that run |

---

## Merge Policy

**Before Phase 8:**
```python
auto_merge_ok = (
    mapping.get("auto_merge_enabled")
    and final_test_result["status"] == "PASSED"
    and applied.get("applied", False)
    and applied.get("count", 0) <= MAX_FILES_FOR_AUTOMERGE
)
```

**After Phase 8:**
```python
auto_merge_ok = (
    mapping.get("auto_merge_enabled")
    and final_test_result["status"] == "PASSED"
    and applied.get("applied", False)
    and applied.get("count", 0) <= MAX_FILES_FOR_AUTOMERGE
    and review_status == ReviewStatus.APPROVED_BY_AI
)

# Downstream:
if auto_merge_ok:
    merge_status = "MERGED"
elif review_status == ReviewStatus.BLOCKED:
    merge_status = "BLOCKED_BY_REVIEW"
else:
    merge_status = "SKIPPED"   # NEEDS_CHANGES, ERROR, or config gate
```

---

## GitHub PR Comment Format

```
## Reviewer Agent Verdict: ✅ APPROVED_BY_AI

**Risk:** LOW

### Summary
The implementation correctly adds the /status endpoint as specified in the Story,
and all tests pass.

### Findings
- [INFO] story_alignment: Implementation matches Story summary and acceptance criteria.
- [INFO] test_awareness: Tests passed (5 passed in 0.42s).

### Blocking Reasons
None

### Recommendations
- Consider adding an edge-case test for empty payloads in a future story.

---
*Reviewed by Reviewer Agent | Run #78 | Model: claude-sonnet-4-6*
```

Emoji mapping: `APPROVED_BY_AI` → ✅, `NEEDS_CHANGES` → ⚠️, `BLOCKED` → 🚫, `ERROR` → ❌

---

## Reviewer Agent Prompt (REVIEWER_PROMPT) — Key Rules

The system prompt instructs Claude to evaluate across four dimensions:

1. **Story alignment** — Does the diff address the Jira Story summary and acceptance criteria? Is scope too broad or narrow?
2. **Code quality** — Clean implementation? Obvious bugs? Unsafe assumptions? Unnecessary refactor?
3. **Test awareness** — Were tests run? Did they pass? Are modified tests legitimate?
4. **Diff risk** — File count, file types, high-risk areas, potential side effects.

Verdict rules embedded in the prompt:
- Return `BLOCKED` if tests failed, tests were intentionally disabled, or the diff clearly contradicts the Story.
- Return `NEEDS_CHANGES` if the change is plausible but incomplete, risky, or has fixable issues.
- Return `APPROVED_BY_AI` only when story alignment, code quality, test awareness, and diff risk are all acceptable.

---

## Feedback Events Added

`record_execution_feedback()` now reads `workflow_runs.review_status` and `agent_reviews.risk_level` for the run and emits:

| Feedback type | Value | When |
|---|---|---|
| `review_status` | `APPROVED_BY_AI` / `NEEDS_CHANGES` / `BLOCKED` / `ERROR` | Always, if `review_status` is not NULL |
| `review_risk_level` | `LOW` / `MEDIUM` / `HIGH` | When a risk_level value exists in `agent_reviews` |
| `review_approved` | `"true"` | When `review_status == APPROVED_BY_AI` |
| `review_needs_changes` | `"true"` | When `review_status == NEEDS_CHANGES` |
| `review_blocked` | `"true"` | When `review_status == BLOCKED` |

These signals are available to `generate_repo_memory_snapshot()` for future memory snapshot generation.

---

## Telegram Event Types Added

| Event | When |
|---|---|
| `review_completed` | Reviewer Agent returned a verdict (any status) |
| `review_error` | Reviewer Agent call failed; verdict defaulted to ERROR |
| `merge_blocked_by_review` | `BLOCKED` verdict prevented auto-merge |

---

## Validation Results (Iteration 9)

### Negative-path scenarios (Iteration 6 — live Claude API calls on dev EC2)

| Scenario | Input | Expected | Result |
|---|---|---|---|
| A | `test_result.status = FAILED` | `BLOCKED` | `BLOCKED` ✅ |
| B | Story about `/status`; diff modifies auth logic | `BLOCKED` or `NEEDS_CHANGES` | `BLOCKED` ✅ |
| C | `test_result.status = NOT_RUN` | Not `APPROVED_BY_AI` | `NEEDS_CHANGES` ✅ |

### E2E integration (Iteration 9 — 18/18 checks on dev EC2)

| Scenario | Inputs | Expected behaviour | Checks |
|---|---|---|---|
| A — Approved | `review_status=APPROVED_BY_AI`, `risk_level=LOW`, `merge_status=MERGED` | Review stored; API returns verdict; feedback emits `review_approved=true`, `review_risk_level=LOW` | 9/9 ✅ |
| B — Needs changes | `review_status=NEEDS_CHANGES`, `risk_level=MEDIUM`, `merge_status=SKIPPED` | API filter by `run_id+review_status` works; `merge_status=SKIPPED` preserved | 3/3 ✅ |
| C — Blocked | `review_status=BLOCKED`, `risk_level=HIGH`, `merge_status=BLOCKED_BY_REVIEW` | `merge_status=BLOCKED_BY_REVIEW` written; API returns `blocking_reasons` | 3/3 ✅ |
| D — Error | `review_status=ERROR`, no `agent_reviews` row, `merge_status=SKIPPED` | `GET .../reviews` returns 404; `workflow_runs` shows honest state | 3/3 ✅ |

---

## Key Design Decisions

**Independent agent, not inline judgment**
The Reviewer Agent receives no reference to the Developer Agent's intermediate reasoning. It reads only the assembled package — story, diff, test result, memory. This prevents the reviewer from being anchored to the developer's framing.

**Forced tool_use schema**
Using `tool_choice={"type": "tool", "name": "submit_review"}` means Claude cannot return a freeform verdict. The response is always a structured dict or an exception. This eliminates the need for output parsing logic and makes verdict extraction deterministic.

**`_build_review_package` → `review_pr(**pkg)` unpacking**
`_build_review_package` returns exactly the five keys that `review_pr` accepts as kwargs. The call site is `review_pr(**_build_review_package(...))`. This keeps the coupling explicit and the function signatures self-documenting.

**Jira AC parsing with stop-headers**
`_parse_acceptance_criteria` stops collection at the first line that begins with any of `{"rationale", "dependencies", "risks", "generated by"}`. Without stop-headers, footer text from the orchestrator's own ADF body bleeds into the AC list and corrupts the Reviewer Agent's story alignment check.

**`BLOCKED_BY_REVIEW` as a distinct merge_status**
Rather than mapping `BLOCKED` to the generic `SKIPPED`, it gets its own `merge_status` value. This makes it distinguishable in feedback events, DB queries, and Telegram without adding a separate column. `SKIPPED` still means "auto-merge conditions not met for non-review reasons."

**Non-fatal review pipeline**
Every step after PR creation is individually try/caught. A failed Claude call, failed DB write, or failed GitHub comment produces a warning log and a safe fallback state — not a failed run. The workflow always reaches a terminal status.

**No `ERROR` verdict from Claude**
`ERROR` is an internal sentinel assigned by the orchestrator when the Claude call itself fails. The `_REVIEW_TOOL` schema only includes `APPROVED_BY_AI`, `NEEDS_CHANGES`, and `BLOCKED` as valid values for Claude to return. This keeps the Claude contract clean.

**Review feedback emitted at feedback-capture time, not at review time**
Review signals are written by `record_execution_feedback()` (called at run completion) rather than inline during the review step. This is consistent with how all other execution signals work and ensures atomic feedback capture for the complete run state.

---

## What Phase 8 Did Not Build (by design)

- Formal GitHub PR reviews (`POST /repos/{slug}/pulls/{pr}/reviews`) — top-level comment is sufficient for Phase 8
- Required GitHub status checks — not needed for the current single-reviewer model
- Inline PR comments per line — deferred; top-level comment covers the verdict
- Automatic human approval override — `BLOCKED` verdicts require manual PR intervention
- Test Quality Agent — a separate agent that evaluates test adequacy; deferred to a future phase
- Architect Agent — deferred
- Multi-agent orchestration framework — Phase 8 extracts one agent; framework comes later
- Review memory learning — review signals are captured in `feedback_events` but `generate_repo_memory_snapshot` does not yet incorporate them into the summary bullets; deferred to Phase 9
- `GET /debug/agent-reviews/{id}` — single-review detail endpoint; the list endpoint is sufficient for now
