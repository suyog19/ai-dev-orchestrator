# PHASE 3 EXECUTION GUIDE — AI DEV ORCHESTRATOR

## 1. Objective

Phase 3 upgrades the AI Dev Orchestrator from a working prototype into a safe, reviewable development assistant that can operate against a real target repository, with proper environment isolation, stronger error handling, better repo mapping, unique branch strategy, and meaningful code generation.

In Phase 1, the system learned to receive events, dispatch workflows, queue jobs, and notify via Telegram.

In Phase 2, the system learned to:
- clone a repository
- analyze it
- ask Claude for a summary and suggested change
- apply a change
- commit and push to GitHub
- create a PR

Phase 3 will make that pipeline production-shaped, not by making it “fully autonomous,” but by making it:
- safer
- more explicit
- more reliable
- less circular
- easier to debug
- usable on a real sandbox repo before touching serious codebases

---

## 2. What Phase 3 Is Trying to Achieve (Simple Language)

Phase 3 is about teaching the system to do real work in a controlled way.

Right now, the system can prove “I can touch GitHub.”

After Phase 3, the system should prove:

- I know which repo to work on
- I can work on a separate target repo, not on myself
- I can fail cleanly instead of getting stuck
- I create unique branches and PRs that don’t collide
- I run in isolated dev and prod environments
- I use a stronger Claude model for meaningful code generation
- I create PRs that a human can realistically review

In short:

Phase 3 = move from “cool demo” to “controlled engineering tool”

---

## 3. Mandatory Prerequisites Before Writing Phase 3 Code

These are not optional. Complete them first.

### 3.1 Environment Split: Separate Dev and Prod VMs

You must create a separate production VM before implementing new Phase 3 features.

#### Required environment model

| Environment | VM | Branch | Runner Label | Purpose |
|---|---|---|---|---|
| Dev | Existing EC2 | `dev` | `self-hosted-dev` | Development and testing |
| Prod | New EC2 | `main` | `self-hosted-prod` | Stable environment |

### Why this is mandatory

Do NOT run both `dev` and `main` on the same VM anymore.

Reasons:
- Both environments now have real side effects
- A bad dev test can create unwanted GitHub pushes/PRs/Telegram messages
- Shared runner can block both environments
- Shared Docker stack causes last-deploy-wins behavior
- Phase 3 now deserves actual environment separation

### Required tasks

1. Provision new EC2 VM for production
2. Run existing setup scripts on that VM:
   - `scripts/setup-vm.sh`
   - `scripts/setup-runner.sh`
   - `scripts/setup-ssl.sh`
3. Register runner with label:
   - `self-hosted-prod`
4. Update existing dev runner label to:
   - `self-hosted-dev`
5. Update GitHub workflows:
   - `deploy-dev.yml` -> `runs-on: self-hosted-dev`
   - `deploy-main.yml` -> `runs-on: self-hosted-prod`

### Acceptance criteria

- `dev` deploys only to dev VM
- `main` deploys only to prod VM
- each VM has its own Docker stack
- each VM has its own GitHub Actions runner
- each VM has its own `.env.orchestrator`

### Verify

Run these checks:

```bash
# On dev VM
docker ps
systemctl status actions.runner.*

# On prod VM
docker ps
systemctl status actions.runner.*
```

Then confirm GitHub workflow runs land on the correct runners.

---

### 3.2 Domain and SSL Split

You must expose distinct HTTPS endpoints for dev and prod.

Recommended:
- Prod: `orchestrator.suyogjoshi.com`
- Dev: `dev.orchestrator.suyogjoshi.com`

### Acceptance criteria

- both domains resolve correctly
- both have valid TLS certificates
- Jira can target each independently if needed

### Verify

```bash
curl -I https://orchestrator.suyogjoshi.com/healthz
curl -I https://dev.orchestrator.suyogjoshi.com/healthz
```

Expected: HTTP 200 or equivalent healthy response

---

### 3.3 Anthropic API Prerequisite Checklist

This was a major Phase 2 pain point and must be explicitly verified before coding.

For each environment where Claude will run:

1. Confirm which Anthropic workspace the API key belongs to
2. Confirm that workspace has purchased API credits
3. Confirm the API key is valid using a minimal API test
4. Confirm the key exists in:
   - `/home/ubuntu/.env.orchestrator`
   - project directory `.env`
5. If env vars changed, redeploy using:

```bash
docker compose up -d --force-recreate
```

### Acceptance criteria

- API key is valid
- credits are usable from that workspace
- both env files are updated
- container sees the updated key

---

### 3.4 Two-File `.env` Pattern Must Be Documented

Before Phase 3 coding starts, update `CLAUDE.md` or project docs with this exact rule:

#### Persistent env file
- `/home/ubuntu/.env.orchestrator`
- source of truth on VM

#### Runtime env file
- `<project_dir>/.env`
- actually read by Docker containers

#### Important rule
If a secret changes manually mid-iteration:
- update both files
- then run:

```bash
docker compose up -d --force-recreate
```

### Acceptance criteria

- this pattern is documented clearly
- Claude does not assume a single `.env` file model

---

## 4. Working Style for Phase 3

Continue the same rule from earlier phases:

### Mandatory loop
1. Implement one small feature
2. Test locally using Docker where relevant
3. Deploy to dev VM
4. Verify via webhook/logs/GitHub/Telegram
5. Only then move ahead

### New Phase 3 rule
Use autonomous SSH testing wherever possible.

Once Claude has:
- EC2 IP / DNS
- SSH key access
- repo access
- GitHub API access
- target sandbox repo details

Claude should run:
- deploy wait
- webhook tests
- log inspection
- PR inspection
- cleanup checks

This should reduce manual back-and-forth.

---

## 5. Architectural Changes Required in Phase 3

These are the core upgrades.

### 5.1 Real Repo Mapping Strategy

Phase 2 used a hardcoded mapping entry. That is no longer enough.

You must implement a real repo mapping strategy.

#### Recommended design
Use the database as the source of truth.

### Table: `repo_mappings`

Required fields:
- `id`
- `jira_project_key`
- `issue_type` (nullable if mapping applies broadly)
- `repo_slug`
- `base_branch`
- `is_active`
- `created_at`
- `updated_at`

Optional useful fields:
- `notes`
- `environment` (`dev` / `prod`)
- `allowed_workflow_type`

### Lookup rule
When a workflow starts:
1. identify Jira project key
2. identify issue type
3. query repo mapping
4. choose the most specific active mapping
5. fail clearly if no mapping exists

### API requirements
Add management endpoints for repo mappings, for example:
- `GET /debug/repo-mappings`
- `POST /debug/repo-mappings`
- `PUT /debug/repo-mappings/{id}`
- `DELETE /debug/repo-mappings/{id}` or soft-disable

### Acceptance criteria

- mappings are not hardcoded in code
- multiple Jira projects can map to different repos
- missing mapping produces `FAILED` workflow state and Telegram alert
- mapping resolution is logged clearly

### Verify

```bash
curl -s https://dev.orchestrator.suyogjoshi.com/debug/repo-mappings
```

and run a workflow against at least two different Jira project keys.

---

### 5.2 Stop Using the Orchestrator Repo as the Target Repo

This is mandatory.

Phase 3 must use a separate sandbox target repository for workflow testing.

#### Recommended target repo characteristics
Create or choose a sandbox repo that:
- is separate from `ai-dev-orchestrator`
- has a small but realistic codebase
- is safe to modify repeatedly
- has tests or at least lintable code
- is not business-critical

Examples:
- a toy FastAPI app
- a simple Python utility repo
- a demo frontend/backend repo
- a learning platform sandbox repo

### Acceptance criteria

- no Phase 3 workflow should target `ai-dev-orchestrator` as the coding sandbox
- all code generation tests run against the sandbox repo
- PRs are created in the sandbox repo

---

### 5.3 Stronger Claude Model Strategy

Haiku is fine for cheap analysis, but not for meaningful implementation from Jira stories.

#### Required model split

Use:
- Haiku for:
  - repo summary
  - lightweight analysis
  - formatting help
- Sonnet (recommended) or stronger model for:
  - story-based code generation
  - code editing plans
  - implementation suggestions
  - patch generation

If extended thinking is available and cost-acceptable, use it only where meaningful.

### Acceptance criteria

- model choice is explicit in code/config
- repo summary and code generation are not forced through the same cheap model
- the code generation path uses a stronger model than Haiku

---

### 5.4 Workflow-Level Error Handling

This is mandatory.

Currently a workflow can get stuck in `RUNNING`. Phase 3 must fix this.

#### Required behavior

Wrap the full workflow body in robust error handling:
- `try`
- `except`
- `finally` where needed

