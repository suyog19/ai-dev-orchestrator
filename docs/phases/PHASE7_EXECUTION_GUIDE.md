
---

# PHASE 7 EXECUTION GUIDE — AI DEV ORCHESTRATOR

## 1. Which Gap Phase 7 Should Address

Phase 5 closed the execution loop successfully:

* code changes can be generated
* multi-file edits are supported
* tests run automatically
* one bounded fix loop exists
* safe auto-merge works under strict conditions
* end-to-end outcomes are observable and trustworthy

Phase 6 then added the planning layer for this project’s chosen hierarchy:

* Epic → Story decomposition is working
* human approval is mandatory
* approved Stories are created in Jira
* created Stories can flow into the existing execution pipeline
* Feature level is intentionally excluded for this project and should remain excluded unless the Jira model changes later

That means the biggest remaining gap against the original vision is now:

> **Can the system learn from what it planned and what it executed?**

Therefore, Phase 7 should address:

## Memory / Feedback / Quality Improvement Layer

Specifically:

* capture planning outcomes
* capture execution outcomes
* connect outcomes back to the originating Epic and planning run
* score what worked and what failed
* expose that feedback to future planning and execution prompts
* build a small, explicit memory layer rather than vague “AI memory”

This is the right next step because you now have:

1. Planning
2. Execution
3. Validation
4. Approval

What you do not yet have is:
5. Learning from outcomes

Without that, the system remains capable but forgetful. 

---

## 2. Why This Gap Is the Right One for Phase 7

Phases 1–6 built a very strong foundation:

* workflow orchestration
* repo mapping
* code generation
* test/fix loop
* auto-merge under strict conditions
* Epic → Story planning with approval
* traceability into Jira and execution

So now the most valuable next question is:

> When the system creates work and executes it, can it use the results to improve future planning and execution?

Right now:

* a good Story and a bad Story are both just historical rows
* a successful breakdown and a poor breakdown are not converted into reusable signal
* repeated failures do not shape future decomposition
* repeated approvals do not create stronger planning heuristics

That is the missing layer.

### Why not multi-agent first?

Because multi-agent without feedback usually becomes “more moving parts, same mistakes.”

### Why not external knowledge ingestion first?

Because internal learning signal from your own system is more valuable and lower risk at this point.

### Why not Feature-level planning?

Because you explicitly locked the project to Epic → Story in Phase 6. Phase 7 should respect that constraint, not reopen it. 

So Phase 7 should build:

> **A compact feedback and memory layer that helps the orchestrator plan and execute better over time.**

---

## 3. What Phase 7 Is Trying to Achieve (Simple Language)

Right now the system can plan and execute, but every run is mostly treated in isolation.

After Phase 7, it should be able to do this:

```text
Epic is decomposed into Stories
→ Stories are approved and created
→ Stories run through execution pipeline
→ outcomes are recorded:
   - merged cleanly
   - needed retry
   - failed tests
   - rejected planning output
   - regenerated planning output
→ those outcomes are summarized into reusable feedback
→ future planning and execution prompts can use that feedback
```

Examples of useful learning:

* Stories with vague acceptance criteria often fail or need regeneration
* Stories affecting storage and models often require multi-file changes
* Certain kinds of breakdowns are consistently approved quickly
* Certain story shapes often fail tests and need smaller scope

The goal is not magical memory.
The goal is **explicit, inspectable feedback**.

---

## 4. Phase 7 Scope

### In Scope

1. Feedback capture for planning runs
2. Feedback capture for execution runs
3. Parent-child outcome aggregation
4. Small memory store / quality signals
5. Prompt enrichment from past outcomes
6. Planning quality scoring
7. Execution quality scoring
8. Inspection APIs for memory/feedback
9. Controlled use of feedback in future runs

### Explicitly Out of Scope

Do NOT build these in Phase 7:

* vector database or RAG over arbitrary docs
* broad external knowledge ingestion
* autonomous reprioritization
* multi-agent orchestration
* self-modifying prompts without explicit rules
* black-box reinforcement learning
* replacing human approval with confidence scores

---

## 5. Design Principles for Phase 7

### 5.1 Explicit memory only

Store structured signals, not vague blobs.

Good:

* retry_count
* test failure categories
* planning approval latency
* rejected planning reasons
* common file clusters changed together

Bad:

* dumping long raw chat transcripts and calling it memory

### 5.2 Feedback must be inspectable

If feedback affects future prompts, you must be able to see:

