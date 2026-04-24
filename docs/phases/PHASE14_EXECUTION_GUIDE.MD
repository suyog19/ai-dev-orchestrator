
---

# PHASE 14 EXECUTION GUIDE — Admin Dashboard / Operations Console

## 1. Objective

Build a lightweight admin dashboard for the AI Dev Orchestrator.

The dashboard should allow the user to:

* see current system health
* inspect workflow runs
* inspect planning runs
* inspect pending clarifications
* inspect release decisions
* inspect agent verdicts
* inspect GitHub status publishing
* inspect memory snapshots
* inspect security events
* pause/resume automation
* trigger safe admin actions

This is an internal dashboard, not a public product UI.

---

## 2. Design Principle

Do **not** build a large frontend platform.

Build a focused operational console.

Use existing APIs wherever possible.

If an API is missing, add only what the dashboard needs.

---

## 3. Recommended Tech Stack

Use the simplest stack compatible with the current orchestrator.

Recommended:

```text
Backend: existing FastAPI app
Frontend: simple server-rendered HTML + minimal JavaScript
Auth: existing X-Orchestrator-Admin-Key
Styling: plain CSS or lightweight Tailwind CDN
```

Avoid for Phase 14:

```text
React SPA
Next.js
complex frontend build pipeline
separate frontend deployment
OAuth
multi-user auth
charts library unless absolutely needed
```

Reason: this is an internal operations console. Keep it boring and reliable.

---

## 4. Dashboard Routes

Add UI routes under:

```text
/admin/ui
```

Suggested pages:

```text
/admin/ui
/admin/ui/runs
/admin/ui/runs/{run_id}
/admin/ui/planning
/admin/ui/clarifications
/admin/ui/security
/admin/ui/memory
/admin/ui/github
/admin/ui/control
```

All `/admin/ui/*` routes must require admin authentication.

---

## 5. Authentication Approach

Reuse existing admin key.

For browser usability, support both:

### Option A — Header-based

Continue accepting:

```http
X-Orchestrator-Admin-Key
```

### Option B — Simple session cookie

Add login page:

```text
/admin/ui/login
```

User enters admin key.

If valid:

* set secure HTTP-only cookie
* redirect to dashboard

Cookie:

```text
orchestrator_admin_session
```

Initial implementation can store a signed token or simply a short-lived session marker derived from admin key hash.

Recommended for Phase 14:

```text
Implement simple admin-key login cookie.
```

Do not build user accounts.

---

# 6. Phase 14 Scope

## In scope

1. Admin UI authentication
2. System overview page
3. Workflow run list
4. Workflow run detail
5. Planning/clarification visibility
6. Agent verdict visibility
7. GitHub status visibility
8. Memory snapshot visibility
9. Security event visibility
10. Pause/resume controls
11. Safe admin actions:

    * resend clarification
    * answer clarification
    * cancel clarification
    * republish GitHub statuses
    * validate branch protection

## Out of scope

Do NOT build:

* public UI
* user management
* OAuth
* role-based permissions
* mobile-perfect UI
* drag/drop workflow editor
* complex charts
* real-time WebSocket updates
* multi-project portfolio dashboard
* external deployment dashboard

---

# 7. Iteration Plan

## Iteration 0 — Dashboard baseline

### Goal

Add a protected dashboard shell.

### Tasks

* Add `/admin/ui/login`
* Add `/admin/ui/logout`
* Add `/admin/ui`
* Implement admin-key based login
* Set secure admin cookie
* Add middleware/helper:

```python
require_admin_ui(request)
```

* Add base HTML layout:

  * navigation
  * environment label
  * current pause status
  * logout link

### Acceptance criteria

* `/admin/ui` redirects to login when unauthenticated
* valid admin key logs in
* invalid key rejected
* logout clears session
* `/debug/*` API auth remains unchanged

Then STOP.

---

## Iteration 1 — System overview page

### Goal

Create a useful home dashboard.

### Page

```text
/admin/ui
```

Show cards:

```text
Environment: DEV/PROD
Pause status
GitHub writes allowed
Auto-merge allowed
Pending clarifications
Recent failed workflows
Recent blocked releases
Recent security events
Last GitHub status publish
```

### Backend needs

Use existing endpoints/functions where possible:

* control flags
* recent workflow runs
* pending clarifications
* security events
* GitHub status updates

### Acceptance criteria

* dashboard loads quickly
* no secrets displayed
* paused state clearly visible
* failed/blocked counts visible

Then STOP.

---

## Iteration 2 — Workflow runs list

### Goal

Make runs inspectable without curl.

