
---

# PHASE 6 EXECUTION GUIDE — AI DEV ORCHESTRATOR

## 1. Which gap Phase 6 should address

Phase 5 proved that the system can safely execute work:

* generate code
* make multi-file edits
* run tests
* do one bounded fix loop
* create PRs
* auto-merge under strict conditions
* keep state trustworthy and observable 

So the biggest remaining gap is no longer execution.

The biggest remaining gap is:

> **Can the system understand larger requirements and break them into executable work?**

That means Phase 6 should address:

## **Planning Layer / Requirement Decomposition**

Specifically:

* Epic → Feature decomposition
* Feature → Story decomposition
* structured acceptance criteria generation
* dependency / sequencing hints
* human approval before Jira child creation
* traceability from parent issue to generated children

---

## 2. Why this is the right next gap

Your original vision was closer to an AI engineering team than an AI coding bot.

That requires two layers:

* **Planning**
* **Execution**

Phases 1–5 built execution very well.

What is still missing:

* AI-assisted planning
* hierarchical requirement understanding
* decomposition of larger work into smaller delivery units

Without this, the system is:

> a very strong Story executor

Instead of:

> a requirement-to-delivery system

### Why not memory yet

Memory is useful, but memory without a structured planning loop mostly stores noise.

### Why not multi-agent yet

Planner / coder / reviewer / tester agents are attractive, but adding role complexity before planning outputs are clearly defined is premature.

### Why not knowledge ingestion yet

External knowledge becomes more valuable once the planning workflow exists.

So Phase 6 should build:

> **A human-supervised planning engine that converts higher-level requirements into structured Jira work.**

---

## 3. What Phase 6 is trying to achieve in simple language

Right now the system waits for a Story and then executes it.

After Phase 6, it should be able to do this:

```text
Epic becomes ready
→ system reads Epic
→ system proposes Features
→ user reviews and approves
→ Features are created in Jira

Feature becomes ready
→ system reads Feature
→ system proposes Stories
→ user reviews and approves
→ Stories are created in Jira

Approved Stories
→ continue into the existing execution pipeline
```

The system should still stay human-supervised.

---

## 4. Phase 6 scope

### In scope

* Epic → Feature decomposition workflow
* Feature → Story decomposition workflow
* structured decomposition schema
* Telegram approval loop
* Jira child issue creation after approval
* traceability between parent and generated children
* observability of planning runs
* idempotency / deduplication for decomposition runs
* planning quality metadata like assumptions, confidence, open questions

### Explicitly out of scope

Do not build these in Phase 6:

* autonomous creation without approval
* multi-agent planning
* sprint planning / reprioritization
* memory engine
* external knowledge ingestion
* automatic story execution right after creation unless explicitly enabled
* autonomous closure of parents

---

## 5. Design principles for Phase 6

### 5.1 Human approval is mandatory

Planning output is a proposal, not truth.

### 5.2 Structured output only

Claude must return strict JSON/tool output, not loose prose.

### 5.3 Bounded decomposition

Recommended initial limits:

* Epic → max 5 Features
* Feature → max 8 Stories

### 5.4 Idempotent planning

Repeated triggers must not duplicate Jira children.

### 5.5 Traceability matters

Every child should retain:

* parent issue key
* planning run ID
* generated-by-AI marker

---

## 6. New capabilities to add

### 6.1 Planning workflows

Add two new workflows:

* `epic_breakdown`
* `feature_breakdown`

### 6.2 Planning prompt layer

For each planning workflow, Claude should produce:

* child item title
* child item description
* acceptance criteria
* rationale
* dependency hints
* risk/open question notes
* confidence

### 6.3 Approval workflow

Planning results should be sent to Telegram for:

* approve
* reject
* regenerate

### 6.4 Jira child creation

After approval:

* create Features under Epic
* create Stories under Feature

### 6.5 Planning observability

Store:

* planning run status
* generated candidates
* approval decision
* created Jira keys
* rejection/regeneration info

---

## 7. Mandatory prerequisites before coding Phase 6

### 7.1 Confirm exact Jira trigger statuses

Do not use vague “final” language.

Define actual trigger states, for example:

* Epic trigger status: `READY FOR BREAKDOWN`
* Feature trigger status: `READY FOR STORY BREAKDOWN`

