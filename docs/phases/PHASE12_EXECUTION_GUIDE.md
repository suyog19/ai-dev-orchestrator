# PHASE 12 EXECUTION GUIDE — Clarification Loop

## 1. Objective

Add a **human clarification loop** into the orchestrator.

Today the system can plan, implement, review, test, merge, learn, and pause securely. Phase 11 already added admin/security controls, pause/resume, Telegram enforcement, GitHub write guard, and audit trail. 

Phase 12 should add the missing collaboration ability:

```text
Workflow reaches ambiguity
→ system asks user a specific question on Telegram
→ workflow pauses
→ user answers
→ answer is stored
→ workflow resumes from the right point
```

This directly addresses the original requirement:

> Claude should be able to ask me questions on Telegram and proceed based on my response.

---

# 2. Core Scope

Implement clarification support for:

1. Epic planning
2. Story implementation
3. Reviewer / Test Quality / Architecture uncertainty
4. Telegram answer capture
5. Workflow resume
6. Timeouts and cancellation
7. Audit trail

---

# 3. New Workflow State

Add:

```text
WAITING_FOR_USER_INPUT
```

Allowed transition:

```text
RUNNING
→ WAITING_FOR_USER_INPUT
→ RUNNING
→ COMPLETED / FAILED
```

Also allow:

```text
WAITING_FOR_USER_INPUT
→ CANCELLED
→ FAILED
```

---

# 4. Clarification Data Model

Add table:

```sql
clarification_requests
```

Suggested fields:

```sql
id SERIAL PRIMARY KEY,
run_id INTEGER NOT NULL,
workflow_type VARCHAR(100),
issue_key VARCHAR(100),
repo_slug VARCHAR(200),
question TEXT NOT NULL,
context_summary TEXT,
options_json TEXT,
status VARCHAR(40), -- PENDING / ANSWERED / CANCELLED / EXPIRED
answer_text TEXT,
answered_at TIMESTAMP NULL,
telegram_message_id VARCHAR(100) NULL,
created_at TIMESTAMP DEFAULT NOW(),
expires_at TIMESTAMP NULL
```

Add to `workflow_runs`:

```text
waiting_for_clarification BOOLEAN DEFAULT FALSE
active_clarification_id INTEGER NULL
```

---

# 5. Clarification Statuses

```text
PENDING
ANSWERED
CANCELLED
EXPIRED
```

---

# 6. Telegram Commands

Support:

```text
ANSWER <clarification_id> <answer>
CANCEL <clarification_id>
CLARIFY <clarification_id>
```

Examples:

```text
ANSWER 42 Use JWT-based auth for this story
CANCEL 42
CLARIFY 42
```

`CLARIFY <id>` should resend the question and context.

---

# 7. When to Ask Clarifying Questions

Do not ask for every minor uncertainty.

Ask only when:

## Epic planning

* Epic description is too vague
* acceptance criteria are missing
* target repo mapping is unclear
* generated Stories would require major assumptions

## Story implementation

* implementation approach has two valid paths
* acceptance criteria conflict with codebase reality
* missing repo mapping or ambiguous target area
* dangerous change detected before coding

## Review agents

* Reviewer/Test Quality/Architecture Agent returns uncertainty that blocks safe decision
* but answer from user could resolve it

---

# 8. Claude Clarification Contract

Agents may return:

```json
{
  "needs_clarification": true,
  "question": "Should the new endpoint be public or require authentication?",
  "context_summary": "The Story asks for /status endpoint but does not specify auth behaviour.",
  "options": [
    "Make endpoint public",
    "Require existing auth",
    "Follow current project convention"
  ]
}
```

Important:

* question must be specific
* options should be short
* no vague “please clarify requirements”
* only one clarification request per workflow step initially

---

# 9. Iteration Plan

## Iteration 0 — Schema and constants

### Tasks