### Page

```text
/admin/ui/runs
```

Columns:

```text
Run ID
Workflow Type
Issue Key
Status
Current Step
Test Status
Review Status
Test Quality Status
Architecture Status
Release Decision
Merge Status
Created At
```

Filters:

```text
status
workflow_type
issue_key
release_decision
limit
```

### Acceptance criteria

* recent runs visible
* failed/blocked runs visually identifiable
* clicking run opens detail page

Then STOP.

---

## Iteration 3 — Workflow run detail page

### Goal

Create one full view of a run.

### Page

```text
/admin/ui/runs/{run_id}
```

Sections:

1. Run summary
2. Jira issue info
3. Repo/branch/PR info
4. Test result
5. Reviewer Agent verdict
6. Test Quality Agent verdict
7. Architecture Agent verdict
8. Release Gate decision
9. GitHub statuses
10. Clarifications
11. Feedback events
12. Error details

### Actions:

* Republish GitHub statuses
* Open PR
* Open Jira issue
* Resend clarification if pending
* Cancel clarification if pending

### Acceptance criteria

* one page tells full story of a run
* PR/Jira links work
* republish button works with confirmation
* no destructive action without confirmation

Then STOP.

---

## Iteration 4 — Planning page

### Goal

Inspect Epic → Story planning.

### Page

```text
/admin/ui/planning
```

Show:

```text
Planning run ID
Epic key
Status
Approval status
Stories proposed
Stories created
Created At
```

Detail view can be either:

* included inline
* linked to run detail page

Show proposed Stories:

* title
* acceptance criteria
* status
* created Jira key

### Acceptance criteria

* approved/rejected/regenerated planning runs visible
* created Jira Story keys visible
* no Feature terminology appears unless historical data requires it

Then STOP.

---

## Iteration 5 — Clarifications page

### Goal

Manage pending clarifications.

### Page

```text
/admin/ui/clarifications
```

Tabs/filters:

```text
Pending
Answered
Cancelled
Expired
```

For pending clarification:

Show:

* clarification ID
* run ID
* issue key
* question
* options
* created_at
* expires_at

Actions:

* answer
* cancel
* resend

### Acceptance criteria

* pending clarifications visible
* answer via UI resumes workflow
* cancel via UI fails workflow safely
* resend sends Telegram again

Then STOP.

---

## Iteration 6 — Agent reviews page

### Goal

Make agent verdicts easy to inspect.

### Page

```text
/admin/ui/agents
```

Sections:

```text
Reviewer Agent
Test Quality Agent
Architecture Agent
```

Filters:

```text
agent_type
status
repo_slug
run_id
```

Show:

* verdict/status
* risk/confidence
* summary
* PR link
* run link

### Acceptance criteria

* all three agent types visible
* blocked/weak/needs-review statuses easy to find
* linked to workflow run

Then STOP.

---

## Iteration 7 — GitHub statuses page

### Goal

Inspect GitHub status publishing and branch protection.

### Page

```text
/admin/ui/github
```

Sections:

1. Recent GitHub status updates
2. Per-run status list
3. Branch protection check form

Actions:

* republish statuses for run
* validate required checks for repo/branch
* open GitHub PR

### Acceptance criteria

* can see five statuses per run
* can validate `orchestrator/release-gate`
* warnings visible when branch protection is missing

Then STOP.

---

## Iteration 8 — Memory page

### Goal

Inspect memory and feedback.

### Page

```text
/admin/ui/memory
```

Show:

```text
Repo-level memory snapshots
Epic-level rollups
Manual memory notes
Feedback event counts
```

Actions:

* add manual memory note
* recompute memory snapshot if endpoint exists
* view evidence JSON

### Acceptance criteria

* memory snapshots readable
* manual notes distinguishable from derived memory
* evidence visible but not overwhelming

Then STOP.

---

## Iteration 9 — Security page

### Goal

Make Phase 11 security layer operable.

### Page

```text
/admin/ui/security
```

Show:

```text
Recent security events
Failed admin auth attempts
Rejected webhooks
Rejected Telegram commands
GitHub write blocks
Rate limit events
```

Filters:

```text
event_type
source
status
limit
```

### Acceptance criteria

* recent security events visible
* failed/rejected events stand out
* no secrets displayed

Then STOP.

---

## Iteration 10 — Control page

### Goal

Expose pause/resume and runtime flags safely.

### Page

```text
/admin/ui/control
```

Show flags:

```text
paused
allow_jira_webhooks
allow_telegram_commands
allow_github_writes
allow_auto_merge
```

Actions:

* pause orchestrator
* resume orchestrator
* disable GitHub writes
* enable GitHub writes
* disable auto-merge
* enable auto-merge

### Safety requirements

* each action requires confirmation
* show current environment clearly
* log security event for each action

### Acceptance criteria

* pause/resume works from UI
* flag changes reflected immediately
* actions are audited

Then STOP.

---

## Iteration 11 — UI hardening and consistency

### Goal

Make the dashboard safe and usable.

### Tasks

* Add consistent error pages
* Add loading/error states
* Add timestamp formatting
* Add environment badge on every page
* Hide stack traces from UI
* Add pagination or limit controls
* Add CSRF protection for POST forms if using cookies

### Acceptance criteria

* POST actions protected against accidental repeat
* UI does not leak secrets or raw tracebacks
* all admin actions require confirmation

Then STOP.

---

## Iteration 12 — End-to-end validation

### Required scenarios

#### Scenario A — Login/logout

```text
Unauthenticated user redirected
valid admin key logs in
logout clears session
```

#### Scenario B — Overview page

```text
shows environment, pause status, recent runs, alerts
```

#### Scenario C — Run detail

```text
one real run shows tests, agents, release gate, GitHub statuses
```

#### Scenario D — Clarification management

```text
pending clarification answered from UI
workflow resumes
```

#### Scenario E — GitHub status republish

```text
republish from UI
DB records new status attempts
```

#### Scenario F — Branch protection validation

```text
UI detects missing release-gate required check
```

#### Scenario G — Pause/resume

```text
pause from UI blocks new workflows
resume restores processing
security event recorded
```

#### Scenario H — Security page

```text
failed admin auth / rejected webhook visible
```

### Acceptance criteria

* all pages protected
* all major operational actions work
* no regression in existing API behavior
* no secrets displayed

Then STOP.

---

# 8. Suggested UI Layout

Use a simple sidebar:

```text
Overview
Workflow Runs
Planning
Clarifications
Agents
GitHub
Memory
Security
Control
Logout
```

Use minimal status colors:

```text
green  = success / approved / merged
yellow = skipped / weak / needs review / waiting
red    = failed / blocked / rejected
gray   = unknown / not run
blue   = informational
```

Do not over-style. Usability first.

---

# 9. Security Requirements

Because Phase 11 added security controls, Phase 14 must not weaken them.

Rules:

* `/admin/ui/*` must require authentication
* do not expose admin key in HTML
* do not log admin key
* use CSRF protection for POST actions if using cookie auth
* never show secret env var values
* all mutating UI actions must call existing protected backend logic
* all mutating actions must record security events

---

# 10. Recommended Implementation Style

Keep frontend simple:

```text
FastAPI route
→ query DB/helper
→ render Jinja2 template
```

Recommended packages:

```text
jinja2
python-multipart  # only if needed for forms
itsdangerous      # optional signed cookie/session helper
```

Folder structure:

```text
app/templates/admin/
  base.html
  login.html
  overview.html
  runs.html
  run_detail.html
  planning.html
  clarifications.html
  agents.html
  github.html
  memory.html
  security.html
  control.html

app/static/admin/
  admin.css
```

---

# 11. API reuse principle

Do not duplicate business logic.

UI POST actions should call existing service functions or route helpers:

```text
pause/resume → existing control flag functions
answer clarification → existing mark + resume functions
republish statuses → existing publish function
branch audit → existing GitHub helper
memory note → existing memory endpoint/helper
```

---

# 12. Definition of Done

Phase 14 is complete when:

* Admin login/logout works
* Dashboard overview works
* Workflow runs are inspectable
* Run detail shows full lifecycle
* Planning runs visible
* Clarifications manageable
* Agent verdicts visible
* GitHub statuses and branch protection visible
* Memory snapshots visible
* Security events visible
* Pause/resume/control flags manageable
* All pages protected
* No secrets exposed
* Existing API behavior remains intact

---

# 13. Final Instruction to Claude

Build Phase 14 as an **operations console**, not a product frontend.

The system is now powerful and secure, but too hard to operate manually.

The goal is:

```text
One place to see what happened,
why it happened,
what is waiting,
what is blocked,
and what action is safe to take.
```

Optimize for:

* clarity
* safety
* low complexity
* reuse of existing backend functions
* fast debugging
* secure admin access

Do not optimize for:

* fancy UI
* public-user polish
* charts everywhere
* frontend framework complexity
* new workflow behavior

The standard for Phase 14:

> You should be able to operate the AI Dev Orchestrator from the dashboard without needing curl for normal daily use.
