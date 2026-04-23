# PHASE 4 EXECUTION GUIDE — AI DEV ORCHESTRATOR

## 1. Objective

Phase 4 improves the system from a reliable story-to-PR pipeline into a **more durable, more context-aware, and more production-realistic automation platform**.

In Phase 3, you proved that the orchestrator can:
- operate across isolated dev and prod environments
- resolve repo mappings from the database
- work against a sandbox repo instead of itself
- generate story-aware PRs with Claude
- fail cleanly instead of getting stuck silently

Phase 4 will strengthen the weak spots that showed up during real use:
- repo mappings must be seeded reliably in every environment
- sandbox state must evolve so Claude is not trapped by the same obvious smell forever
- workflow workspaces must be cleaned up
- orphaned `RUNNING` rows must auto-recover on worker startup
- dev/prod mapping drift must be visible
- Claude must get better file context selection
- real Jira webhook integration must be tested end to end
- webhook deduplication / idempotency must be added

This phase is not about adding flashy capabilities.
It is about making the existing system **operationally trustworthy**.

---

## 2. What Phase 4 Is Trying to Achieve (Simple Language)

Phase 4 is about making sure the system behaves like a careful teammate instead of an enthusiastic intern.

Right now, the system can do useful work.
But it still has some rough edges:
- it can drift between environments
- it can leave temporary files behind
- it can repeat the same kind of PR because the sandbox never changes
- it has not yet been proven against a real Jira Cloud workflow
- it can potentially enqueue the same work twice

After Phase 4, the system should prove:
- I can start cleanly after restarts
- I keep my environments aligned
- I clean up after myself
- I can process real Jira events, not just handcrafted curl payloads
- I avoid duplicate work when webhooks repeat
- I choose better files to show Claude, so suggestions improve

In short:

> Phase 4 = make the automation system operationally disciplined

---

## 3. Mandatory Prerequisites Before Writing Phase 4 Code

These are required before feature work begins.

### 3.1 Confirm Phase 3 Baseline Is Stable

Before changing anything, confirm that the current Phase 3 system still works in dev.

### Required checks
- `dev` deployment works
- a sandbox repo mapping exists in dev
- the worker is healthy
- one end-to-end workflow still produces a PR successfully

### Verify

```bash
curl -s https://dev.orchestrator.suyogjoshi.com/healthz
curl -s https://dev.orchestrator.suyogjoshi.com/debug/repo-mappings
```

Then trigger one known-good test story and inspect:
- Telegram notifications
- worker logs
- workflow DB row
- PR in sandbox repo

### Acceptance criteria
- current Phase 3 behavior is intact before Phase 4 changes begin

---

### 3.2 Decide Exact Trigger Configuration Up Front

Do not use example values in code.

Document the actual values to be used in Phase 4 testing:
- Jira project key(s)
- issue type(s)
- story trigger status (for example `READY FOR DEV`)
- dev webhook URL
- prod webhook URL
- sandbox repo slug

### Acceptance criteria
- Phase 4 guide copy handed to Claude contains concrete values or clearly marked placeholders for only the values you have not finalized yet

---

### 3.3 Real Jira Cloud Dev Project Must Exist Before Iteration 6

Phase 4 includes real Jira integration testing.
Create or identify a Jira Cloud project for dev testing before that iteration begins.

### Required setup
- at least one project
- issue type `Story` available
- chosen trigger status available
- workflow transition into that status is configured
- webhook target points to dev environment

### Acceptance criteria
- a real Jira story can be moved manually into the trigger status
- Jira is configured to send a webhook to dev

---

## 4. Working Style for Phase 4

Use the same controlled loop as earlier phases.

### Mandatory loop
1. implement one small feature
2. test locally via Docker if relevant
3. deploy to dev
4. verify with exact commands
5. only then continue

### Additional Phase 4 rule
For every iteration, verify both:
- **positive path**: expected behavior works
- **negative path**: the system rejects bad or duplicate behavior clearly

Examples:
- for mapping seeding: verify mappings exist after deploy, and verify missing mapping still fails cleanly
- for deduplication: verify one webhook is accepted, and repeated webhook does not enqueue duplicate work

---

## 5. Architectural Changes Required in Phase 4

These are the main improvements Phase 4 must deliver.

### 5.1 Repo Mapping Seeding Must Become Part of Deployment

In Phase 3, mappings had to be added manually per environment. That is fragile.

