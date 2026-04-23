
# PHASE 5 EXECUTION GUIDE — AI DEV ORCHESTRATOR

## 1. Objective

Phase 5 upgrades the AI Dev Orchestrator from a system that can generate code changes and open reviewable PRs into a system that can **validate its own work** and handle **real feature-sized changes**.

In Phase 1, the system learned to receive and orchestrate workflows.

In Phase 2, it learned to clone repos, modify code, commit, push, and open PRs.

In Phase 3, it became safer and more reviewable.

In Phase 4, it became more operationally durable and better integrated with real Jira and better file selection.

Phase 5 is about the next leap:

- generate tests or identify existing tests
- run tests automatically
- understand failures
- attempt controlled fixes
- support multi-file changes for real stories
- optionally auto-merge PRs only when strict conditions are met

This is the phase where the system starts moving from:

> “AI-assisted code generator”

to:

> “AI-assisted task completer”

---

## 2. What Phase 5 Is Trying to Achieve (Simple Language)

Right now the system can produce code changes.

But it still cannot confidently answer:

- Did the code actually work?
- Did the change break something else?
- Can this PR be safely merged?
- Can the system handle a real change that spans more than one file?

Phase 5 answers those questions.

The core target is this loop:

```text
Story arrives
→ Claude proposes implementation
→ system applies code changes
→ system runs tests
→ if tests fail, Claude sees the failure
→ Claude attempts a fix
→ tests re-run
→ if tests pass, PR is created
→ optionally merge automatically under strict rules
```

That is the minimum viable closed-loop engineering workflow.

---

## 3. Scope of Phase 5

### In Scope

1. Test discovery
2. Optional test generation
3. Test execution
4. Failure parsing
5. Retry / fix loop
6. Multi-file suggestion + apply pipeline
7. PR merge policy (controlled, optional)
8. Better observability for workflow runs and test runs
9. Debug/admin endpoints for workflow inspection

### Explicitly Out of Scope

Do NOT build these in Phase 5:

- Epic → Feature → Story planning intelligence
- full multi-agent orchestration
- long-term memory / learning engine
- production auto-deploy of generated code
- unbounded retry loops
- broad external knowledge ingestion
- autonomous refactoring across entire repo

These are valuable, but not Phase 5.

---

## 4. Phase 5 Design Principles

### 4.1 Correctness before cleverness

If a change cannot be validated, it should not be treated as success.

### 4.2 Fail clearly, not optimistically

Do not create a “successful” PR when:
- tests fail
- only partial changes were applied
- the model could not implement the story reliably

### 4.3 Small bounded loops

Retry count must be bounded.
Recommended initial limit:
- max 1 implementation attempt
- max 1 fix attempt after test failure

Total max model-assisted coding passes per workflow:
- 2

### 4.4 Multi-file support must stay controlled

Do not allow “edit everything.”
Every file change should be:
- explicitly named
- validated
- logged
- reviewable

### 4.5 Merge only when rules are strict

Auto-merge should be optional and conservative, never default-on for all repos.

---

## 5. New Capabilities to Add

Phase 5 adds five major capabilities.

### 5.1 Test Discovery and Execution

The system should:
- detect whether the repo has tests
- identify the test framework
- run a safe command
- capture stdout/stderr/exit code
- persist results

### 5.2 Fix Loop

If tests fail after implementation:
- summarize failure
- send minimal relevant code + failure output to Claude
- ask for a focused fix
- apply fix
- rerun tests

### 5.3 Multi-File Change Support

Allow Claude to return multiple edits in one workflow.
Example:
- modify API layer
- modify model
- modify storage
- optionally modify tests

### 5.4 Merge Policy

Optionally auto-merge PRs when:
- tests pass
- workflow status is healthy
- branch protection allows it
- repo is marked as eligible

### 5.5 Better Observability

Add endpoints and DB fields so workflow state, test results, branches, PRs, and failures are inspectable without SSHing into Postgres every time.

---

## 6. Mandatory Prerequisites Before Writing Phase 5 Code

These must be done before feature iterations begin.

### 6.1 Deploy Phase 4 to Prod and Validate

Your Phase 4 feedback explicitly noted that prod was not revalidated after Phase 4 changes. Phase 5 should not start until both environments run the same durable baseline. fileciteturn7file0

#### Tasks
- merge/deploy Phase 4 code to `main`
- confirm prod picks up:
  - config seeding
  - dedup
  - issue_key column changes
  - mapping health endpoint