* what feedback was used
* where it came from
* why it was relevant

### 5.3 Keep feedback local to this system first

Start with learning from:

* Jira planning runs
* execution runs
* PR/test outcomes
* approval/rejection behavior

Do not jump to external corpora.

### 5.4 Use feedback as guidance, not authority

Phase 7 should influence planning and execution, not override them.

### 5.5 Start with small bounded signals

Do not attempt a full AI memory engine.
Build a small, useful one.

---

## 6. New Capabilities to Add

### 6.1 Planning Feedback Capture

For each planning run, record:

* parent Epic key
* number of Stories proposed
* number approved
* number rejected
* whether regeneration was requested
* time to approval
* time to rejection
* whether generated Stories later succeeded or failed in execution

### 6.2 Execution Feedback Capture

For each execution run, record:

* story key
* final status
* test status
* retry count
* files changed count
* whether merged
* failure category if failed
* whether fix loop succeeded

### 6.3 Aggregated Quality Signals

At Epic / run / repo level, derive signals like:

* approval rate
* regeneration rate
* execution success rate
* average retry count
* average files changed
* failure categories by repo or story pattern

### 6.4 Prompt Feedback Enrichment

Before planning a new Epic or executing a Story, include a small relevant feedback block, for example:

* Previous approved Stories in this repo were smaller and more focused
* Stories touching storage often require model + tests together
* Generated Stories with weak acceptance criteria were frequently regenerated

### 6.5 Memory / Feedback Inspection APIs

Allow you to inspect:

* feedback records
* parent Epic outcome rollups
* repo-level quality summaries
* what memory was injected into prompts

---

## 7. Mandatory Prerequisites Before Writing Phase 7 Code

### 7.1 Confirm Epic → Story Only Is a Locked Constraint

This project intentionally excludes Feature level.

Before coding Phase 7, document this clearly in:

* `CLAUDE.md`
* config/constants
* planning prompts
* any inspection API descriptions

The system should treat:

* Epic → Story
  as the only planning hierarchy for this project.

### Acceptance criteria

* no code path expects Feature-level planning
* no prompt references Feature unless explicitly disabled/not used

### 7.2 Confirm Phase 6 Baseline Is Stable in Both Environments

Before adding feedback logic:

* dev and prod should both be on the Phase 6 baseline
* Telegram webhook + Jira creation + planning inspection APIs should be working
* at least one Epic should have gone through planning successfully

### Acceptance criteria

* one real planning run exists with child Stories created
* one or more generated Stories exist in execution history

### 7.3 Decide Initial Feedback Granularity

Before coding, choose the first memory granularity.

### Recommended initial granularity

Use three levels:

1. Run-level
2. Epic-level
3. Repo-level

Do NOT start with organization-wide global memory.

### Acceptance criteria

* chosen granularity is documented
* Phase 7 schemas match that choice

---

## 8. Data Model Changes for Phase 7

### 8.1 New table: `feedback_events`

This captures atomic learning signals.

Suggested fields:

* `id`
* `source_type` (`planning_run`, `execution_run`)
* `source_run_id`
* `epic_key` (nullable)
* `story_key` (nullable)
* `repo_slug` (nullable)
* `feedback_type`
* `feedback_value`
* `details_json`
* `created_at`

Examples:

* `feedback_type=planning_regenerated`, `feedback_value=true`
* `feedback_type=execution_retry_count`, `feedback_value=1`
* `feedback_type=test_failure_category`, `feedback_value=import_error`

### 8.2 New table: `memory_snapshots`

This stores summarized, reusable guidance.

Suggested fields:

* `id`
* `scope_type` (`epic`, `repo`, `global`)
* `scope_key`
* `memory_kind` (`planning_guidance`, `execution_guidance`)
* `summary`
* `evidence_json`
* `created_at`
* `updated_at`

Examples:

* repo-level planning guidance
* repo-level execution guidance
* epic-level postmortem summary

### 8.3 Optional table: `prompt_memory_usage`

This makes feedback injection auditable.

Suggested fields:

* `id`
* `run_id`
* `workflow_type`
* `memory_snapshot_ids_json`
* `memory_text_used`
* `created_at`

If you want to stay lighter, you can store this directly on `workflow_runs` at first.

---

## 9. Architectural Changes Required in Phase 7

### 9.1 Add Feedback Capture Hooks

Add explicit hooks at the end of:

#### Planning runs

Capture:

* approved or rejected
* regeneration requested
* number of Stories proposed
* number created
* approval latency

#### Execution runs

Capture:

* completed / failed
* test status
* retry count
* merged / skipped / failed merge
* files changed count
* failure category

### Acceptance criteria

* each planning/execution run produces structured feedback events
* feedback capture is deterministic and logged

### 9.2 Add Outcome Aggregation Layer

Build a small summarizer that can derive higher-level signals from raw feedback events.

#### Example aggregations

##### Epic-level

* total generated Stories
* total executed Stories
* execution success rate
* retry-heavy Stories
* common failure patterns

##### Repo-level

* average retry count
* approval speed
* frequent failure categories
* common file combinations changed together

### Acceptance criteria

* aggregated summaries are reproducible from stored events
* summaries can be refreshed/recomputed safely

### 9.3 Add Failure Categorization

Right now failure is mostly raw status/error detail.
Phase 7 should classify failures into useful buckets.

#### Recommended initial categories

* `test_failure`
* `syntax_failure`
* `apply_validation_failure`
* `jira_creation_failure`
* `merge_failure`
* `duplicate_blocked`
* `approval_rejected`
* `approval_regenerated`
* `worker_interrupted`
* `unknown`

### Acceptance criteria

* failed runs receive a category
* categories are persisted and can be counted/reportable

### 9.4 Add Prompt Enrichment from Feedback

This is the most important behavior change.

Before Claude plans a new Epic:

* retrieve relevant repo-level planning guidance
* optionally retrieve recent Epic-level outcomes

Before Claude executes a Story:

* retrieve relevant repo-level execution guidance
* optionally retrieve guidance based on previous Story patterns

#### Important constraint

Injected memory must be:

* short
* relevant
* explicit
* inspectable

#### Recommended initial limit

* max 5 memory bullets
* max ~800–1200 chars of memory text

### Acceptance criteria

* future prompts can include feedback context
* memory used is visible in logs / DB
* prompt size does not explode

### 9.5 Add Manual Feedback Override / Notes

Sometimes you will know something the system cannot infer.

Add a way to store manual notes like:

* Stories in this repo should be smaller
* Avoid changing tests in planning-generated work
* Storage-layer changes usually need concurrency review

Recommended approach:

* simple debug/admin endpoint to add memory snapshot manually

### Acceptance criteria

* user can inject one manual guidance note
* future runs can use it if relevant

---

## 10. Phase 7 Iteration Plan

Follow this order strictly.

---

## Iteration 0 — Memory Baseline

### Goal

Prepare schemas and scope for feedback/memory.

### Tasks

* document Epic → Story only constraint clearly
* add `feedback_events`
* add `memory_snapshots`
* optionally add `prompt_memory_usage`
* define initial failure categories
* define run / epic / repo scopes

### Acceptance criteria

* schema is ready
* memory scope is explicit
* failure categories are documented

### Verify

* inspect DB migrations
* inspect code constants/config
* confirm no Feature-level assumptions remain

Then STOP.

---

## Iteration 1 — Planning Feedback Capture

### Goal

Capture structured signals from planning runs.

### Tasks

For every planning run, record:

* approval status
* whether regenerated
* stories proposed count
* stories created count
* approval latency
* rejection/regeneration reason if known

### Acceptance criteria

* each planning run emits feedback events
* approval / rejection is queryable without inspecting raw workflow rows

### Verify

Run:

* one approved planning run
* one rejected planning run
* one regenerated planning run

Then inspect `feedback_events`.

Then STOP.

---

## Iteration 2 — Execution Feedback Capture

### Goal

Capture structured signals from story execution runs.

### Tasks

For every execution run, record:

* final status
* test status
* retry count
* merge status
* files changed count
* failure category if any

### Acceptance criteria

* every execution run emits feedback events
* success and failure are both captured consistently

### Verify

Use:

* one successful story run
* one retry-success run
* one failed run

Then inspect stored feedback.

Then STOP.

---

## Iteration 3 — Failure Categorization

### Goal

Convert raw failures into useful categories.

### Tasks

* map existing error patterns into failure categories
* classify both planning and execution failures
* store category on `feedback_events` and/or `workflow_runs`

### Recommended initial approach

Simple rule-based categorization using:

* run status
* test status
* error message text
* current_step

Do NOT use AI classification yet.

### Acceptance criteria

* failed runs are categorized deterministically
* category counts can be reported

### Verify

Replay/inspect a set of historical failed runs and confirm categories look sensible.