Replace with your real Jira values before coding.

### 7.2 Confirm Jira hierarchy model

Before coding, confirm:

* which issue types exist
* whether `Feature` is a real Jira issue type
* how parent-child linking should work
* which fields are mandatory when creating children

### 7.3 Add environment prefixing to planning messages

Reuse `ENV_NAME` pattern:

* `[DEV] epic_breakdown proposed`
* `[DEV] feature_breakdown awaiting approval`

### 7.4 Define approval protocol

Start with Telegram text commands:

* `APPROVE <run_id>`
* `REJECT <run_id>`
* `REGENERATE <run_id>`

Keep it simple. Avoid buttons initially.

---

## 8. Data model changes

### 8.1 Extend `workflow_runs`

Add or confirm:

* `parent_issue_key`
* `approval_status` (`PENDING`, `APPROVED`, `REJECTED`, `REGENERATE_REQUESTED`)
* `approval_requested_at`
* `approval_received_at`
* `created_jira_children_count`

### 8.2 New table: `planning_outputs`

Suggested fields:

* `id`
* `run_id`
* `parent_issue_key`
* `parent_issue_type`
* `proposed_issue_type`
* `sequence_number`
* `title`
* `description`
* `acceptance_criteria`
* `rationale`
* `dependency_notes`
* `risk_notes`
* `confidence`
* `status` (`PROPOSED`, `APPROVED`, `REJECTED`, `CREATED`)
* `created_issue_key`
* `created_at`
* `updated_at`

### 8.3 Optional table: `approval_messages`

Only if Telegram handling becomes richer. Otherwise defer.

---

## 9. Architectural changes required

### 9.1 Add planning workflow routing

Update dispatcher:

* `(Epic, <epic_trigger_status>)` → `epic_breakdown`
* `(Feature, <feature_trigger_status>)` → `feature_breakdown`

Story execution remains unchanged.

### 9.2 Add structured decomposition contract

Claude must return something like:

```json
{
  "summary": "Break this Epic into implementation-ready Features",
  "assumptions": [
    "Authentication already exists"
  ],
  "open_questions": [
    "Should reporting be separate?"
  ],
  "items": [
    {
      "issue_type": "Feature",
      "title": "Add item search capability",
      "description": "Enable filtering and searching items by name and status.",
      "acceptance_criteria": [
        "User can search by name substring",
        "Results can be filtered by status",
        "API returns count and results"
      ],
      "rationale": "This is an independently deliverable capability.",
      "dependency_notes": "Should precede reporting",
      "risk_notes": "May need optimization later",
      "confidence": "medium"
    }
  ]
}
```

Rules:

* strict JSON/tool output only
* cap item count
* descriptions must be implementation-oriented

### 9.3 Add approval gate before Jira creation

Workflow phases:

1. planning run starts
2. Claude generates decomposition
3. output stored in DB as `PROPOSED`
4. Telegram sends summary + approval request
5. run enters `WAITING_FOR_APPROVAL`
6. user replies
7. only then create Jira children

### 9.4 Add Jira creation layer

After approval:

* create child issues
* link them to parent
* persist created keys

Initial policy:

* create sequentially
* persist after each creation
* if creation fails mid-way, fail clearly
* do not auto-rollback Jira

### 9.5 Add planning idempotency / deduplication

Before creating children:

* check if parent already has planning-created children
* block duplicate creation unless regeneration is explicit

---

## 10. Phase 6 iteration plan

Follow this order strictly.

---

## Iteration 0 — Planning baseline

### Goal

Prepare the system for planning workflows.

### Tasks

* define exact trigger statuses for Epic and Feature
* confirm Jira hierarchy and required fields
* add DB schema changes
* add environment-prefixed Telegram planning messages
* document approval command format

### Acceptance criteria

* triggers are explicit
* DB schema is ready
* approval flow is documented
* no vague assumptions remain

### Verify

* inspect DB migrations
* inspect config/constants
* verify sample approval command format

Then STOP.

---

## Iteration 1 — Epic breakdown stub workflow

### Goal

Create `epic_breakdown` end-to-end without Jira creation yet.

### Tasks