- verify at least one end-to-end prod-safe dry run or controlled run

#### Acceptance criteria
- prod health check passes
- prod mapping health works
- prod DB schema includes Phase 4 columns
- prod logs show worker startup recovery check
- prod is not still on Phase 3 behavior

---

### 6.2 Decide Phase 5 Sandbox Policy

Phase 4 revealed that PRs accumulate and `main` never advances, which limits realism. fileciteturn7file0

Before Phase 5, choose one of these:

#### Option A — Controlled merge-forward sandbox (recommended)
- sandbox repo remains long-lived
- approved PRs are merged between sessions
- next runs start from a gradually improving codebase

#### Option B — Rotating sandbox repo
- periodically reset or rotate to a fresh repo

#### Option C — Ephemeral branch-only testing
- workflow validates changes, but PRs are closed/reset frequently

### Recommendation
Choose **Option A**.

It keeps the repo realistic without overcomplicating environment management.

### Acceptance criteria
- chosen policy is documented in `CLAUDE.md` or runbook
- Claude knows whether successful sandbox PRs are expected to merge

---

### 6.3 Telegram Environment Prefixing

Phase 4 feedback noted that dev and prod share the same Telegram chat and messages are indistinguishable. fileciteturn7file0

#### Required change
Add environment-specific prefixing:
- `[DEV]`
- `[PROD]`

Use an env variable:
- `ENV_NAME=DEV`
- `ENV_NAME=PROD`

Prepend it to all Telegram event titles/messages.

#### Acceptance criteria
- every Telegram message clearly indicates environment
- duplicate confusion is eliminated

---

### 6.4 Add Workflow Inspection Endpoints

Phase 4 feedback noted that inspecting workflow runs still requires SSH + SQL. fileciteturn7file0

Before deeper Phase 5 work, expose:
- `GET /debug/workflow-runs?limit=N`
- `GET /debug/workflow-runs/{id}`

### Acceptance criteria
- recent runs are visible via HTTP
- run details include status, current_step, issue_key, working_branch, PR URL, and error fields if present

---

## 7. Data Model Changes for Phase 5

You should extend the database deliberately before building logic around it.

### 7.1 Extend `workflow_runs`

Add fields if not already present:

- `test_status` (nullable; values like NOT_RUN, PASSED, FAILED)
- `test_command` (nullable)
- `test_output` (nullable, truncated if needed)
- `retry_count` (default 0)
- `files_changed_count` (nullable)
- `merge_status` (nullable; NOT_ATTEMPTED, MERGED, SKIPPED, FAILED)
- `merged_at` (nullable)

### 7.2 New table: `workflow_attempts` (recommended)

Tracks each implementation/test/fix cycle.

Suggested columns:
- `id`
- `run_id`
- `attempt_number`
- `attempt_type` (`implement`, `fix`)
- `model_used`
- `status`
- `started_at`
- `completed_at`
- `failure_summary`
- `test_status`
- `files_touched`

Why:
- makes debugging much easier
- keeps retry history separate from final run record

### 7.3 Optional table: `test_runs`

Use this if you want more detailed test history.

Suggested columns:
- `id`
- `run_id`
- `attempt_id`
- `command`
- `exit_code`
- `status`
- `output`
- `started_at`
- `completed_at`

If this feels heavy, store test data on `workflow_runs` first and defer this table.

---

## 8. Architectural Changes Required in Phase 5

### 8.1 Add Test Strategy Layer

After code changes are applied but before PR finalization, the workflow should decide:

1. Are tests present?
2. Which command should run?
3. Is this repo eligible for auto-test execution?

### Initial safe strategy

Support Python repos first.

Detect:
- `pytest.ini`
- `pyproject.toml`
- `requirements.txt`
- `tests/`
- `conftest.py`

Recommended initial command:
```bash
pytest -q
```

If repo is clearly not Python or test framework is unclear:
- mark test status as `NOT_RUN`
- do not fake success
- PR should clearly state “tests not run”

### Acceptance criteria
- system can discover basic pytest-based repos
- test command is explicit and logged
- unsupported repos fail safely or skip transparently

---

### 8.2 Add Multi-File Suggestion Contract

Current system is effectively one suggestion / one file. Phase 5 must expand this.

#### New suggestion contract

Claude should return structured JSON like:

```json
{
  "summary": "Implement item count in list response and add test coverage",
  "changes": [
    {
      "path": "app/main.py",
      "description": "Add count field to list response",
      "original": "...",
      "replacement": "..."
    },
    {
      "path": "app/models.py",
      "description": "Add response model update",
      "original": "...",
      "replacement": "..."
    },
    {
      "path": "tests/test_items.py",
      "description": "Add test for count field",
      "original": "...",
      "replacement": "..."
    }
  ]
}
```

#### Rules
- keep max changed files small at first:
  - recommended max = 3
- all changes must pass per-file validation
- if one change fails, choose one of two policies:

##### Recommended initial policy
Fail the workflow rather than partially applying a multi-file set.

This avoids hidden inconsistency.

### Acceptance criteria
- Claude can return multiple file changes
- system validates and applies each change deterministically
- partial silent success is not allowed

---

### 8.3 Add Fix Loop

This is the heart of Phase 5.

#### Required behavior

After implementation:
1. run tests
2. if tests pass → continue
3. if tests fail:
   - capture output
   - summarize failure
   - ask Claude for focused fix
   - apply fix
   - rerun tests
4. if tests still fail:
   - mark workflow failed or PR-as-draft depending on policy

#### Recommended initial policy
If tests still fail after 1 fix attempt:
- mark workflow `FAILED`
- do not auto-merge
- optionally do not open PR unless explicitly configured

This is stricter, but safer.

### Acceptance criteria
- failed tests trigger exactly one fix attempt
- second failure ends workflow clearly
- all steps are logged in DB and Telegram

---

### 8.4 Add Merge Policy Layer

This should be optional and configuration-driven.

#### Per-repo merge eligibility

Extend `repo_mappings` or config with something like:
- `auto_merge_enabled` (boolean)

#### Auto-merge allowed only if:
- tests passed
- run status is healthy
- PR created successfully
- repo mapping allows auto-merge
- branch protection permits it
- PR is not draft
- changed files count is within safe threshold

#### Recommended threshold
- max files changed for auto-merge: 2 or 3
- no auto-merge when tests were skipped
- no auto-merge on first ever run for a repo

### Acceptance criteria
- auto-merge never happens by accident
- skipped/failed tests block merge
- merge decision is logged and visible

---

## 9. Phase 5 Iteration Plan

Follow this order strictly.

---

## Iteration 0 — Phase 5 Prerequisite Stabilization

### Goal
Start Phase 5 from a clean and trustworthy baseline.

### Tasks
- deploy Phase 4 to prod
- validate prod parity with dev
- add Telegram environment prefixes
- add workflow run inspection endpoints
- confirm sandbox merge policy
- confirm seed mappings are active on both environments

### Acceptance criteria
- dev and prod show same mapping fingerprint where expected
- Telegram messages are environment-distinct
- workflow runs can be inspected via HTTP
- sandbox strategy is documented

### Verify
- `GET /debug/mapping-health`
- `GET /debug/workflow-runs`
- one Telegram event from each environment
- prod health and logs

Then STOP.

---

## Iteration 1 — Test Discovery and Basic Test Execution

### Goal
Teach the system to find and run tests for supported repos.

### Tasks
- implement Python test discovery
- determine test command
- run test command after code changes
- capture exit code + output
- update workflow state with test results

### Out of scope
- no fix loop yet
- no multi-file changes yet
- no auto-merge yet

### Acceptance criteria
- repo with pytest tests is detected
- `pytest -q` runs in workflow workspace
- output is captured and persisted
- Telegram includes test status
- unsupported repo shows transparent `NOT_RUN`

### Verify
Use sandbox repo with:
- one passing test
- one intentionally failing test scenario

Run workflow and inspect:
- logs
- DB
- Telegram
- stored test command/output

Then STOP.

---

## Iteration 2 — Better Sandbox Test Fixture

### Goal
Make the sandbox realistic enough for Phase 5.

### Tasks
- add/confirm actual tests in sandbox repo
- ensure stories can cause:
  - passing implementation
  - failing implementation
- keep sandbox simple but not toy-like

### Recommended sandbox shape
- small FastAPI repo
- at least 3–5 tests
- one story can require:
  - API update
  - model update
  - test update

### Acceptance criteria
- sandbox repo contains meaningful executable tests
- at least one Jira story can be validated end-to-end using those tests

### Verify
Manually run:
```bash
pytest -q
```
inside sandbox repo locally/in container before involving orchestrator

Then STOP.

---