Then STOP.

---

## Iteration 4 — Repo-Level Memory Snapshot Generation

### Goal

Summarize recurring lessons at repo level.

### Tasks

Build a small summarizer that creates repo-level memory such as:

* Execution failures often involve storage-related changes
* Stories with smaller scope are more likely to pass without retry
* Planning runs with 3–5 Stories are approved more often than larger sets

Use structured evidence from `feedback_events`, not raw intuition.

### Acceptance criteria

* at least one repo-level planning guidance snapshot exists
* at least one repo-level execution guidance snapshot exists
* each snapshot references evidence

### Verify

Generate snapshots for the sandbox repo and inspect:

* summary text
* evidence JSON
* updated timestamps

Then STOP.

---

## Iteration 5 — Epic-Level Outcome Rollup

### Goal

Summarize how a planned Epic actually performed after execution.

### Tasks

For each Epic with created Stories:

* count Stories created
* count Stories executed
* count successes / failures
* compute retry-heavy Stories
* derive short Epic outcome summary

Example:

* “Epic KAN-20 produced 5 Stories; 4 completed, 1 failed, 2 required retry.”

### Acceptance criteria

* one Epic outcome rollup can be generated from existing data
* rollup is inspectable through API or DB
* rollup can feed future planning later

### Verify

Use a real Epic from Phase 6 and confirm the rollup matches actual child story outcomes.

Then STOP.

---

## Iteration 6 — Prompt Enrichment for Planning

### Goal

Use feedback to improve future Epic → Story decomposition.

### Tasks

Before calling Claude for planning:

* retrieve relevant repo-level planning guidance
* optionally retrieve recent Epic outcome guidance
* inject a short “prior lessons” block into the planning prompt

### Example memory block

* Previous Epics in this repo were approved faster when broken into 3–5 Stories
* Stories with explicit acceptance criteria were less likely to be regenerated
* Storage-related work often needs smaller Stories

### Acceptance criteria

* planning prompts include memory guidance when available
* guidance used is stored/logged
* prompt remains bounded

### Verify

Run one new Epic planning flow and inspect:

* prompt memory used
* resulting breakdown quality
* whether memory usage is auditable

Then STOP.

---

## Iteration 7 — Prompt Enrichment for Execution

### Goal

Use feedback to improve Story execution.

### Tasks

Before calling Claude for execution:

* retrieve relevant repo-level execution guidance
* optionally include patterns like:

  * storage changes often require tests
  * multi-file changes are common for certain story types
  * previous retries often involved model + storage together

### Acceptance criteria

* execution prompt includes bounded feedback guidance
* memory usage is persisted/logged
* execution still remains deterministic and reviewable

### Verify

Run one new Story execution and inspect:

* memory block used
* changed files
* test/retry behavior
* whether memory appears helpful rather than noisy

Then STOP.

---

## Iteration 8 — Manual Guidance / Override Notes

### Goal

Allow human-injected lessons to shape future runs.

### Tasks

Add a simple admin/debug endpoint to create manual memory snapshots, for example:

* `POST /debug/memory`
* `GET /debug/memory?scope_type=repo&scope_key=...`

Manual notes should be tagged clearly as human-authored.

### Acceptance criteria

* one manual repo-level note can be added
* future planning or execution can include it if relevant
* manual vs derived memory is distinguishable

### Verify

Add a note like:

* “Stories in this repo should stay small and avoid test edits unless explicitly requested”
  Then confirm it can appear in prompt enrichment.

Then STOP.

---

## Iteration 9 — Memory / Feedback Inspection API

### Goal

Make memory and feedback self-service.

### Tasks

Add endpoints such as:

* `GET /debug/feedback-events?limit=N`
* `GET /debug/memory-snapshots?scope_type=repo&scope_key=...`
* `GET /debug/epic-outcomes/{epic_key}`

Optional:

* `POST /debug/memory/recompute?scope_type=repo&scope_key=...`

### Acceptance criteria

* feedback and memory are inspectable without SSH
* Epic / repo summaries are visible via API
* prompt memory usage can be audited

### Verify

Compare:

* one repo memory summary
* one Epic outcome summary
* raw feedback events that support them

Then STOP.

---

## Iteration 10 — End-to-End Phase 7 Validation

### Goal

Validate that the system now learns in a bounded, explicit way.

### Required scenarios

#### Scenario A — Approved Epic produces feedback

* Epic planned and approved
* Stories created
* planning feedback recorded