* dispatcher recognizes Epic trigger
* workflow run is created
* parent Epic details are loaded
* stub outputs are stored in `planning_outputs`
* Telegram says proposal exists

### Acceptance criteria

* Epic trigger routes correctly
* planning run persists rows
* Telegram shows run ID and pending approval

### Verify

* trigger Epic webhook
* inspect `workflow_runs`
* inspect `planning_outputs`
* inspect Telegram

Then STOP.

---

## Iteration 2 — Feature breakdown stub workflow

### Goal

Mirror Iteration 1 for Features.

### Tasks

* dispatcher recognizes Feature trigger
* workflow produces stub Story proposals
* outputs are stored
* Telegram approval request sent

### Acceptance criteria

* Feature trigger routes correctly
* outputs stored
* no Jira children created yet

### Verify

* trigger Feature webhook
* inspect DB + Telegram

Then STOP.

---

## Iteration 3 — Claude-based structured decomposition

### Goal

Replace stub outputs with real Claude-generated planning output.

### Tasks

* add Epic and Feature decomposition prompts
* enforce structured JSON/tool output
* validate schema before storing
* reject malformed outputs
* cap number of generated items

### Recommended limits

* Epic → max 5 Features
* Feature → max 8 Stories

### Acceptance criteria

* Claude output is structured and stored
* malformed output fails clearly
* generated items are implementation-oriented

### Verify

Use a realistic Epic and Feature and inspect:

* summary
* items
* acceptance criteria
* rationale
* risks/open questions

Then STOP.

---

## Iteration 4 — Approval handling via Telegram

### Goal

Allow approve/reject/regenerate through Telegram.

### Tasks

* add parser for:

  * `APPROVE <run_id>`
  * `REJECT <run_id>`
  * `REGENERATE <run_id>`
* update `approval_status`
* route replies to correct pending run

### Initial constraint

One run ID and one action per reply.

### Acceptance criteria

* valid commands update DB
* invalid command is rejected clearly
* pending runs remain pending until explicit response

### Verify

* send approve
* send reject
* send malformed command
* inspect DB state changes

Then STOP.

---

## Iteration 5 — Jira child creation after approval

### Goal

Create real Jira children from approved planning output.

### Tasks

* implement Jira issue creation helper
* create Features under Epic
* create Stories under Feature
* store created Jira keys back in `planning_outputs`
* update `created_jira_children_count`

### Acceptance criteria

* no child issues before approval
* created keys are persisted
* mid-creation failure is visible and honest

### Verify

* approve one Epic run → confirm Features in Jira
* approve one Feature run → confirm Stories in Jira
* inspect DB traceability

Then STOP.

---

## Iteration 6 — Planning idempotency and regeneration

### Goal

Prevent duplicates while supporting explicit regeneration.

### Tasks

* detect existing created outputs for parent
* block duplicate automatic creation
* support `REGENERATE <run_id>` path

### Recommended initial policy

* if children already created for parent, block auto-repeat
* regeneration allowed only while run is still proposal-stage or via explicit admin/debug action

### Acceptance criteria

* repeated trigger does not duplicate Jira children
* regeneration is distinguishable from accidental rerun

### Verify

* trigger same parent twice
* confirm no duplicates
* test regeneration flow

Then STOP.

---

## Iteration 7 — Parent/child traceability and rich metadata

### Goal

Make decomposition auditable.

### Tasks

Ensure each created Jira child includes markers in description or labels:

* parent issue key
* AI-generated
* planning run ID

Improve Telegram and DB visibility to show:

* proposed item count
* created count
* approval decision
* open questions

### Acceptance criteria

* children are traceable to parent and planning run
* planning run detail is sufficient for debugging

### Verify

* inspect created Jira issues
* inspect DB planning rows
* inspect Telegram summary

Then STOP.

---

## Iteration 8 — Planning run inspection API

### Goal

Make planning operations self-service.

### Tasks

Add:

* `GET /debug/planning-runs?limit=N`
* `GET /debug/planning-runs/{run_id}`

Include:

* parent issue
* workflow type
* approval status
* proposed items
* created issue keys
* assumptions/open questions

Optional:

* `POST /debug/planning-runs/{run_id}/approve`
* `POST /debug/planning-runs/{run_id}/reject`