If an exception happens:
1. set workflow status to `FAILED`
2. persist error summary
3. persist traceback or detailed error text
4. send Telegram failure notification
5. ensure worker does not leave orphan state

### Data model changes

Extend `workflow_runs` with:
- `error_message` (nullable)
- `error_traceback` (nullable)
- `started_at` (nullable)
- `completed_at` (nullable)

Optional:
- `current_step`
- `step_details`

### Acceptance criteria

- failed runs end in `FAILED`, not `RUNNING`
- Telegram failure message contains useful context
- traceback can be inspected later
- success path still ends in `COMPLETED`

### Verify

Intentionally break one workflow step and confirm:
- DB shows `FAILED`
- Telegram shows failure
- logs contain traceback

---

### 5.5 Unique Branch Naming Strategy

Phase 2 reused the same branch per issue key. That will collide.

#### Required branch naming rule

Use a unique branch name per run.

Recommended format:

```text
ai/<issue-key>/<run-id>
```

or

```text
ai/<issue-key>/<timestamp>
```

If Git branch naming becomes too long, shorten the run ID.

### Additional requirement

Store the created branch name in `workflow_runs` or a related field so PRs and logs can reference it later.

### Acceptance criteria

- repeated runs for same Jira issue create different branches
- repeated runs create distinct PRs
- old runs do not get overwritten

### Verify

Trigger same Jira story twice and confirm:
- two different branches exist
- two distinct PRs are visible

---

### 5.6 PR Content Must Be Reviewable

Phase 2 created PRs, but Phase 3 must verify actual PR quality, not just existence.

#### Required PR fields

At minimum, PR content should include:
- Jira issue key
- workflow run ID
- Claude-generated summary
- what changed
- why it changed
- any known limitations
- files modified

If possible, include a short “review notes” section.

### Acceptance criteria

Each verification cycle must inspect:
- PR title
- PR body
- commit message
- diff quality

### Verify

Use GitHub CLI or API, for example:

```bash
gh pr view <number> --json title,body,commits,files
```

Review:
- title makes sense
- body is not empty or misleading
- commit message is clear
- diff reflects intended change

---

## 6. Phase 3 Iteration Plan

Follow this order strictly.

---

## Iteration 0 — Phase 3 Prerequisite Setup

This iteration is mandatory and must happen before feature work.

### Goal
Separate environments and remove hidden configuration risks.

### Tasks
- create prod VM
- register prod runner
- relabel dev runner
- split domains and SSL
- document two-file `.env` model
- verify Anthropic API key and billing path
- update workflows to deploy to separate runners

### Acceptance criteria
- dev and prod fully isolated
- both domains healthy
- both env models documented
- Anthropic API verified

### Verify
Use:
- `curl /healthz`
- runner status
- deploy logs
- minimal Anthropic API test

Then STOP and confirm Phase 3 prerequisites are complete.

---

## Iteration 1 — Real Repo Mapping System

### Goal
Replace hardcoded mapping with a real configurable mapping system.

### Tasks
- redesign `repo_mappings` table
- update lookup logic
- add CRUD debug endpoints
- validate active mapping resolution
- fail cleanly when mapping missing

### Out of scope
- no code generation changes yet
- no prompt changes yet

### Acceptance criteria
- mappings managed via API/DB
- workflows use DB mapping
- missing mapping fails cleanly
- mapping resolution logged

### Verify
- create two mappings
- resolve each correctly from test payloads
- confirm failure for unmapped project

Then STOP.

---

## Iteration 2 — Sandbox Target Repo Setup

### Goal
Move workflow execution to a separate test repo.

### Tasks
- create or choose sandbox repo
- add mapping for sandbox Jira project/story
- ensure clone/branch/PR pipeline points to sandbox repo
- remove reliance on orchestrator repo as target

### Acceptance criteria
- Phase 3 test workflow modifies sandbox repo only
- PRs appear in sandbox repo
- orchestrator repo remains untouched

### Verify
- trigger a test story
- inspect target repo branch/PR
- confirm orchestrator repo has no workflow-generated branch

Then STOP.

---

## Iteration 3 — Workflow Failure Handling

### Goal
Make workflow lifecycle robust.

### Tasks
- extend `workflow_runs` schema
- wrap workflow in `try/except`
- store error message and traceback
- send Telegram failure notifications
- set `FAILED` state properly