#### Scenario B — Execution outcomes generate memory

* Stories run through execution pipeline
* success / retry / failure signals captured

#### Scenario C — New planning run uses prior lessons

* new Epic in same repo uses planning memory
* memory usage is visible

#### Scenario D — New execution run uses prior lessons

* new Story in same repo uses execution memory
* memory usage is visible

#### Scenario E — Manual note influences prompt

* manual repo note added
* future run includes it if relevant

### Acceptance criteria

* feedback capture is reliable
* memory summaries are understandable
* future prompts use memory in bounded ways
* no hidden black-box behavior is introduced

Then STOP and review before Phase 8.

---

## 11. Prompt Design Guidance for Claude in Phase 7

### Planning prompt enrichment should aim for:

* smaller, sharper Story breakdowns
* explicit acceptance criteria
* fewer regenerations
* repo-sensitive decomposition

### Execution prompt enrichment should aim for:

* better file targeting
* fewer retries
* more realistic multi-file edits
* better test awareness

### Claude must:

* treat memory as guidance, not hard law
* avoid copying memory blindly into outputs
* keep memory references concise
* surface uncertainty when guidance conflicts

### Claude must not:

* invent memory
* use stale or unrelated feedback
* overfit to one prior run
* allow memory to override the actual current requirement

---

## 12. Recommended Config Additions for Phase 7

Add to config or DB as appropriate:

* `memory_enabled`
* `max_memory_bullets`
* `max_memory_chars`
* `allow_manual_memory_notes`
* `memory_scopes_enabled` (`run`, `epic`, `repo`)
* `failure_categories_enabled`
* `memory_refresh_mode` (`on_write`, `scheduled`, `manual`)

Recommended initial values:

* `memory_enabled = true`
* `max_memory_bullets = 5`
* `max_memory_chars = 1000`
* `memory_refresh_mode = on_write`

---

## 13. Telegram / Notification Enhancements for Phase 7

Add events such as:

* `[DEV] planning_feedback_recorded`
* `[DEV] execution_feedback_recorded`
* `[DEV] memory_snapshot_updated`
* `[DEV] epic_outcome_ready`
* `[DEV] manual_memory_added`

Keep these sparse. Do not spam Telegram with every feedback row.

Telegram should notify on:

* memory summary creation
* major Epic outcome summary
* manual memory note added
* recompute failures

---

## 14. Verification Commands Template

For each iteration, Claude should provide exact verify steps.

Examples:

### List workflow runs

```bash
curl -s "https://dev.orchestrator.suyogjoshi.com/debug/workflow-runs?limit=5"
```

### List planning runs

```bash
curl -s "https://dev.orchestrator.suyogjoshi.com/debug/planning-runs?limit=5"
```

### List feedback events

```bash
curl -s "https://dev.orchestrator.suyogjoshi.com/debug/feedback-events?limit=20"
```

### List memory snapshots

```bash
curl -s "https://dev.orchestrator.suyogjoshi.com/debug/memory-snapshots?scope_type=repo&scope_key=suyog19/sandbox-fastapi-app"
```

### Inspect Epic outcome

```bash
curl -s "https://dev.orchestrator.suyogjoshi.com/debug/epic-outcomes/KAN-20"
```

### View worker logs

```bash
docker compose logs worker --tail=200
```

### Recreate stack after env change

```bash
docker compose up -d --force-recreate
```

---

## 15. Definition of Done for Phase 7

Phase 7 is complete when all of these are true:

* Epic → Story only constraint is clearly encoded
* planning runs emit structured feedback signals
* execution runs emit structured feedback signals
* failures are categorized deterministically
* repo-level memory snapshots can be generated
* Epic-level outcome rollups can be generated
* planning prompts can use bounded prior lessons
* execution prompts can use bounded prior lessons
* manual memory notes can be added and inspected
* feedback and memory are available via HTTP APIs
* memory usage in prompts is auditable
* the system is still explicit and reviewable, not black-box

---

## 16. Final Instruction to Claude

Build Phase 7 like a memory and feedback system, not like a mystical “AI brain.”

Do not optimize for:

* huge memory stores
* vague summaries
* hidden adaptive behavior

Optimize for:

* structured signals
* explicit summaries
* bounded prompt enrichment
* auditable learning
* better future planning and execution quality

The key question for Phase 7 is:

> **Can the system use what it has already learned from planning and execution to make the next run better?**

That is the standard now.

---