* Add `clarification_requests` table
* Extend `workflow_runs`
* Add constants:

  * `WAITING_FOR_USER_INPUT`
  * `PENDING`
  * `ANSWERED`
  * `CANCELLED`
  * `EXPIRED`
* Add config:

  * `CLARIFICATION_ENABLED=true`
  * `CLARIFICATION_TIMEOUT_HOURS=24`

### Acceptance criteria

* migrations idempotent
* existing workflow unaffected
* no behavior change yet

Then STOP.

---

## Iteration 1 — Clarification service layer

### Tasks

Create helpers:

```python
create_clarification_request(run_id, question, context_summary, options)
mark_clarification_answered(clarification_id, answer_text)
mark_clarification_cancelled(clarification_id)
get_active_clarification(run_id)
list_pending_clarifications()
```

### Acceptance criteria

* helper unit/static tests pass
* DB rows created and updated correctly
* no Telegram integration yet

Then STOP.

---

## Iteration 2 — Telegram question sender

### Tasks

Add:

```python
send_clarification_request(clarification_id)
```

Telegram format:

```text
[DEV] clarification_required

Run: 91
Issue: KAN-24
Question:
Should the new endpoint be public or require authentication?

Options:
1. Public
2. Require existing auth
3. Follow current convention

Reply:
ANSWER 42 <your answer>
CANCEL 42
CLARIFY 42
```

### Acceptance criteria

* message sent
* clarification row stores message id if available
* failure to send marks workflow failed or logs clearly

Then STOP.

---

## Iteration 3 — Telegram answer parser

### Tasks

Extend Telegram webhook handling:

```text
ANSWER <id> <answer_text>
CANCEL <id>
CLARIFY <id>
```

Validation:

* chat id must match configured user
* clarification id must exist
* status must be `PENDING`
* answer must not be empty
* paused mode blocks command execution

### Acceptance criteria

* valid answer updates DB
* invalid id rejected
* wrong chat rejected
* cancel works
* clarify resends question

Then STOP.

---

## Iteration 4 — Workflow pause / resume mechanics

### Tasks

Create generic function:

```python
pause_for_clarification(run_id, question, context_summary, options)
```

Behavior:

* create clarification row
* set workflow status `WAITING_FOR_USER_INPUT`
* set `waiting_for_clarification=true`
* send Telegram question
* stop current worker execution safely

Create resume function:

```python
resume_workflow_after_clarification(run_id)
```

Initial approach:

* enqueue same workflow run again
* workflow detects answered clarification
* continues from stored step

### Acceptance criteria

* workflow can enter `WAITING_FOR_USER_INPUT`
* no duplicate run created
* answer resumes existing run
* cancelled clarification stops run safely

Then STOP.

---

## Iteration 5 — Add clarification checkpoints to Epic planning

### Goal

Planning should ask when Epic is too vague.

### Tasks

Before Claude decomposition:

* inspect Epic summary/description
* if too short or missing acceptance criteria, ask clarification

Also allow Claude planning output to request clarification.

### Example question

```text
The Epic asks for "user dashboard" but does not define target users or metrics.
Which dashboard scope should I use?
1. Basic user activity dashboard
2. Admin operational dashboard
3. Learning progress dashboard
```

### Acceptance criteria

* vague Epic triggers clarification
* answer is injected into planning prompt
* approved planning continues after answer

Then STOP.

---

## Iteration 6 — Add clarification checkpoints to Story implementation

### Goal

Implementation should ask before making risky assumptions.

### Tasks

Before Developer Agent suggestion:

* detect ambiguous acceptance criteria
* detect multiple valid implementation paths
* allow Developer Agent to return clarification request

Store answer and inject into `suggest_change`.

### Acceptance criteria

* ambiguous Story pauses
* answer resumes implementation
* answer appears in Developer Agent prompt context
* no code change before clarification answer

Then STOP.

---

## Iteration 7 — Add clarification support for review agents

### Goal

Agents can request clarification instead of forcing `NEEDS_CHANGES` / `BLOCKED`.