### Acceptance criteria
- induced failure transitions workflow to `FAILED`
- failure details stored in DB
- Telegram alerts user
- success still works

### Verify
Intentionally break:
- repo mapping
- git push
- Claude call
or file apply step

Then inspect DB and Telegram.

Then STOP.

---

## Iteration 4 — Unique Branch and PR Strategy

### Goal
Remove branch collision and PR overwriting.

### Tasks
- implement unique branch naming using issue key + run ID/timestamp
- store branch name in workflow metadata
- update PR creation logic to use unique branch
- confirm repeated runs create distinct PRs

### Acceptance criteria
- same Jira issue can trigger multiple safe runs
- old PRs remain intact
- branches are traceable to workflow runs

### Verify
Trigger the same story twice and inspect:
- Git branches
- PR list
- DB metadata

Then STOP.

---

## Iteration 5 — Stronger Claude Model for Implementation

### Goal
Improve the quality of generated changes.

### Tasks
- introduce model selection config
- keep Haiku for summary/cheap analysis
- use Sonnet or stronger model for story-based code changes
- log which model handled which step
- ensure code generation prompt is explicit and scoped

### Acceptance criteria
- implementation step no longer relies on Haiku
- workflow logs show model choice
- suggestion quality improves beyond trivial edits

### Verify
Run same Jira story through both:
- old Haiku path (if kept for comparison)
- new stronger-model path

Compare:
- relevance
- patch usefulness
- hallucination rate
- review quality

Then STOP.

---

## Iteration 6 — Story-to-Implementation Prompting

### Goal
Generate code based on Jira story content, not just generic repo suggestions.

### Tasks
- extract from Jira webhook or issue data:
  - issue key
  - title
  - description
  - acceptance criteria (if available)
- build structured implementation prompt
- instruct Claude to:
  - propose change relevant to story
  - keep scope limited
  - avoid unrelated refactors
  - specify files changed
- preserve prompt and response logs for debugging

### Acceptance criteria
- Claude suggestion is tied to actual Jira story intent
- changes are not generic cleanup-only
- prompt structure is explicit and inspectable

### Verify
Use a realistic sandbox Jira story and confirm:
- PR title/body map to the story
- code changes relate to the story description
- Telegram summary mentions story intent

Then STOP.

---

## Iteration 7 — Pre-Apply Validation for Claude Suggestions

### Goal
Reduce bad edits before touching files.

### Tasks
Before applying Claude-generated changes:
- validate target file exists
- validate target snippet or insertion point exists
- reject ambiguous edits
- if validation fails, mark workflow `FAILED` or request fallback path
- stop using “always commit something” fallback for production-shaped runs unless explicitly configured

### Important design choice
For Phase 3, prefer safe failure over fake success.

In Phase 2, timestamp fallback was useful for proving the pipeline.
In Phase 3, that fallback can hide bad code generation.

### Acceptance criteria
- invalid suggestions fail clearly
- no meaningless fallback edits unless specifically enabled for debug mode
- PRs contain intentional changes, not dummy markers

### Verify
Force Claude to produce a bad target snippet and confirm:
- workflow fails clearly
- no dummy commit/PR is created

Then STOP.

---

## Iteration 8 — PR Quality and Review Metadata

### Goal
Make PRs genuinely reviewable.

### Tasks
Improve PR title/body to include:
- Jira issue key
- workflow run ID
- summary of requested change
- implementation summary
- files changed
- model used
- known limitations / confidence notes
- testing performed (if any)

Improve commit message format similarly.

### Acceptance criteria
- PR body is informative
- commit message is traceable
- reviewer can understand change without reading logs

### Verify

```bash
gh pr view <number> --json title,body,commits,files
```

Review that:
- title is meaningful
- body explains the change
- files list is accurate
- commit message is not vague

Then STOP.

---

## Iteration 9 — End-to-End Phase 3 Validation

### Goal
Validate the full Phase 3 flow against a real sandbox repo and realistic Jira story.

### Flow
1. Jira story moves to trigger state
2. webhook received
3. repo mapping resolved
4. workflow run created
5. unique branch created
6. repo analyzed
7. stronger Claude model generates story-based change
8. change validated before apply
9. files updated
10. commit created
11. push succeeds
12. PR created
13. Telegram shows meaningful progress
14. DB reflects final success or failure accurately

