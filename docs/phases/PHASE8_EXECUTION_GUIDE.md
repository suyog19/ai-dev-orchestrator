# PHASE 8 EXECUTION GUIDE — Reviewer Agent

## 1. Objective

Extract an independent **Reviewer Agent** from the existing generic workflow.

Today, review-like judgment is mixed into the implementation workflow, PR body, tests, and merge policy. Phase 8 separates that into a dedicated agent.

Goal:

```text
Developer flow creates PR
→ Reviewer Agent independently reviews PR
→ Reviewer Agent produces structured verdict
→ Merge is allowed only if review passes
```

## 2. Key principle

Do **not** build a full multi-agent framework.

Build one independent agent:

```text
Reviewer Agent
```

It must have:

* own prompt
* own input contract
* own output schema
* own DB state
* own GitHub review/status behavior
* own memory usage
* own blocking decision

## 3. What to preserve from current system

Do not break existing Phase 7 flow:

```text
Epic → Story → implementation → tests → PR → merge → feedback/memory
```

Current generic review intelligence must be transferred into Reviewer Agent:

* PR body validation checklist
* review checklist
* test result awareness
* story intent awareness
* changed files awareness
* memory-enriched execution guidance
* merge safety checks

After Phase 8, the workflow should not rely only on PR body text and test pass status for review judgment.

## 4. New target flow

```text
Story implementation completes
→ tests pass or NOT_RUN/FAILED state known
→ commit + push
→ PR created
→ Reviewer Agent runs
→ structured review stored
→ GitHub PR comment/review posted
→ review status recorded
→ merge policy checks review verdict
→ merge or skip/block
```

## 5. Reviewer Agent responsibilities

Reviewer Agent checks:

### Story alignment

* Does the change address the Jira Story?
* Are acceptance criteria covered?
* Is scope too broad or too narrow?

### Code quality

* Is the implementation clean?
* Any obvious bugs?
* Any unsafe assumptions?
* Any unnecessary refactor?

### Test awareness

* Were tests run?
* Did tests pass?
* Are tests relevant to the change?
* Did the PR avoid modifying tests improperly?

### Diff risk

* Number of changed files
* Type of files changed
* High-risk areas touched
* Potential side effects

### Merge recommendation

Output one of:

```text
APPROVED_BY_AI
NEEDS_CHANGES
BLOCKED
```

## 6. Explicit non-goals

Do NOT build:

* Test Quality Agent yet
* Architect Agent yet
* Release Agent yet
* multi-agent orchestration framework
* inline PR comments per line initially
* automatic human approval
* autonomous merge override

## 7. Data model changes

### New table: `agent_reviews`

Suggested schema:

```sql
id
run_id
pr_number
pr_url
repo_slug
story_key
agent_name
review_status
risk_level
summary
findings_json
recommendations_json
blocking_reasons_json
model_used
memory_snapshot_ids_json
created_at
updated_at
```

Allowed values:

```text
agent_name = reviewer_agent
review_status = APPROVED_BY_AI | NEEDS_CHANGES | BLOCKED | ERROR
risk_level = LOW | MEDIUM | HIGH
```

### Extend `workflow_runs`

Add:

```text
review_status
review_required
review_completed_at
review_summary
```

## 8. Reviewer Agent input contract

Reviewer Agent should receive:

```json
{
  "story": {
    "key": "KAN-24",
    "summary": "...",
    "description": "...",
    "acceptance_criteria": [...]
  },
  "repo": {
    "repo_slug": "suyog19/sandbox-fastapi-app",
    "base_branch": "main",
    "working_branch": "ai/KAN-24/78"
  },
  "pr": {
    "number": 33,
    "url": "...",
    "title": "...",
    "body": "..."
  },
  "implementation": {
    "files_changed": [...],
    "diff": "...",
    "commit_message": "...",
    "tests": {
      "status": "PASSED",
      "command": "pytest -q",
      "output_excerpt": "..."
    },
    "retry_count": 0,
    "files_changed_count": 2
  },
  "memory": {
    "execution_guidance": "...",
    "manual_notes": "..."
  }
}
```

## 9. Reviewer Agent output contract

