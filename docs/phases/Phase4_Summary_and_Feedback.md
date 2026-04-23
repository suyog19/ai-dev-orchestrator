# Phase 4 Summary and Feedback — AI Dev Orchestrator

## Part 1: Phase 4 Summary

### Goal

Harden the Phase 3 pipeline from a manually-managed prototype into a self-operating,
operationally durable system. Specific targets: auto-seed repo mappings on every deploy,
auto-recover stale runs on worker restart, clean up temporary workspaces, improve Claude's
file context selection to be story-aware, validate against a real Jira Cloud instance,
and prevent duplicate workflow execution from webhook retries.

---

### What Was Built

**New / significantly changed files:**

| File | Change |
|---|---|
| `app/database.py` | Added `recover_stale_runs()`, `update_run_step()`, `update_run_field()`; migrations for `started_at`, `completed_at`, `current_step`, `working_branch`, `pr_url`, `issue_key` columns |
| `app/worker.py` | `shutil.rmtree` workspace cleanup in `finally` block; startup recovery call before dequeue loop; `started_at`/`completed_at` timestamps on status transitions |
| `app/dispatcher.py` | `issue_key` stored on `workflow_runs` insert; `_active_run_exists()` deduplication check before enqueue |
| `app/claude_client.py` | Full rewrite of file selection: `_extract_keywords()`, `_select_files_for_story()` scorer replacing fixed `_collect_key_files()`; updated `SUGGEST_PROMPT` for multi-file input; selection logged with reasons |
| `app/repo_mapping.py` | Added `upsert_seed_mappings()` — idempotent check-then-insert for config-driven seed data |
| `app/main.py` | Added `GET /debug/mapping-health` (fingerprint + active mappings); `GET /debug/jira-events` (last N raw payloads) |
| `config/seed_mappings.json` | New file — canonical mapping config committed to repo, loaded by `init_db()` on every startup |
| `Dockerfile` | Added `COPY config/ ./config/` (seed file was unreachable in container without this) |
| `suyog19/sandbox-fastapi-app` | Removed deliberate duplicate import from `main.py`; added name validation in `create` endpoint; left realistic improvement areas (no thread safety in `storage.py`, no field length constraints in `models.py`) |

---

### Full Pipeline (Phase 4 additions)

The core flow is unchanged from Phase 3. Phase 4 adds the following around it:

```
Worker startup
  -> recover_stale_runs()
      UPDATE workflow_runs SET status='FAILED'
      WHERE status='RUNNING'             <- any run interrupted by prior restart
      RETURNING id                       <- count logged; Telegram alert if >0
  -> dequeue loop starts

POST /webhooks/jira (real Jira Cloud event, two POST requests per transition)
  -> first POST: changelog.items contains status change -> processed
  -> second POST: no changelog status item -> ignored (200 returned, nothing queued)

  -> dispatcher.dispatch()
      -> _active_run_exists(issue_key, workflow_type)
          SELECT id WHERE status IN ('QUEUED','RUNNING')  <- dedup check
          if found: WARNING logged, return None (webhook still returns 200)
          if not found: INSERT workflow_runs with issue_key column populated

  -> worker: _execute()
      [try]
      -> update_run_step(run_id, step) at 9 stages:
         mapping_lookup, cloning, analyzing, summarizing, suggesting,
         applying, pushing, creating_pr, done
      -> _select_files_for_story(repo_path, primary_language, issue_summary)
          for each source file: score by
            path keyword match (+3 per keyword)
            content keyword hits (capped at 5)
            entry-point membership (+2)
            test-file match when story mentions testing (+2)
          returns top 4 files + README, logged with selection reasons
      -> suggest_change sends all selected files to Claude (multi-file context)
      [finally]
      -> shutil.rmtree(/tmp/workflows/<run_id>) — runs on success AND failure

GET /debug/mapping-health
  -> returns active mapping list + SHA-256 fingerprint of sorted set
  -> same fingerprint on dev and prod = environments are in parity
```

---

### Iteration Completion Status

| # | Description | Status |
|---|---|---|
| 0 | Config-based seed mappings — `config/seed_mappings.json` upserted on every `init_db()` | Done |
| 1 | `upsert_seed_mappings()` — idempotent, no UNIQUE constraint needed | Done |
| 2 | Workspace cleanup — `shutil.rmtree` in `finally` block, runs on success and failure | Done |
| 3 | Startup stale run recovery — `recover_stale_runs()` marks RUNNING→FAILED before dequeue | Done |
| 4 | Sandbox evolution — deliberate import smell removed, realistic improvement areas left | Done |
| 5 | Scored file selection — keyword overlap replaces fixed entry-point list in `suggest_change` | Done |
| 6 | Real Jira Cloud end-to-end validation — KAN-6 triggered by actual Jira UI transition | Done |
| 7 | Webhook deduplication — active run check before enqueue; completed runs do not block re-trigger | Done |
| 8 | End-to-end Phase 4 validation — full checklist verified against fresh Jira story (KAN-7) | Done |