### Acceptance criteria
- end-to-end flow works in dev against sandbox repo
- every step is logged
- failure paths are recoverable and observable
- PR is meaningful
- no collision with previous runs
- workflow state is trustworthy

### Verify checklist

Run all of these:
- webhook test
- DB query for workflow run
- inspect Telegram notifications
- inspect git branch on remote
- inspect PR via `gh pr view`
- inspect workflow status in DB
- inspect worker logs

Then STOP and review before planning Phase 4.

---

## 7. Trigger Definitions Must Be Explicit

This was a problem before. Do not leave status names as examples.

Before implementing or testing, document exact trigger values.

### Required Phase 3 trigger config
Decide and document:
- Jira status name for story implementation trigger
- issue types supported
- Jira project keys supported
- environment-specific webhook target

Example only — replace with your actual chosen values:
- Story trigger status: `READY FOR DEV`
- Supported issue type: `Story`
- Supported environment for implementation testing: `dev`

### Acceptance criteria
- no code contains vague “Final” assumption
- status names are config-driven or documented explicitly

---

## 8. Local Development and Testing Rules

### Mandatory rule
For anything involving:
- PostgreSQL
- Redis
- worker
- Git
- environment variables

test through Docker Compose, not local Python.

### Reason
This avoids Windows/Python/local dependency drift and keeps the environment consistent with EC2.

### Acceptance criteria
- Phase 3 instructions and docs explicitly say Docker-based testing for integrated features
- Claude does not recommend local host-only testing for integrated workflow paths unless specifically requested

---

## 9. Suggested Debug / Admin Endpoints for Phase 3

You do not need all of these at once, but these are useful:

- `POST /debug/run-story`
- `GET /debug/workflow-runs`
- `GET /debug/workflow-runs/{id}`
- `GET /debug/repo-mappings`
- `POST /debug/repo-mappings`
- `PUT /debug/repo-mappings/{id}`

These make iterative testing much easier.

---

## 10. Suggested Data Model Additions

### `workflow_runs`
Add or confirm:
- `issue_key`
- `repo_slug`
- `base_branch`
- `working_branch`
- `pr_number`
- `pr_url`
- `model_used`
- `current_step`
- `error_message`
- `error_traceback`
- `started_at`
- `completed_at`

### Why
Phase 3 needs real observability, not just binary success/failure.

---

## 11. Safety Rules for Claude in Phase 3

Claude must:
- stay inside workflow workspace
- avoid unrelated refactors
- change the smallest reasonable number of files
- explain what it changed
- fail clearly when uncertain
- prefer no change over unsafe change

Claude must not:
- invent files unless explicitly needed
- rewrite large sections unless the story requires it
- silently fall back to dummy edits in non-debug mode
- assume the target repo is the orchestrator repo

---

## 12. Verification Commands Template

For every iteration, provide exact verification commands.

Examples:

### Health
```bash
curl -s https://dev.orchestrator.suyogjoshi.com/healthz
```

### Trigger webhook
```bash
curl -X POST https://dev.orchestrator.suyogjoshi.com/webhooks/jira \
  -H "Content-Type: application/json" \
  -d @payload.json
```

### View workflow run in DB
```bash
docker exec -it <postgres-container> psql -U <user> -d <db> -c "select * from workflow_runs order by created_at desc limit 5;"
```

### View worker logs
```bash
docker compose logs worker --tail=200
```

### View PR
```bash
gh pr view <number> --json title,body,commits,files
```

### Recreate stack after env change
```bash
docker compose up -d --force-recreate
```

---

## 13. Definition of Done for Phase 3

Phase 3 is complete when all of these are true:

- dev and prod are on separate VMs and separate runners
- repo mappings are configurable, not hardcoded
- workflows target a sandbox repo, not the orchestrator repo
- stronger Claude model is used for implementation
- branch naming is unique per run
- failed workflows end in `FAILED` with useful diagnostics
- bad suggestions do not create fake-success commits
- PRs are meaningful and reviewable
- repeated runs of the same Jira story do not collide
- end-to-end sandbox workflow works reliably in dev

---

## 14. Final Instruction to Claude

Build Phase 3 like an engineer, not like a magician.

Do not optimize for “AI wow factor.”
Optimize for:
- trust
- observability
- reversibility
- controlled behavior

If a feature can either:
- fail clearly, or
- pretend success with a dummy edit

choose fail clearly.

That is how this system becomes usable.

