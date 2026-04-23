# Phase 3 Summary and Feedback — AI Dev Orchestrator

## Part 1: Phase 3 Summary

### Goal

Harden the Phase 2 pipeline from a single-environment prototype into a production-grade,
dual-environment system. Specific targets: replace hardcoded repo mappings with a
DB-driven CRUD API; introduce a real sandbox target repo separate from the orchestrator
itself; add failure observability, unique branch naming, a stronger Claude model,
story-informed prompting, pre-apply code validation, and rich PR metadata.

---

### What Was Built

**New / significantly changed files:**

| File | Change |
|---|---|
| `app/repo_mapping.py` | Full rewrite — DB-driven CRUD with most-specific-match resolution |
| `app/database.py` | Schema migrations for `repo_mappings` (new columns) and `workflow_runs` (`error_detail`); `fail_run()` helper |
| `app/worker.py` | `try/except` around workflow handler; calls `fail_run()` and sends Telegram alert on error |
| `app/git_ops.py` | Branch name changed from `ai/issue-<key>` to `ai/<key>/<run_id>`; `commit_and_push` accepts `run_id` |
| `app/claude_client.py` | Both calls upgraded from `claude-haiku-4-5` to `claude-sonnet-4-6`; `suggest_change` accepts `issue_key` and `issue_summary` |
| `app/file_modifier.py` | Three pre-apply validation gates: path traversal guard, no-op guard, `ast.parse` syntax check |
| `app/github_api.py` | `ensure_label()` and `add_label_to_pr()` for the `ai-generated` label |
| `app/workflows.py` | Story context threaded into suggestion call; unified diff via `difflib`; validation checklist + review checklist in PR body; label applied after creation |
| `app/main.py` | Full CRUD for repo mappings: GET / POST / PUT / DELETE `/debug/repo-mappings` |
| `.github/workflows/deploy-dev.yml` | `runs-on: self-hosted-dev` |
| `.github/workflows/deploy-main.yml` | `runs-on: self-hosted-prod` |
| `CLAUDE.md` | Added Environment Model table and Two-File `.env` Rule section |

**New GitHub repo:** `suyog19/sandbox-fastapi-app` — a minimal FastAPI CRUD app used as
the pipeline's target repo (not the orchestrator itself).

---

### Full Pipeline (Phase 3)

```
Jira issue moved to "READY FOR DEV" (Story type)
  -> POST /webhooks/jira
      -> changelog.items filter (status change only)
      -> DB: INSERT workflow_events (received)
      -> Telegram: event notification
      -> dispatcher: (issue_type="Story", status="READY FOR DEV") -> story_implementation
      -> DB: INSERT workflow_runs (QUEUED)
      -> Redis: enqueue {run_id, workflow_type, issue_key, issue_type, summary}
  -> worker: dequeue -> spawn thread
      -> DB: UPDATE workflow_runs (RUNNING)
      -> Telegram: workflow / RUNNING
      [try]
      -> get_mapping(jira_project_key, issue_type)
          WHERE jira_project_key=? AND (issue_type=? OR issue_type IS NULL)
          ORDER BY issue_type NULLS LAST  <- specific beats catch-all
      -> git clone --depth=1 + create branch ai/<key>/<run_id>
      -> Telegram: repo_analysis / COMPLETE
      -> Claude Sonnet 4.6: summarize_repo()
      -> Telegram: claude_summary / COMPLETE
      -> Claude Sonnet 4.6: suggest_change(repo, analysis, issue_key, issue_summary)
          story context in user turn (system prompt stays cached)
      -> Telegram: claude_suggestion / COMPLETE
      -> apply_suggestion() with pre-apply validation gates:
          1. required fields present
          2. path traversal guard (realpath check)
          3. file exists
          4. original text found in file
          5. no-op guard (original != replacement)
          6. ast.parse() syntax check for .py files
          -> if any gate fails -> applied=False -> fallback to modify_file()
      -> Telegram: file_apply / COMPLETE
      -> git add -A -> git commit (message = suggestion description)
      -> git push origin ai/<key>/<run_id>
      -> Telegram: git_push / COMPLETE
      -> ensure_label(repo, "ai-generated")  <- idempotent, no-ops if label exists
      -> GitHub API: create PR
          body: story context, Claude summary, unified diff (difflib),
                validation checklist, review checklist
      -> add_label_to_pr(repo, pr_number, "ai-generated")
      -> Telegram: pr_created / COMPLETE
      -> DB: UPDATE workflow_runs (COMPLETED)
      -> Telegram: workflow / COMPLETED
      [except]
      -> DB: UPDATE workflow_runs (FAILED, error_detail=traceback[:2000])
      -> Telegram: workflow / FAILED (error type + message)
```

**Total Telegram notifications per successful run: 8**
**On failure: 3 (RUNNING notification, FAILED alert, no further steps)**