You must make repo mapping availability part of the environment setup / deployment flow.

#### Recommended options
Choose one and implement it explicitly:

**Option A — Seed SQL file**
- maintain seed records in SQL
- apply on DB init or deploy

**Option B — Seed script calling the internal API**
- deploy script invokes `POST /debug/repo-mappings`
- idempotent behavior required

**Option C — App startup reconciliation**
- app loads desired mappings from a config file and inserts/updates them

### Recommendation
Use **Option B** or **Option C**.
They fit the current architecture better than raw SQL.

### Required behavior
- deploy to new environment
- at least one active mapping becomes available automatically
- rerunning deployment does not create duplicates

### Acceptance criteria
- `GET /debug/repo-mappings` returns expected active mappings after deploy
- dev and prod both have the expected minimum mapping set
- seeding is idempotent

### Verify

```bash
curl -s https://dev.orchestrator.suyogjoshi.com/debug/repo-mappings
curl -s https://orchestrator.suyogjoshi.com/debug/repo-mappings
```

Then compare returned mappings.

---

### 5.2 Sandbox State Must Evolve Between Runs

Right now the sandbox repo never improves because PRs are never merged and the same obvious code smell stays in `main` forever.
That causes Claude to keep suggesting the same thing.

Phase 4 must remove that trap.

#### Choose one explicit strategy

**Option A — Periodically merge selected AI PRs**
- after review, merge good PRs into sandbox `main`
- next run sees a slightly improved repo

**Option B — Reset sandbox intentionally between scenarios**
- use curated branches representing different story scenarios
- map stories to different branches or rotate sandbox states

**Option C — Remove the deliberate smell and rely on story context**
- clean up the duplicate import / obvious issue in `main`
- let future suggestions be driven by story intent instead

### Recommendation
Use **Option C first**, then optionally combine with **Option A**.

### Acceptance criteria
- sandbox repo no longer has a permanent obvious smell that dominates every suggestion
- at least two different Jira stories produce meaningfully different PRs

### Verify
Run two distinct stories and compare:
- changed files
- PR summaries
- commit messages

The second run should not just repeat the same cosmetic fix.

---

### 5.3 Workspace Cleanup After Every Run

Every workflow creates `/tmp/workflows/<run_id>/repo` and currently leaves it behind.
That must be fixed.

#### Required behavior
After a workflow ends — whether `COMPLETED` or `FAILED` — remove the workflow directory.

Use cleanup in a `finally` block so it runs for both success and failure.

### Safety rules
- only delete the run-specific workspace
- never delete broader parent directories
- log cleanup success/failure
- if cleanup fails, do not overwrite workflow result status

### Acceptance criteria
- successful runs clean up their workspace
- failed runs also clean up their workspace
- worker logs show cleanup attempt and result

### Verify
Before run:

```bash
ls -la /tmp/workflows
```

Trigger a workflow, wait for finish, then run:

```bash
ls -la /tmp/workflows
```

The completed run directory should not remain.

---

### 5.4 Auto-Recover Orphaned RUNNING Rows on Worker Startup

This must become automatic.

In Phase 3, container restart during deploy could leave rows stuck in `RUNNING`.
Phase 4 must resolve that on worker startup.

#### Required behavior
When the worker starts, before processing new jobs:
1. query for any `workflow_runs` with status `RUNNING`
2. mark them `FAILED`
3. set `error_detail` (or equivalent field) to a clear message such as:
   - `Interrupted by worker restart before completion`
4. log how many rows were recovered
5. optionally send one summary Telegram alert if any were recovered

### Acceptance criteria
- worker startup reconciles stale `RUNNING` rows automatically
- no manual SQL cleanup is needed

### Verify
Create a controlled test:
1. trigger a workflow
2. restart the worker mid-run
3. let worker come back
4. inspect DB row

Expected:
- previous run becomes `FAILED`
- new worker logs startup recovery

---

### 5.5 Mapping Parity Visibility Across Dev and Prod

In Phase 3, dev and prod mappings could silently diverge.
Phase 4 must make parity visible.

#### Minimum requirement
Expose a simple way to verify active mappings per environment.

#### Recommended implementation
Either:
- extend `/healthz` or add `/debug/mapping-health`
- or provide a small script / runbook command that fetches mappings from both environments and compares them

### Recommendation
Add a dedicated lightweight endpoint such as:
- `GET /debug/mapping-health`