Claude must return strict structured output:

```json
{
  "review_status": "APPROVED_BY_AI",
  "risk_level": "LOW",
  "summary": "The PR implements the requested item count change and tests pass.",
  "findings": [
    {
      "severity": "INFO",
      "category": "story_alignment",
      "message": "The implementation matches the Story summary."
    }
  ],
  "blocking_reasons": [],
  "recommendations": [
    "Consider adding an edge-case test for empty results in a future story."
  ]
}
```

Allowed statuses:

```text
APPROVED_BY_AI
NEEDS_CHANGES
BLOCKED
```

Rules:

* `BLOCKED` if tests fail
* `BLOCKED` if diff clearly contradicts story
* `NEEDS_CHANGES` if implementation is plausible but incomplete/risky
* `APPROVED_BY_AI` only when story alignment, code risk, and test state are acceptable

## 10. Merge policy update

Current merge gate likely checks:

```text
auto_merge_enabled
tests passed
changes applied
file count <= threshold
```

Update it to include:

```text
review_status == APPROVED_BY_AI
```

New gate:

```python
auto_merge_ok = (
    mapping.auto_merge_enabled
    and test_status == "PASSED"
    and review_status == "APPROVED_BY_AI"
    and applied_count <= MAX_FILES_FOR_AUTOMERGE
)
```

If review is `NEEDS_CHANGES` or `BLOCKED`:

* do not merge
* leave PR open
* send Telegram notification
* record feedback event

## 11. GitHub behavior

Initial implementation should post a top-level PR comment or review summary.

Recommended first version:

* create top-level PR comment with Reviewer Agent verdict

Later enhancement:

* submit formal GitHub PR review
* create required status check

For Phase 8 initial target:

* PR comment is enough
* DB status is authoritative for merge policy

PR comment format:

```md
## Reviewer Agent Verdict: APPROVED_BY_AI

Risk: LOW

### Summary
...

### Findings
- [INFO] Story alignment: ...

### Blocking Reasons
None

### Recommendations
- ...
```

## 12. Feedback and memory integration

Phase 7 feedback system should be extended.

Add feedback events:

```text
review_status
review_risk_level
review_blocked
review_needs_changes
review_approved
```

Memory snapshots may later learn:

* common review failures
* risky file patterns
* stories often marked incomplete

But in Phase 8:

* capture review signals
* do not over-optimize memory yet

## 13. Telegram events

Add events:

```text
review_started
review_completed
review_blocked
review_needs_changes
review_error
merge_blocked_by_review
```

Example:

```text
[DEV] review_completed
Story: KAN-24
PR: #33
Verdict: APPROVED_BY_AI
Risk: LOW
```

## 14. Iteration plan

## Iteration 0 — Reviewer Agent baseline

### Goal

Prepare schema and config.

### Tasks

* add `agent_reviews` table
* extend `workflow_runs` with review fields
* add constants:

  * `REVIEWER_AGENT`
  * `APPROVED_BY_AI`
  * `NEEDS_CHANGES`
  * `BLOCKED`
* add config:

  * `review_required=true`
  * `review_blocks_merge=true`

### Acceptance criteria

* DB schema migrates cleanly
* no behavior change yet
* existing story pipeline still passes

Then STOP.

---

## Iteration 1 — Reviewer Agent prompt and schema

### Goal

Create Reviewer Agent client function without wiring into workflow.

### Tasks

Add function:

```python
review_pr(
    story_context,
    pr_context,
    diff,
    test_result,
    memory_context
) -> dict
```

Claude must return structured output only.

### Acceptance criteria

* function can be tested with static sample input
* malformed output fails clearly
* no GitHub/DB writes yet

Then STOP.

---

## Iteration 2 — Collect review input package

### Goal

Build complete review context after PR creation.

### Tasks

Collect:

* Story key and summary
* acceptance criteria if available
* repo slug
* PR number and URL
* PR body
* changed files
* unified diff
* test status/output
* retry count
* memory guidance used

### Acceptance criteria

* review input package can be logged/debugged
* no Reviewer Agent call yet
* no sensitive secrets included

Then STOP.

---