---

### Iteration Completion Status

| # | Description | Status |
|---|---|---|
| 0 | Dev/prod VM isolation — two EC2 instances, two runners, split domains | Done |
| 1 | Real repo mapping system — DB-driven CRUD API, most-specific-match resolution | Done |
| 2 | Sandbox target repo — `suyog19/sandbox-fastapi-app` with deliberate code smells | Done |
| 3 | Workflow failure handling — try/except, FAILED status, error_detail in DB, Telegram alert | Done |
| 4 | Unique branch naming — `ai/<issue-key>/<run-id>` | Done |
| 5 | Stronger Claude model — both calls upgraded to `claude-sonnet-4-6` | Done |
| 6 | Story-to-implementation prompting — issue key + summary passed to suggest_change | Done |
| 7 | Pre-apply validation — path traversal, no-op guard, ast.parse syntax check | Done |
| 8 | PR quality and review metadata — unified diff, validation checklist, review checklist, label | Done |
| 9 | End-to-end validation — verified on dev and prod against sandbox repo | Done |

---

### Key Technical Decisions

**1. DB-driven repo mapping with most-specific-match resolution.**
The `repo_mappings` table supports a `NULL` `issue_type` as a catch-all. Lookups use
`ORDER BY issue_type NULLS LAST LIMIT 1` so an exact `(project, issue_type)` match always
beats a `(project, NULL)` catch-all. This allows one fallback mapping per Jira project while
still routing specific issue types (Bug, Story, Epic) to different repos.

**2. Branch naming includes run_id.**
`ai/<issue-key>/<run-id>` guarantees uniqueness across retries of the same issue. The old
`ai/issue-<key>` format caused force-push collisions when the same story was re-triggered
(e.g. re-opened and moved back to Ready for Dev).

**3. Story context in the user turn, not the system prompt.**
`SUGGEST_PROMPT` (the system prompt) stays static so it benefits from prompt caching.
The `issue_key` and `issue_summary` go into the user message, which varies per request.
This keeps caching intact while making Claude suggestions story-aware.

**4. Pre-apply validation runs entirely in memory before any write.**
All six validation checks complete before `open(abs_path, "w")` is called. If the syntax
check (`ast.parse`) raises `SyntaxError` on the modified content, the file on disk is never
touched — the workflow falls back to `modify_file()` and still produces a commit.

**5. Labels are created idempotently via a POST that accepts 422.**
`ensure_label()` posts to `/repos/{slug}/labels` and treats HTTP 422 (already exists) as
success. The first run on any new repo auto-creates the label; all subsequent runs are
silent no-ops. No pre-check or conditional logic needed.

**6. Two-file `.env` pattern documented and enforced.**
`/home/ubuntu/.env.orchestrator` is the persistent secret store on each VM. The GitHub
Actions deploy step copies it to the project directory `.env` before `docker compose up`.
Containers read from the project `.env` via `env_file:`. Manual credential updates require
updating `.env.orchestrator` and then running `docker compose up -d --force-recreate`.

**7. Separate EC2 VMs for dev and prod.**
Dev: `65.2.140.4` (`dev.orchestrator.suyogjoshi.com`, runner `self-hosted-dev`, branch `dev`).
Prod: `13.234.33.241` (`orchestrator.suyogjoshi.com`, runner `self-hosted-prod`, branch `main`).
The `runs-on:` label in each workflow file routes deploys to the correct machine with no
overlap possible.

---

### Problems Encountered and Solutions

**1. Orphaned RUNNING runs after container restarts during deploys.**

- **Root cause:** GitHub Actions deploys recreate the worker container mid-flight. If a
  workflow thread was running inside the old container, it was killed but the DB row stayed
  at `RUNNING`. The new container never sees that job — it was already dequeued from Redis.
- **Fix:** One-time cleanup query in Iteration 9 to set all lingering `RUNNING` rows to
  `FAILED` with `error_detail = "Worker restarted mid-run (container recreate during deploy)"`.
  A future improvement: run this automatically on worker startup before the dequeue loop.

**2. Prod database empty — mapping not seeded automatically.**

- **Root cause:** The repo mapping added to dev via the API is stored in the dev Postgres
  instance. The prod VM has its own isolated Postgres with an empty `repo_mappings` table
  after the first deploy.
- **Fix:** Added the SANDBOX mapping manually via `curl` against the prod endpoint after
  the first prod deploy.

**3. Worker readiness race condition on fresh deploys.**

- **Root cause:** The health check (`/healthz`) is served by the FastAPI app container,
  which starts faster than the worker. A webhook fired immediately after the health check
  passes may be enqueued to Redis before the worker dequeue loop has started.