## Iteration 3 — Fix Loop (Single Retry)

### Goal
Allow one controlled fix attempt after test failure.

### Tasks
- parse test failure output
- create focused fix prompt for Claude
- record attempt history
- apply fix
- rerun tests
- stop after one retry

### Prompt rule
The fix prompt must include:
- original story summary
- changed files
- failing test output (trimmed)
- instruction to minimize scope

### Acceptance criteria
- failed implementation triggers fix attempt
- one retry only
- second failure ends workflow clearly
- Telegram and DB reflect attempt count and final state

### Verify
Create a story that causes:
- initial failing implementation
- fixable failure

Then create one that remains broken after one retry.

Check both paths.

Then STOP.

---

## Iteration 4 — Multi-File Suggestion Contract

### Goal
Move from single-file edits to small, controlled multi-file changes.

### Tasks
- update Claude contract to return list of file changes
- update apply pipeline to iterate through changes
- validate all changes before writing
- enforce max changed files
- log file list and count

### Recommended initial constraints
- max files = 3
- fail entire run if any change cannot be validated
- no partial commit in initial version

### Acceptance criteria
- multi-file stories are supported
- all file changes are explicit and reviewable
- validation prevents inconsistent partial writes

### Verify
Use a story that requires:
- API file change
- model or storage change
- test file change

Then inspect:
- changed file list
- diff
- PR body
- files_changed_count

Then STOP.

---

## Iteration 5 — Better Context Selection for Multi-File Work

### Goal
Improve relevance of files sent to Claude for implementation.

Phase 4 improved keyword scoring, but feedback noted it still stops at repo surface and does not follow imports. fileciteturn7file0

### Tasks
- keep keyword scoring
- add lightweight import traversal for Python repos
- include direct dependencies of top-ranked files
- avoid exploding context size
- log why each selected file was included:
  - keyword
  - entry-point
  - import dependency
  - test relevance

### Recommended initial rule
- select top 2 scored files
- add up to 2 direct dependency/import-related files
- plus 1 test file if relevant

### Acceptance criteria
- context selection is more relevant than pure keyword-only
- related utility/model files can be included
- selection reasons are logged and inspectable

### Verify
Use a story where:
- top file imports another file that also needs change

Confirm second file is included because of import traversal, not just keyword chance.

Then STOP.

---

## Iteration 6 — PR Lifecycle Strategy

### Goal
Prevent endless accumulation of open sandbox PRs.

### Tasks
Choose and implement one strategy.

#### Recommended strategy
Controlled auto-merge for sandbox only, under strict conditions.

Conditions:
- tests passed
- PR created successfully
- repo mapping enables auto-merge
- files_changed_count <= configured threshold
- not a fix-loop failure
- no skipped tests
- repo is explicitly marked sandbox-safe

### Alternative strategy
Leave PR open but add runbook step to merge approved ones manually.

### Acceptance criteria
- sandbox repo does not accumulate infinite stale PRs
- merge policy is explicit and logged
- risky repos are excluded

### Verify
Run one passing story on sandbox and confirm:
- PR auto-merges or is clearly ready for manual merge depending on chosen policy

Then STOP.

---

## Iteration 7 — Workflow Run API Expansion

### Goal
Make operations and debugging self-service.

### Tasks
Add:
- `GET /debug/workflow-runs?limit=N`
- `GET /debug/workflow-runs/{id}`
- include:
  - status
  - current_step
  - issue_key
  - working_branch
  - PR URL
  - test_status
  - retry_count
  - merge_status
  - error summary

Optional:
- `GET /debug/test-runs/{run_id}` if you create a test_runs table

### Acceptance criteria
- no SSH needed for basic run inspection
- recent runs are understandable from API output alone

### Verify
Run a successful workflow and a failed workflow, then compare endpoint output.

Then STOP.

---

## Iteration 8 — Recovery Path Under Real Failure

### Goal
Prove stale-run recovery actually works under realistic interruption.

Phase 4 feedback specifically called out that startup recovery under real conditions was not yet explicitly tested. fileciteturn7file0

### Tasks
- start a workflow
- intentionally kill/recreate worker mid-run
- restart worker
- verify stale `RUNNING` transitions to `FAILED`
- ensure Telegram and DB reflect restart interruption

### Acceptance criteria
- interrupted run does not remain `RUNNING`
- recovered failure reason is visible
- worker resumes normal operation afterwards

### Verify
Check:
- worker logs
- DB state
- Telegram alert
- follow-up workflow still works