### Acceptance criteria

* planning runs are inspectable over HTTP
* no SSH needed for basic planning debugging

### Verify

* compare approved vs rejected run output

Then STOP.

---

## Iteration 9 — End-to-end Phase 6 validation

### Goal

Validate full planning-to-execution chain.

### Required scenarios

#### Scenario A — Epic approved

* Epic moves to trigger status
* system proposes Features
* user approves
* Features created in Jira

#### Scenario B — Feature approved

* Feature moves to trigger status
* system proposes Stories
* user approves
* Stories created in Jira

#### Scenario C — Rejected run

* system proposes items
* user rejects
* no Jira children created

#### Scenario D — Duplicate trigger blocked

* same parent triggers again
* no duplicate creation

#### Scenario E — Regeneration path

* user requests regeneration
* new proposal produced
* no silent duplicates

### Stretch scenario

Take one generated Story through the existing execution pipeline.

### Acceptance criteria

* planning workflow is trustworthy
* approval gate works
* Jira creation works
* duplicates are prevented
* outputs are traceable and reviewable

Then STOP and review before Phase 7.

---

## 11. Prompt design guidance for Claude

### Epic → Feature prompt should aim for:

* capability-level breakdown
* independent deliverable units
* minimal overlap

### Feature → Story prompt should aim for:

* implementation-sized units
* clear acceptance criteria
* testable scope

### Claude must:

* avoid vague titles like “Improve system”
* avoid mixing future ideas with immediate implementation
* surface assumptions and open questions explicitly
* prefer fewer, better items over noisy lists

### Claude must not:

* generate excessive children
* invent dependencies casually
* assume missing architecture details without calling them out

---

## 12. Recommended config additions

* `epic_breakdown_trigger_status`
* `feature_breakdown_trigger_status`
* `max_features_per_epic`
* `max_stories_per_feature`
* `planning_requires_approval`
* `allow_regeneration`
* `jira_feature_issue_type_name`
* `jira_story_issue_type_name`

---

## 13. Telegram message enhancements

Add events such as:

* `[DEV] epic_breakdown_proposed`
* `[DEV] feature_breakdown_proposed`
* `[DEV] planning_awaiting_approval`
* `[DEV] planning_approved`
* `[DEV] planning_rejected`
* `[DEV] jira_children_created`
* `[DEV] planning_duplicate_blocked`

Keep them short and explicit.

---

## 14. Verification commands template

### List workflow runs

```bash
curl -s "https://dev.orchestrator.suyogjoshi.com/debug/workflow-runs?limit=5"
```

### Inspect planning runs

```bash
curl -s "https://dev.orchestrator.suyogjoshi.com/debug/planning-runs?limit=5"
```

### Trigger Jira webhook

```bash
curl -X POST https://dev.orchestrator.suyogjoshi.com/webhooks/jira \
  -H "Content-Type: application/json" \
  -d @payload.json
```

### View worker logs

```bash
docker compose logs worker --tail=200
```

### Check mappings

```bash
curl -s https://dev.orchestrator.suyogjoshi.com/debug/mapping-health
```

### Recreate stack after env change

```bash
docker compose up -d --force-recreate
```

---

## 15. Definition of done for Phase 6

Phase 6 is complete when all of these are true:

* Epic and Feature planning trigger statuses are explicit and configured
* `epic_breakdown` and `feature_breakdown` workflows exist
* Claude produces structured decomposition output
* planning outputs are stored and inspectable
* Telegram approval loop works
* approved runs create Jira children
* rejected runs create nothing
* duplicate parent triggers do not create duplicate children
* regeneration path is explicit and safe
* created Jira children are traceable to parent and planning run
* planning runs can be inspected via HTTP APIs
* at least one generated Story can flow into the existing execution pipeline

---

## 16. Final instruction to Claude

Build Phase 6 like a planning system, not like a brainstorming assistant.

Do not optimize for:

* lots of generated items
* impressive prose
* speculative architecture

Optimize for:

* structured outputs
* traceable decisions
* approval safety
* implementation-ready decomposition

The key question for Phase 6 is:

> **Can the system convert larger requirements into clean, reviewable, executable work?**

That is the standard now.

---