Return:
- count of active mappings
- list of active `(jira_project_key, issue_type, repo_slug, base_branch)` entries
- optionally a hash/fingerprint of the active set

### Acceptance criteria
- parity check is easy and fast
- drift is visible without manually querying Postgres

### Verify

```bash
curl -s https://dev.orchestrator.suyogjoshi.com/debug/mapping-health
curl -s https://orchestrator.suyogjoshi.com/debug/mapping-health
```

Compare outputs.

---

### 5.6 Smarter File Selection for Claude Context

Phase 3 only passed a README and a few entry-point files. That is too shallow for real story implementation.

Phase 4 must improve file selection.

#### Goal
Send Claude a more relevant, story-aware subset of files without dumping the entire repo.

#### Recommended selection strategy
Build a ranked candidate list using multiple signals:

1. **Entry points and README**
   - keep existing baseline

2. **Keyword overlap with Jira story**
   - issue summary keywords
   - description keywords
   - acceptance criteria keywords
   - compare against file paths and file contents

3. **Relevant code neighbors**
   - files imported by likely target modules
   - tests related to likely target modules
   - models / schemas / routes near the matched files

4. **Extension-aware priorities**
   - for Python stories, rank `.py` and tests higher
   - for frontend stories, rank `.ts`, `.tsx`, `.js`, `.jsx`, etc. higher

5. **Cap total context deliberately**
   - do not send everything
   - keep a configurable maximum file count and byte budget

### Suggested implementation approach
Start simple:
- score files by keyword overlap in path + content
- include top N relevant files
- always include 1 README if present
- include 1–2 related tests if available

Do not build an elaborate semantic retrieval engine yet.

### Acceptance criteria
- file selection is no longer hardcoded to a tiny fixed set
- story-relevant files are often included
- debug logs or Telegram summary show which files were selected and why

### Verify
Use two different stories targeting different parts of the sandbox repo.
Confirm the selected file list changes accordingly.

---

### 5.7 Real Jira Cloud Integration Test

This is mandatory in Phase 4.
Up to now, tests used handcrafted curl payloads.
That was useful, but no longer enough.

#### Required scope
At least in dev:
- configure a real Jira Cloud webhook
- create or use a real Jira story
- move it into the trigger status manually
- confirm that the actual webhook payload works with the system without hand-editing JSON

### Required checks
Validate that the real Jira payload contains what your code expects:
- `issue.key`
- issue type name
- status change information in changelog
- correct target status string

If the real payload shape differs from assumptions, fix the parser before moving on.

### Acceptance criteria
- at least one end-to-end run is triggered by a real Jira status transition
- no handcrafted curl is used for the final proof
- logs confirm payload handled correctly

### Verify
Use:
- Jira UI transition
- worker logs
- DB row creation
- Telegram
- PR creation

Document the exact steps used so this can be repeated later.

---

### 5.8 Webhook Deduplication / Idempotency

This is one of the most important Phase 4 changes.
Jira may retry webhooks, and the same story could otherwise be enqueued multiple times.

#### Required behavior
Before enqueuing a new workflow run, check whether an equivalent active run already exists.

#### Recommended deduplication key
Start with:
- `issue_key`
- `workflow_type`
- active status in (`QUEUED`, `RUNNING`)

Optional stronger key if available from Jira payload:
- Jira webhook event identifier or changelog identifier

### Required policy
If a duplicate active run exists:
- do not enqueue a new run
- log deduplication event
- optionally send a lightweight Telegram note only in debug mode
- return a normal success response to the webhook caller so Jira does not keep retrying unnecessarily

### Important nuance
Do **not** block legitimate future runs forever.
Only deduplicate when an equivalent run is already active.
A new run after completion or failure should still be allowed.

### Acceptance criteria
- repeated webhook for same active story does not create duplicate workflow rows or duplicate PRs
- once prior run finishes, a new trigger can create a fresh run

### Verify
Test both cases:

**Case A — duplicate while active**
1. trigger story
2. while still `RUNNING`, send same webhook again
3. confirm second one is ignored / deduped

**Case B — new run after completion**
1. allow run to finish
2. trigger same story again
3. confirm a new run is created

---

## 6. Phase 4 Iteration Plan

Follow this order strictly.

---

## Iteration 0 — Phase 4 Baseline and Deployment Hygiene

### Goal
Confirm the current system is healthy and make mapping availability part of environment readiness.