Apply initially to:

```text
Reviewer Agent
Architecture Agent
```

Not necessary for Test Quality Agent initially.

### Behavior

If agent returns needs_clarification:

* pause run
* ask Telegram question
* resume agent review after answer
* include answer in review context

### Acceptance criteria

* reviewer ambiguity pauses
* architecture ambiguity pauses
* resumed verdict is stored normally

Then STOP.

---

## Iteration 8 — Timeout and expiry handling

### Tasks

Add periodic / startup check:

```python
expire_stale_clarifications()
```

If `expires_at < now` and status `PENDING`:

* mark `EXPIRED`
* mark workflow `FAILED` or `CANCELLED`
* send Telegram notification

Recommended initial behavior:

```text
Expired clarification → workflow FAILED with reason clarification_timeout
```

### Acceptance criteria

* stale clarification expires
* workflow does not stay waiting forever
* security/audit event recorded

Then STOP.

---

## Iteration 9 — Audit and feedback integration

### Tasks

Add feedback events:

```text
clarification_requested
clarification_answered
clarification_cancelled
clarification_expired
clarification_count
```

Add security events for:

* invalid answer command
* wrong chat answer
* cancelled clarification

Update memory snapshots lightly:

* count how often planning needs clarification
* count how often implementation needs clarification

### Acceptance criteria

* clarification events visible in feedback
* memory remains bounded
* no Telegram spam

Then STOP.

---

## Iteration 10 — Inspection APIs

Add admin-protected endpoints:

```text
GET /debug/clarifications?status=PENDING
GET /debug/clarifications/{id}
POST /debug/clarifications/{id}/answer
POST /debug/clarifications/{id}/cancel
POST /debug/clarifications/{id}/resend
```

Useful for testing without Telegram.

### Acceptance criteria

* admin key required
* pending clarifications visible
* admin answer can resume workflow

Then STOP.

---

## Iteration 11 — End-to-end validation

### Required scenarios

#### Scenario A — Epic planning clarification

```text
Vague Epic
→ question asked
→ answer given
→ Stories generated
```

#### Scenario B — Story implementation clarification

```text
Ambiguous Story
→ question asked
→ answer given
→ code generated
→ tests/reviews continue
```

#### Scenario C — Reviewer clarification

```text
PR ambiguity
→ Reviewer asks
→ answer given
→ review completes
```

#### Scenario D — Cancel clarification

```text
question asked
→ CANCEL
→ workflow stops safely
```

#### Scenario E — Timeout

```text
question asked
→ no answer
→ expires
→ workflow FAILED
```

#### Scenario F — Invalid Telegram command

```text
wrong chat / wrong id / empty answer
→ rejected
→ no DB mutation
```

### Acceptance criteria

* no workflow remains stuck forever
* answer is visible in workflow history
* resumed workflow continues from correct step
* DB/API/Telegram agree

Then STOP.

---

# 10. Important Design Notes

## Do not create a second workflow run

Resume the same run.

## Do not ask vague questions

Bad:

```text
Please clarify the story.
```

Good:

```text
Should the endpoint require authentication?
1. Public
2. Authenticated
3. Follow existing convention
```

## Do not allow multiple pending questions per run initially

One active clarification per workflow run.

## Clarification is not failure

It is a normal intermediate state.

## Human answer becomes first-class context

Store and inject it explicitly.

---

# 11. Final Instruction to Claude

Build Phase 12 as a **conversation-in-the-workflow layer**.

The goal is not to chat casually.

The goal is:

```text
When a workflow cannot safely proceed,
pause,
ask one precise question,
store the answer,
resume from the correct step.
```

Optimize for:

* precise questions
* safe pause/resume
* auditable answers
* no stuck workflows
* Telegram-first UX

Do not optimize for:

* long conversations
* multiple simultaneous questions
* free-form chatbot behavior
* generic support bot features

This phase completes a major missing human-in-the-loop capability.