---

### Key Technical Decisions

**1. Config-file seeding with check-then-insert idempotency.**
Rather than a UNIQUE constraint (which would require schema changes and careful migration),
`upsert_seed_mappings()` checks for an existing active row matching
`(jira_project_key, issue_type, repo_slug)` before inserting. Every deploy runs the seed
safely — no duplicates created, no exceptions on repeat. The trade-off is that stale
seed entries must be disabled manually via the API if the config changes.

**2. Workspace cleanup in `finally`, not after the happy path.**
`shutil.rmtree(work_dir)` runs whether the workflow succeeds or fails, keeping disk usage
bounded in both cases. Placed in `finally` so it does not interfere with the run's final
status (`COMPLETED` or `FAILED` is set before or in the `except` block, not after `finally`).

**3. Stale run recovery runs before the dequeue loop, not on first job pickup.**
If recovery ran lazily (on first dequeue), a worker restart with no pending jobs would leave
stale `RUNNING` rows indefinitely. Running it at startup unconditionally means the DB state
is trustworthy as soon as the worker logs "Worker started".

**4. Keyword scoring: path match weighted 3×, content hits capped at 5.**
Path keyword overlap is a strong, low-noise signal (a file named `storage.py` is almost
certainly relevant to a story about "storage"). Content hits can be inflated in large files
(a 500-line file may mention "item" 50 times), so they are capped per keyword at 2 and
globally at 5 points. Entry-point bonus (2 pts) prevents well-known structural files from
disappearing entirely when they have no keyword overlap.

**5. Deduplication at dispatch time, not at the webhook handler.**
The webhook handler always returns `{"received":true,"processed":true}` regardless of
whether a duplicate is detected — this is correct: Jira must receive a 200 or it will retry.
The dedup check and the `WARNING` log happen inside `dispatch()`, below the Telegram
notification layer. Consequence: Telegram still fires a "Story status change" notification
even for a duplicate event, but no new workflow run is created.

**6. `issue_key` stored on `workflow_runs` to make dedup queries self-contained.**
The dedup check queries `workflow_runs` directly rather than joining back through
`workflow_events` → `payload_json`. This is simpler, faster, and avoids JSON parsing in
SQL. The `issue_key` column is nullable (existing rows have NULL) and is populated only
from the dispatch path, which always has the key available.

**7. `GET /debug/mapping-health` fingerprint for cross-environment parity.**
A SHA-256 hash of the sorted active mapping set (first 10 hex chars) gives a single
value to compare between dev and prod: same fingerprint = same configuration. The sort is
by `(jira_project_key, issue_type)` so insertion order and row IDs don't affect the result.

**8. Dual Jira webhooks (dev + prod) → same Telegram = duplicate notifications.**
When both Jira webhooks are active, every story transition fires two full pipelines and
sends two sets of Telegram messages. Discovered during Iteration 6. Short-term fix: disable
the prod webhook in Jira while testing on dev. Long-term fix needed: environment-prefixed
Telegram messages (`[DEV]` / `[PROD]`) or separate Telegram chats per environment.

---

### Problems Encountered and Solutions

**1. `config/seed_mappings.json` not found inside the container.**

- **Root cause:** `Dockerfile` had `COPY app/ ./app/` but no `COPY config/ ./config/`.
  The seed file existed in the repo but was invisible to `init_db()` running inside the
  container — `Path(__file__).parent.parent / "config" / "seed_mappings.json"` resolved to
  a path that did not exist in the image.
- **Fix:** Added `COPY config/ ./config/` to the Dockerfile. Discovered by checking
  `seed_file.exists()` returned False inside the container after the first deploy.

**2. Duplicate Telegram messages on every event.**