### Tasks
- verify dev and prod health
- verify current mappings in both environments
- implement automatic mapping seeding or explicit seeded deploy step
- document mapping verification in deploy checklist

### Acceptance criteria
- fresh deploy leaves environment with expected minimum mappings
- mapping verification is part of deploy routine

### Verify
- deploy to dev
- call `/debug/repo-mappings`
- confirm seeded mapping exists

Then STOP.

---

## Iteration 1 — Worker Workspace Cleanup

### Goal
Ensure every workflow cleans up its temporary workspace.

### Tasks
- add cleanup in workflow `finally`
- log cleanup result
- ensure cleanup works on both success and failure

### Acceptance criteria
- no leftover `/tmp/workflows/<run_id>` directories after run completes
- cleanup does not delete unrelated directories

### Verify
- inspect `/tmp/workflows` before and after a successful run
- inspect `/tmp/workflows` before and after an induced failure

Then STOP.

---

## Iteration 2 — Orphaned RUNNING Auto-Recovery on Worker Startup

### Goal
Make startup state trustworthy.

### Tasks
- add worker startup reconciliation for stale `RUNNING` rows
- mark them `FAILED`
- store explanatory error detail
- log recovery count
- optionally Telegram summary

### Acceptance criteria
- manual SQL cleanup no longer needed
- stale rows are repaired automatically on startup

### Verify
- trigger workflow
- restart worker mid-run
- inspect DB after restart

Then STOP.

---

## Iteration 3 — Mapping Parity Visibility

### Goal
Make dev/prod mapping drift easy to detect.

### Tasks
- add mapping health endpoint or parity script
- expose active mapping set clearly
- document a quick parity-check command

### Acceptance criteria
- operator can compare dev/prod mapping state quickly
- drift is obvious without DB access

### Verify
- compare outputs from dev and prod
- intentionally create mismatch in dev only and confirm parity check reveals it

Then STOP.

---

## Iteration 4 — Sandbox Evolution Strategy

### Goal
Stop Claude from fixing the same obvious issue forever.

### Tasks
- remove or reduce the dominant deliberate smell in sandbox `main`
- optionally merge one or two good historical PRs
- prepare at least two realistic Jira stories that target different behavior changes

### Acceptance criteria
- repeated runs do not default to the same cosmetic fix
- different stories produce meaningfully different PRs

### Verify
Run two distinct stories and compare:
- selected files
- generated change
- PR summary
- diff

Then STOP.

---

## Iteration 5 — Smarter File Selection for Claude

### Goal
Give Claude better context for story-based implementation.

### Tasks
- replace fixed `_collect_key_files()` approach with scored file selection
- include keyword overlap from issue summary / description / acceptance criteria
- include related tests where possible
- log selected files and selection reason
- keep context bounded

### Acceptance criteria
- selected file set changes based on story intent
- Claude sees more relevant files than just README + entry points
- selection behavior is visible in logs/debug output

### Verify
Use at least two different stories against the sandbox repo and confirm selected files differ meaningfully.

Then STOP.

---

## Iteration 6 — Real Jira Cloud End-to-End Validation

### Goal
Prove the pipeline works from an actual Jira transition, not just a crafted webhook.

### Tasks
- configure real Jira webhook to dev URL
- create or update a real Jira story
- move it into trigger status in Jira UI
- observe end-to-end execution
- fix payload parsing if real payload differs

### Acceptance criteria
- real Jira event triggers workflow successfully
- no manual curl is required for the final proof
- payload assumptions are validated against reality

### Verify
Observe:
- Jira transition time
- app logs
- worker logs
- workflow DB row
- Telegram
- GitHub PR

Then STOP.

---

## Iteration 7 — Webhook Deduplication / Idempotency

### Goal
Prevent duplicate active runs for the same story and workflow.

### Tasks
- add deduplication check before enqueueing
- define active-run rule (`QUEUED` / `RUNNING`)
- log dedupe events clearly
- ensure webhook still returns success response when duplicate is ignored

### Acceptance criteria
- duplicate active webhook does not create extra run
- same story can still create a new run after previous one finishes

### Verify
Test both:
- duplicate while run active
- new run after prior completion

Then STOP.

---

## Iteration 8 — End-to-End Phase 4 Validation

### Goal
Validate that the system is now more durable and production-realistic.