## Iteration 3 — Run Reviewer Agent after PR creation

### Goal

Call Reviewer Agent after PR is created.

### Tasks

* call `review_pr(...)`
* store result in `agent_reviews`
* update `workflow_runs.review_status`
* send Telegram notification

### Acceptance criteria

* successful PR gets a review row
* status is visible in DB/API
* Telegram shows verdict
* merge behavior not changed yet

Then STOP.

---

## Iteration 4 — Post review to GitHub PR

### Goal

Make review visible where code review happens.

### Tasks

* add GitHub API helper to post PR comment
* format review summary clearly
* include verdict, risk, findings, blockers, recommendations

### Acceptance criteria

* PR contains Reviewer Agent comment
* comment matches DB review result
* failures to comment do not corrupt workflow state

Then STOP.

---

## Iteration 5 — Block merge based on review status

### Goal

Make Reviewer Agent a real gate.

### Tasks

Update auto-merge policy:

* require `review_status == APPROVED_BY_AI`
* skip merge for `NEEDS_CHANGES`
* skip/block merge for `BLOCKED`
* store `merge_status=SKIPPED` or `BLOCKED_BY_REVIEW`

### Acceptance criteria

* approved review allows existing merge flow
* needs-changes blocks merge
* blocked review blocks merge
* Telegram clearly explains reason

Then STOP.

---

## Iteration 6 — Negative-path review validation

### Goal

Prove Reviewer Agent catches unsafe cases.

### Test scenarios

Create or simulate PRs where:

1. tests failed
2. story intent not addressed
3. unrelated file changed
4. too many risky changes
5. tests not run

### Acceptance criteria

* tests failed → `BLOCKED`
* story mismatch → `BLOCKED` or `NEEDS_CHANGES`
* unrelated change → `NEEDS_CHANGES`
* tests not run → not auto-merged
* all results stored and visible

Then STOP.

---

## Iteration 7 — Feedback event integration

### Goal

Feed review outcomes into Phase 7 memory system.

### Tasks

Record feedback events:

* review_status
* risk_level
* blocking reason count
* recommendation count

Update memory snapshot generation minimally to include review outcomes.

### Acceptance criteria

* review results appear in feedback events
* repo memory can mention recurring review problems
* prompt enrichment still bounded

Then STOP.

---

## Iteration 8 — Review inspection API

### Goal

Make reviews inspectable.

### Add endpoints:

```text
GET /debug/agent-reviews?limit=N
GET /debug/agent-reviews/{id}
GET /debug/workflow-runs/{run_id}/reviews
```

### Acceptance criteria

* reviewer output visible without SSH
* linked to workflow run and PR

Then STOP.

---

## Iteration 9 — End-to-end Phase 8 validation

### Required scenarios

#### Scenario A — Approved PR

* tests pass
* reviewer approves
* auto-merge allowed

#### Scenario B — Needs changes

* tests pass
* reviewer flags incompleteness
* merge skipped

#### Scenario C — Blocked

* tests fail or story mismatch
* reviewer blocks
* merge blocked

#### Scenario D — Review error

* reviewer call fails
* merge blocked safely
* workflow state honest

### Acceptance criteria

* Reviewer Agent is independent
* merge gate respects review verdict
* GitHub PR shows review comment
* DB/API/Telegram agree
* existing Phase 5/6 behavior remains intact

Then STOP.

## 15. Important implementation instruction

Current generic review capability should be **migrated**, not duplicated.

Move these concerns out of generic PR generation / merge assumptions and into Reviewer Agent judgment:

* story alignment
* review checklist interpretation
* risk judgment
* test adequacy awareness
* merge confidence

PR body can still contain facts:

* diff
* tests
* files changed
* validation checklist

But the final judgment should come from Reviewer Agent.

## 16. Final instruction to Claude

Build Phase 8 as an **agent extraction**, not a feature bolt-on.

The question is:

> Can an independent Reviewer Agent look at the PR and decide whether it is safe to proceed?

Do not optimize for long reviews.
Optimize for:

* clear verdict
* structured findings
* merge safety
* auditability
* independence from Developer Agent

This is the first real step toward a proper multi-agent architecture.