- **Observed symptom:** Webhook returned `{"received":true,"processed":true}` but the worker
  log showed "Worker started" with no subsequent job pickup for that run.
- **Fix for testing:** Re-fired the webhook a few seconds later. Not an issue in production
  since real Jira events are triggered by human action, not automated bursts.

**4. Claude ignores story context when an obvious code smell is present.**

- **Observation:** Despite stories like "Return item count in list response", Claude Sonnet
  still frequently suggested removing the duplicate `from fastapi import FastAPI` import —
  the most visible issue in the file regardless of story content.
- **Root cause:** The sandbox repo was seeded with a deliberate duplicate import as a target.
  Because PRs are never merged back to `main`, every clone sees the same file with the same
  obvious problem, which outweighs the story signal.
- **Implication:** The sandbox repo produces correct, cleanly applied PRs, but the underlying
  file never permanently improves across sessions.

---

## Part 2: Feedback on Phase 3 Instructions

### Strengths

**1. The execution guide as a committed file was the right format.**
Having `phase_3_execution_guide.md` checked into the repo meant it was available throughout
the session without relying on context memory. Each iteration having an explicit goal, flow,
and acceptance criteria made the work tractable. No iteration required re-scoping mid-way.

**2. Trigger definitions being explicit prevented the Phase 2 confusion.**
Documenting `READY FOR DEV` as the exact status string avoided any ambiguity about which
status names the dispatcher recognises. Curl test payloads were unambiguous from the start.

**3. Iterations 3 through 8 were well-ordered and non-overlapping.**
Each iteration added one orthogonal capability with no rework of prior ones. The order was
correct: observability (3) before collision prevention (4) before quality improvements (5–8).
No iteration required touching files that a previous iteration had just stabilised.

**4. Autonomous SSH testing made verification fast.**
With the PEM key and IPs available to the assistant, every iteration could be committed,
deployed, and fully verified (logs, DB state, GitHub PR, branch existence) without manual
steps. The pattern of "push → wait for health check → fire webhook → check logs" became
routine and reliable.

---

### Gaps and Issues to Address in Phase 4 Instructions

**1. Repo mapping must be seeded as part of the deployment checklist.**
Every new environment (and every fresh prod deploy that wipes the DB) requires the mapping
to be re-added manually. Phase 4 should include either a seed SQL file, a `POST` call baked
into the deploy script, or an explicit post-deploy checklist step: "verify
`GET /debug/repo-mappings` returns at least one active mapping before testing."

**2. The sandbox repo main branch never advances — PRs are never merged.**
All PRs open against `main` but `main` always contains the original code (including the
deliberate duplicate import). Every run sees the same starting point and Claude makes the
same suggestion. Phase 4 should either merge a subset of PRs between sessions, use a
rotating sandbox, or remove the deliberate code smell and rely on story context alone to
guide suggestions.

**3. No cleanup of `/tmp/workflows/<run_id>/repo` directories in the worker container.**
Each workflow run clones into `/tmp/workflows/<run_id>/repo` inside the worker container
and never deletes it. On a long-running container, disk usage grows without bound. Phase 4
should add a `shutil.rmtree(work_dir)` cleanup step after the workflow completes or fails.

**4. Orphaned RUNNING runs after worker restarts should be auto-recovered on startup.**
Currently requires a manual SQL fix. Phase 4 should add a startup routine in `worker.py`
that transitions any rows stuck in `RUNNING` to `FAILED` with an "interrupted by restart"
message before the dequeue loop begins. This makes the state trustworthy without manual
intervention.

**5. Prod and dev repo mappings diverge silently.**
There is no mechanism to detect that dev has mappings prod does not (or vice versa). Phase 4
should either document an explicit sync step in the deploy runbook or expose a health
endpoint that lists active mappings so a quick `curl` confirms parity.

**6. Claude suggestion quality is bounded by the files shown to it.**
`_collect_key_files()` sends at most one README and three entry-point files. For a real Jira
story implementation, Claude often needs more context (related models, storage layer, existing
tests). Phase 4 should explore a smarter file-selection strategy — for example, keyword
matching between story summary and file names/content to select the most relevant files.

**7. Real Jira webhook integration has not been tested.**
Every test in Phase 3 used manually crafted curl payloads. The `changelog.items` format,
status field names, and issue type names were chosen to match expected Jira Cloud output but
have not been verified against an actual Jira instance. Phase 4 should include a real Jira
project wired to the dev webhook URL and at least one end-to-end trigger from a genuine
Jira status transition.

**8. No rate limiting or deduplication on the webhook endpoint.**
If Jira retries a webhook (which it does on non-2xx responses or timeouts), the same story
could be enqueued multiple times. Phase 4 should add idempotency logic — for example, check
whether a `QUEUED` or `RUNNING` run already exists for the same `(issue_key, workflow_type)`
before enqueuing a new one.