### Flow to validate
1. environment deploy seeds mappings
2. worker starts and reconciles stale runs
3. real Jira story triggers workflow
4. repo mapping resolves correctly
5. smarter file selection picks story-relevant files
6. Claude generates change
7. workflow cleans up workspace afterward
8. duplicate webhook while active is ignored
9. final PR is reviewable
10. dev/prod mapping parity can be checked quickly

### Acceptance criteria
- no manual cleanup required after normal runs
- no stale `RUNNING` rows survive worker restart
- no duplicate active run gets created
- real Jira integration is proven
- context selection improves relevance
- operational trust is higher than Phase 3

### Verify checklist
Run all of these:
- health checks
- mapping list / parity check
- real Jira transition
- workflow DB inspection
- worker logs
- `/tmp/workflows` directory inspection
- PR inspection via GitHub CLI or UI
- duplicate webhook test

Then STOP and review before planning Phase 5.

---

## 7. Suggested Debug / Admin Enhancements for Phase 4

These are optional but recommended if they make testing easier.

### Suggested endpoints or utilities
- `GET /debug/mapping-health`
- `GET /debug/workflow-runs`
- `GET /debug/workflow-runs/{id}`
- `POST /debug/run-story` for internal dry runs without Jira
- small script to compare mappings across dev/prod

These are not the main feature of Phase 4, but they reduce friction.

---

## 8. Suggested Data / State Improvements

### `workflow_runs`
If not already present, consider confirming or adding:
- `current_step`
- `started_at`
- `completed_at`
- `error_detail`
- `working_branch`
- `pr_url`

### Why
Phase 4 is about operational trust. Better visibility helps debugging and makes failure analysis quicker.

---

## 9. Safety Rules for Claude in Phase 4

Claude must:
- preserve the fail-clearly philosophy from Phase 3
- avoid hidden side effects
- keep cleanup scoped to the run-specific workspace only
- treat real Jira as the source of truth during real integration testing
- keep deduplication rules explicit and inspectable
- explain file selection decisions in logs or debug output

Claude must not:
- silently create duplicate workflow runs
- delete broad temporary directories carelessly
- assume dev and prod have the same DB contents unless checked
- rely only on handcrafted webhook payloads once real Jira integration is available
- reintroduce dummy-success behavior that hides genuine failures

---

## 10. Verification Commands Template

Use exact commands wherever possible.

### Health
```bash
curl -s https://dev.orchestrator.suyogjoshi.com/healthz
curl -s https://orchestrator.suyogjoshi.com/healthz
```

### Repo mappings
```bash
curl -s https://dev.orchestrator.suyogjoshi.com/debug/repo-mappings
curl -s https://orchestrator.suyogjoshi.com/debug/repo-mappings
```

### Mapping health / parity
```bash
curl -s https://dev.orchestrator.suyogjoshi.com/debug/mapping-health
curl -s https://orchestrator.suyogjoshi.com/debug/mapping-health
```

### Worker logs
```bash
docker compose logs worker --tail=200
```

### Workflow DB inspection
```bash
docker exec -it <postgres-container> psql -U <user> -d <db> -c "select id, issue_key, status, error_detail, created_at from workflow_runs order by created_at desc limit 10;"
```

### Temp workspace inspection
```bash
ls -la /tmp/workflows
```

### PR inspection
```bash
gh pr view <number> --json title,body,commits,files
```

### Force recreate after env changes
```bash
docker compose up -d --force-recreate
```

---

## 11. Definition of Done for Phase 4

Phase 4 is complete when all of these are true:
- repo mappings are seeded automatically or through a documented idempotent deploy step
- dev and prod minimum mappings can be verified quickly
- workspace directories are cleaned after both success and failure
- worker startup auto-recovers stale `RUNNING` rows
- sandbox state no longer traps Claude into the same repetitive fix every run
- file selection for Claude is smarter and story-aware
- at least one full run is triggered by a real Jira Cloud status transition
- duplicate active webhook deliveries do not create duplicate workflow runs
- operational trust is improved compared to Phase 3

---

## 12. Final Instruction to Claude

Build Phase 4 like an operations-minded engineer.

This phase is not about giving the system more power.
It is about making the existing power safer, cleaner, and more realistic.

Prefer:
- explicit deploy hygiene
- visible parity checks
- bounded cleanup
- real integration validation
- deduplication with clear rules

If a choice exists between:
- clever hidden behavior, or
- obvious, inspectable behavior

choose obvious, inspectable behavior.

That is what makes automation dependable.