- **Root cause:** Both dev and prod Jira webhooks were active simultaneously, pointing at
  different VMs but sharing the same `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Each
  environment independently processed the event and sent its own notifications.
- **Fix (immediate):** Disabled the prod Jira webhook in Jira Cloud for the duration of
  Phase 4 dev testing.
- **Fix (pending):** Phase 5 should add an `ENV_NAME` variable to `.env.orchestrator` on
  each VM and prefix every Telegram message with `[DEV]` or `[PROD]`.

**3. Real Jira sends two POST requests per transition.**

- **Observed:** Every status transition fired two simultaneous `POST /webhooks/jira` calls
  with the same issue key. The first contained `changelog.items` with the status change;
  the second had an empty or missing changelog.
- **Impact:** None — the existing `changelog.items` filter already discarded the second
  request with `"Jira webhook ignored (no status change)"`. The behaviour was noted in
  Iteration 6 logs but required no code change.

**4. Worker container naming instability across `--force-recreate` deploys.**

- **Observed:** After some `docker compose up -d --force-recreate` runs, the worker
  container acquired a hash-prefixed name (`63aa98917ea7_ai-dev-orchestrator-worker-1`)
  instead of the clean `ai-dev-orchestrator-worker-1`. Caused by Docker not being able to
  remove the old container immediately during recreate.
- **Impact:** `docker logs ai-dev-orchestrator-worker-1` sometimes returned nothing; the
  correct container had a hash prefix. No functional impact on the pipeline itself.
- **Workaround:** Added a plain `docker compose up -d` call after `--force-recreate` to
  ensure all containers start under their canonical names.

---

## Part 2: Feedback on Phase 4 Instructions

### Strengths

**1. The execution guide was structured around the right problems.**
Every gap called out in the Phase 3 feedback document was addressed in Phase 4 — seeding,
cleanup, recovery, deduplication, real Jira validation, smarter file selection. The
mapping between feedback items and iteration goals was tight enough that no iteration felt
invented or out of place.

**2. Acceptance criteria were binary and testable.**
Each iteration had explicit verify steps (curl commands, SQL queries, log grep patterns)
that produced pass/fail results. "No stale RUNNING rows survive worker restart" can be
checked with a single SELECT; "duplicate webhook does not create extra run" can be checked
by counting rows. This made iteration sign-off unambiguous.

**3. The positive/negative path requirement was the right testing discipline.**
Requiring both "expected behavior works" and "bad behavior is rejected cleanly" for each
iteration caught real edge cases — particularly that a completed run should not block
re-triggering the same issue. Without the negative path test, the dedup check would have
been under-specified.

**4. The sandbox evolution task was well-scoped.**
Removing the deliberate code smell and leaving realistic improvement areas (no thread
safety, no field constraints) gave Claude natural targets without over-engineering the
test fixture. The file selection improvement (Iteration 5) then proved itself immediately
by routing model-related stories to `models.py` instead of always picking `main.py`.

---

### Gaps and Issues to Address in Phase 5

**1. Dev and prod share the same Telegram chat — no environment labelling.**
Every notification looks identical regardless of source. In Phase 5, `ENV_NAME` should be
added to `.env.orchestrator` on each VM (`dev` / `prod`) and prepended to the Telegram
`event` field in `send_message()` so `[DEV] Story status change` is visually distinct from
`[PROD] Story status change`.

**2. Prod was not re-validated after Phase 4 changes.**
All Phase 4 iterations were tested on dev only. Prod still runs the Phase 3 codebase
(`main` branch). Prod should receive a deploy and end-to-end test before Phase 5 begins —
particularly to pick up the seeding fix (Dockerfile `COPY config/`), deduplication, and
the `issue_key` column migration.

**3. The `issue_key` column is NULL on all pre-Phase-4 `workflow_runs` rows.**
Existing rows were inserted before the column existed. This is harmless for dedup queries
(they only match non-NULL issue keys), but the DB state is inconsistent. Phase 5 could
backfill `issue_key` from `workflow_events.payload_json` for completed runs as a one-time
migration, or simply document it as acceptable historical debt.

**4. No `GET /debug/workflow-runs` endpoint — DB inspection requires SSH.**
The guide suggested this endpoint as an optional enhancement (Section 7). All workflow
run inspection currently requires SSH to the VM and direct `psql` queries. Adding a
`GET /debug/workflow-runs?limit=N` and `GET /debug/workflow-runs/{id}` would make
operations self-service from `curl` and reduce friction for future debugging.

**5. File selection stops at the repo surface — no call graph or import traversal.**
The scorer ranks files by keyword overlap but does not follow imports. A story about
"thread safety in storage" correctly surfaces `storage.py`, but if `storage.py` imports
from a utility module that also needs changing, that utility file is invisible unless it
happens to score high on keywords. Phase 5 could add a lightweight import-graph pass for
Python repos to include direct dependencies of the top-scored file.

**6. Claude generates a change to a single file — multi-file stories are not supported.**
The `suggest_change()` contract is one suggestion, one file. Many real stories span
multiple files (e.g. "add a new endpoint" requires changes to the router, a model, and
possibly a storage layer). Phase 5 should either allow Claude to return a list of changes,
or chain multiple `suggest_change()` calls (one per file) guided by the story context.

**7. PRs are never merged — the sandbox repo accumulates open PRs.**
Every run opens a new PR against `main`, but `main` never advances. After Phase 4, there
are 20 open PRs on `sandbox-fastapi-app`. File selection improves relevance but cannot
compensate for a target repo that never incorporates any of its suggestions. Phase 5 should
explore either auto-merging validated PRs (after syntax and no-regression checks pass) or
using a per-run ephemeral branch off the previous run's merged state.

**8. No test for the startup recovery path under real conditions.**
Recovery was tested by observing "no stale runs found" on clean restarts. The recovery
path itself (marking RUNNING rows as FAILED) has only been exercised manually via SQL in
Phase 3. Phase 5 should include an explicit test: start a workflow, kill the worker
container mid-run, restart it, and verify the interrupted run appears as FAILED with the
expected `error_detail`.