Then STOP.

---

## Iteration 9 — End-to-End Phase 5 Validation

### Goal
Validate the full closed-loop workflow.

### Full flow
1. real Jira story triggers workflow
2. repo mapping resolved
3. repo cloned
4. multi-file context selected
5. Claude proposes implementation
6. changes validated and applied
7. tests discovered and run
8. if failure:
   - one fix attempt
   - tests rerun
9. if success:
   - commit
   - push
   - PR created
   - optional merge policy evaluated
10. workflow state finalized with full observability

### Required scenarios
You must test all of these:

#### Scenario A — straightforward success
- implementation passes tests first time

#### Scenario B — fix-loop success
- first implementation fails
- one fix attempt succeeds

#### Scenario C — hard failure
- implementation fails
- fix attempt fails
- workflow ends clearly in FAILED

#### Scenario D — skipped tests
- unsupported or unconfigured repo
- PR shows transparent “tests not run”
- no merge

### Acceptance criteria
- all four scenarios behave predictably
- no silent fake success
- DB, Telegram, PR, and APIs all agree on outcome

Then STOP and review before Phase 6.

---

## 10. Trigger Definitions Must Stay Explicit

As with Phase 3 and 4, do not use vague examples in code.

Document exact values:
- Jira status that triggers implementation
- supported issue types
- which repos are test-enabled
- which repos are merge-enabled
- environment webhook URLs

Example placeholders only:
- Story trigger status: `READY FOR DEV`
- supported issue type: `Story`
- test-enabled repo: `suyog19/sandbox-fastapi-app`
- auto-merge-enabled: false by default, true only for sandbox

Replace with actual values before execution.

---

## 11. Recommended Config Additions

Add to config or repo mappings as appropriate:

- `supports_tests`
- `test_command`
- `auto_merge_enabled`
- `max_changed_files`
- `max_fix_attempts`
- `environment_name`

If you do not want to store all of these in DB yet, keep some in a repo-level config file first.

---

## 12. Telegram Message Enhancements for Phase 5

Now that testing and retrying exist, Telegram must become more informative.

Add events such as:
- `[DEV] tests_started`
- `[DEV] tests_passed`
- `[DEV] tests_failed`
- `[DEV] fix_attempt_started`
- `[DEV] fix_attempt_failed`
- `[DEV] pr_merge_skipped`
- `[DEV] pr_merged`

Keep messages short but explicit.

---

## 13. Verification Commands Template

For each iteration, Claude should provide exact commands.

Examples:

### Health
```bash
curl -s https://dev.orchestrator.suyogjoshi.com/healthz
```

### Mapping health
```bash
curl -s https://dev.orchestrator.suyogjoshi.com/debug/mapping-health
```

### List workflow runs
```bash
curl -s "https://dev.orchestrator.suyogjoshi.com/debug/workflow-runs?limit=5"
```

### Trigger webhook
```bash
curl -X POST https://dev.orchestrator.suyogjoshi.com/webhooks/jira \
  -H "Content-Type: application/json" \
  -d @payload.json
```

### Worker logs
```bash
docker compose logs worker --tail=200
```

### Inspect PR
```bash
gh pr view <number> --json title,body,commits,files,state
```

### Recreate stack after env change
```bash
docker compose up -d --force-recreate
```

### Manual test run inside workspace repo
```bash
pytest -q
```

---

## 14. Definition of Done for Phase 5

Phase 5 is complete when all of these are true:

- Phase 4 baseline is deployed and validated in prod
- Telegram messages are environment-prefixed
- workflow runs can be inspected via HTTP APIs
- supported repos can discover and run tests
- test output is persisted and visible
- one bounded fix loop exists
- multi-file changes are supported safely
- context selection includes lightweight dependency/import awareness
- sandbox PR lifecycle no longer accumulates endless open PRs
- restart recovery is tested under real interruption
- end-to-end closed-loop validation works for success, fix-success, failure, and skipped-test scenarios

---

## 15. Final Instruction to Claude

Build Phase 5 like a reliability-minded engineer.

Do not optimize for:
- larger code diffs
- more “creative” AI behavior
- more retries

Optimize for:
- correctness
- bounded loops
- explicit state
- reviewable outcomes
- safe failure

The key question for Phase 5 is no longer:

> “Can the system make a code change?”

It is:

> “Can the system determine whether the change is good enough to trust?”

That is the standard now.
