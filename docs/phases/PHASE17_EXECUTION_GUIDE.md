# PHASE 17 EXECUTION GUIDE — Real Project Onboarding & Repo Understanding

## 1. Objective

Phase 17 moves the orchestrator from sandbox validation to real personal-project usage.

Target project:

```text
Learning Platform website / repo
```

The goal is:

```text
Existing real repo
→ orchestrator scans it
→ understands stack and structure
→ creates project knowledge snapshot
→ validates commands
→ proposes safe onboarding improvements
→ executes one real Story end-to-end
```

Phase 16 already added post-merge deployment validation and smoke testing, so Phase 17 should use that capability where possible, but not expand deployment automation. 

---

# 2. What Phase 17 Is Trying to Prove

Phase 17 should answer:

```text
Can this orchestrator safely understand and improve one of Suyog’s real projects?
```

Not:

```text
Can it support every possible repo?
```

Focus on your Learning Platform repo first.

---

# 3. Scope

## In scope

1. Real repo onboarding workflow
2. Repo architecture scan
3. Capability profile validation
4. Project knowledge snapshot
5. Test/build/lint/deploy profile verification
6. Jira mapping setup
7. First real Epic → Stories
8. First safe Story implementation
9. Post-merge deployment validation if profile is configured
10. Dashboard visibility for onboarded repo

## Out of scope

Do NOT build:

* full RAG system
* vector database
* full documentation ingestion
* multi-repo portfolio intelligence
* production deployment automation
* large feature implementation
* self-improvement mode yet
* greenfield project bootstrap yet

---

# 4. New Concept: Project Onboarding

Add an onboarding workflow:

```text
project_onboarding
```

Triggered manually first, not automatically from Jira.

Purpose:

```text
Read a repo and generate a usable project understanding package.
```

Later, Jira Epic can reference this knowledge.

---

# 5. Data Model Changes

## 5.1 New table: `project_onboarding_runs`

```sql
id SERIAL PRIMARY KEY,
repo_slug VARCHAR(200) NOT NULL,
base_branch VARCHAR(100) DEFAULT 'main',
status VARCHAR(50) NOT NULL,
capability_profile_name VARCHAR(100),
architecture_summary TEXT,
test_command TEXT,
build_command TEXT,
lint_command TEXT,
deployment_profile_status VARCHAR(50),
risk_notes_json TEXT,
recommendations_json TEXT,
created_at TIMESTAMP DEFAULT NOW(),
completed_at TIMESTAMP NULL
```

Statuses:

```text
PENDING
RUNNING
COMPLETED
FAILED
```

## 5.2 New table: `project_knowledge_snapshots`

```sql
id SERIAL PRIMARY KEY,
repo_slug VARCHAR(200) NOT NULL,
snapshot_kind VARCHAR(100) NOT NULL,
summary TEXT NOT NULL,
details_json TEXT NULL,
source_files_json TEXT NULL,
created_at TIMESTAMP DEFAULT NOW(),
updated_at TIMESTAMP DEFAULT NOW()
```

Snapshot kinds:

```text
architecture
commands
testing
deployment
coding_conventions
open_questions
```

---

# 6. New Admin APIs

Add admin-protected endpoints:

```text
POST /admin/project-onboarding/start
GET /admin/project-onboarding/runs
GET /admin/project-onboarding/runs/{id}
GET /debug/project-knowledge?repo_slug=...
POST /debug/project-knowledge/{repo_slug}/refresh
```

Payload for start:

```json
{
  "repo_slug": "suyog19/learning-platform",
  "base_branch": "main"
}
```

---

# 7. Iteration Plan

## Iteration 0 — Schema and constants

### Goal

Prepare database and constants.

### Tasks

* Add `project_onboarding_runs`
* Add `project_knowledge_snapshots`
* Add constants:

  * `PROJECT_ONBOARDING`
  * onboarding statuses
  * snapshot kinds
* Add dashboard route placeholder:

  * `/admin/ui/projects`

### Acceptance criteria

* migrations idempotent
* existing workflows unaffected
* dashboard still loads

Then STOP.

---

## Iteration 1 — Manual onboarding start API

### Goal

Start onboarding from admin API.

### Tasks

Implement:

```text
POST /admin/project-onboarding/start
```

Behavior:

1. validate repo_slug
2. create onboarding run
3. enqueue onboarding job
4. return onboarding_run_id

### Acceptance criteria

* admin key required
* invalid repo slug rejected
* onboarding run created
* job queued

Then STOP.

---

## Iteration 2 — Repo clone and profile detection

### Goal

Reuse existing repo clone and capability profile detection.

### Tasks

On onboarding job:

1. clone repo
2. checkout base branch
3. run `detect_repo_capability_profile`
4. upsert `repo_capability_profiles`
5. update onboarding run with profile info

### Acceptance criteria

* Learning Platform repo can be cloned
* capability profile detected
* profile stored
* failure captured clearly

Then STOP.

---

## Iteration 3 — Command validation dry run

### Goal

Validate known commands without changing code.

### Tasks

Using capability profile:

* detect test command
* detect build command
* detect lint command
* run safe commands if available

Policy:

```text
test command: run
build command: run if not too expensive
lint command: run if configured
```

Store results in onboarding run.

### Acceptance criteria

* commands run or skip clearly
* output captured safely
* timeout applied
* no repo modifications

Then STOP.

---

## Iteration 4 — Repo structure scanner

### Goal

Generate structural understanding.

### Tasks

Create scanner:

```python
scan_repo_structure(workspace_path, profile) -> dict
```

Collect:

```text
top-level folders
source folders
test folders
config files
package/build files
routing/API files
models/entities
services/components
docs files
deployment files
```

Limit output size.

### Acceptance criteria

* scanner works for Learning Platform repo
* output stored as JSON
* no huge file dumps

Then STOP.

---

## Iteration 5 — Architecture summary generator

### Goal

Ask Claude to summarize project architecture.

### Input

* repo structure scan
* capability profile
* selected important files
* README/package/build config
* existing tests overview

### Output

Structured:

```json
{
  "architecture_summary": "...",
  "main_modules": [],
  "entry_points": [],
  "data_flow": "...",
  "test_strategy": "...",
  "deployment_notes": "...",
  "risks": [],
  "open_questions": []
}
```

Store as `project_knowledge_snapshots`.

### Acceptance criteria

* architecture snapshot created
* summary is concise and useful
* open questions are explicit
* no hallucinated certainty

Then STOP.

---

## Iteration 6 — Coding conventions snapshot

### Goal

Capture project style.

### Tasks

Analyze:

```text
naming conventions
folder organization
component/service patterns
test naming
API style
state management style if frontend
error handling style
```

Store snapshot:

```text
snapshot_kind = coding_conventions
```

### Acceptance criteria

* conventions snapshot exists
* future agents can use it
* summary is practical, not vague

Then STOP.

---

## Iteration 7 — Deployment profile check

### Goal

Connect repo to Phase 16 deployment validation if possible.

### Tasks

* check existing deployment profile for repo/environment
* if none, create disabled draft profile
* infer possible deployment type:

  * GitHub Pages
  * Hugging Face Space
  * EC2 service
  * none/unknown
* add recommendations

### Acceptance criteria

* deployment profile status visible
* disabled draft profile created if useful
* no deployment validation enabled without real base_url

Then STOP.

---

## Iteration 8 — Project dashboard page

### Goal

Expose onboarding result in UI.

Add page:

```text
/admin/ui/projects
/admin/ui/projects/{repo_slug}
```

Show:

```text
repo slug
capability profile
command status
architecture summary
coding conventions
deployment profile
open questions
recommendations
latest onboarding run
```

### Acceptance criteria

* Learning Platform project visible
* snapshots readable
* links to runs/workflows available

Then STOP.

---

## Iteration 9 — Jira repo mapping setup helper

### Goal

Make repo ready for Epic → Story execution.

### Tasks

Add helper endpoint:

```text
POST /admin/project-onboarding/{repo_slug}/create-jira-mapping
```

Payload:

```json
{
  "jira_project_key": "LP",
  "base_branch": "main",
  "environment": "dev"
}
```

Behavior:

* create or update repo mapping
* link mapping to capability profile
* verify mapping health

### Acceptance criteria

* mapping created for Learning Platform repo
* mapping health passes
* dashboard shows mapping status

Then STOP.

---

## Iteration 10 — Prompt enrichment from project knowledge

### Goal

Use onboarding knowledge in future planning/execution.

### Tasks

Before Epic planning or Story execution for this repo, retrieve:

```text
architecture snapshot
coding conventions snapshot
deployment snapshot
manual memory notes
```

Inject bounded context into prompts.

Limit:

```text
max 5 project knowledge bullets
max 1200 chars
```

### Acceptance criteria

* prompts include project knowledge for onboarded repo
* prompt memory usage records it
* no prompt explosion

Then STOP.

---

## Iteration 11 — First real Epic planning rehearsal

### Goal

Use the onboarded project for planning, but keep implementation safe.

### Tasks

Create a small Jira Epic for Learning Platform.

Example safe Epic:

```text
Improve landing page footer content and structure
```

or

```text
Add simple About section placeholder
```

Run:

```text
Epic → Story decomposition
Telegram approval
Story creation
```

### Acceptance criteria

* Stories are small
* no overreach
* project knowledge appears in planning prompt
* user approval works

Then STOP.

---

## Iteration 12 — First real Story implementation

### Goal

Execute one low-risk improvement.

Criteria for first Story:

```text
small UI/text/content improvement
no auth
no payment
no destructive DB change
no deployment config change
```

Run full pipeline:

```text
Story → implementation → tests/build/lint → agents → release gate → PR
```

Auto-merge policy:

```text
Disable auto-merge for first real repo run unless user explicitly enables.
```

### Acceptance criteria

* PR created
* agents review
* GitHub statuses published
* dashboard shows full lifecycle
* user manually reviews PR

Then STOP.

---

## Iteration 13 — Optional merge and deployment validation

### Goal

Validate actual usage flow.

If user approves PR:

* merge manually or allow controlled merge
* run deployment validation if deployment profile configured
* otherwise record `NOT_CONFIGURED`

### Acceptance criteria

* merge does not break repo
* deployment validation result visible
* memory captures first real run

Then STOP.

---

## Iteration 14 — Onboarding retrospective snapshot

### Goal

Record what the orchestrator learned from onboarding.

Create snapshot:

```text
snapshot_kind = onboarding_retrospective
```

Include:

```text
what worked
what failed
repo-specific improvements needed
recommended next Epics
manual actions required
confidence level
```

### Acceptance criteria

* retrospective visible in project dashboard
* recommendations are actionable
* no further platform changes required for using repo

Then STOP.

---

# 8. Safety Rules

For Learning Platform first use:

```text
auto_merge = false initially
deployment_validation = observational
no large refactors
no auth/payment changes
no destructive storage changes
max changed files = 3
one Story at a time
```

---

# 9. Definition of Done

Phase 17 is complete when:

* Learning Platform repo is onboarded
* capability profile detected
* commands validated
* architecture snapshot created
* coding conventions snapshot created
* deployment profile checked
* Jira repo mapping configured
* dashboard shows project knowledge
* prompts use project knowledge
* one real Epic is planned
* one real Story is implemented into PR
* first real run is reviewed safely
* onboarding retrospective exists

---

# 10. Final Instruction to Claude

Build Phase 17 as **real project onboarding**, not another generic framework expansion.

The question is:

```text
Can the orchestrator understand Suyog’s real Learning Platform repo well enough to safely make its first useful change?
```

Optimize for:

* conservative repo understanding
* project-specific knowledge
* first safe PR
* dashboard visibility
* no risky auto-merge
* learning from onboarding

Do not optimize for:

* large feature delivery
* broad RAG
* perfect architecture analysis
* automatic deployment
* many repos at once

The standard for Phase 17:

> After this phase, the orchestrator should be ready to help improve the Learning Platform project through normal Jira Epic → Story workflow.
