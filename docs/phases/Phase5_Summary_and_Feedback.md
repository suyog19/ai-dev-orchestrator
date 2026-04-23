# Phase 5 Execution Summary — AI Dev Orchestrator

## Goal

Upgrade the AI Dev Orchestrator from a system that generates code changes and opens reviewable PRs into a system that **validates its own work** through test execution, bounded retry loops, and controlled auto-merge — closing the loop from Jira story to merged code with confidence.

---

## What Phase 5 Built

### The Closed-Loop Workflow

The final Phase 5 flow for every incoming Story:

```
Jira webhook → repo mapping → clone repo
  → repo analysis → Claude summary
  → context selection (keyword + AST import traversal)
  → Claude multi-file suggestion (≤3 files, structured tool output)
  → atomic validation + apply
  → test discovery + pytest execution
  → [if fail] one fix attempt → retest
  → [if still fail] abort with FAILED + Telegram
  → commit + push working branch
  → PR created with full diff and test section
  → auto-merge policy evaluated (4 conditions)
  → workflow state finalized, DB updated, Telegram notified
```

---

## Iterations

### Iteration 0 — Prerequisite Stabilisation

**Goal:** Establish a clean operational baseline before Phase 5 work began.

**Done:**
- Phase 4 deployed to prod; mapping fingerprints matched across environments
- `[DEV]` / `[PROD]` Telegram prefixes added via `ENV_NAME` env var
- `GET /debug/workflow-runs?limit=N` and `GET /debug/workflow-runs/{id}` endpoints added
- Sandbox merge policy confirmed: **Option A — controlled merge-forward** (approved PRs merged between sessions; `auto_merge_enabled=true` for `suyog19/sandbox-fastapi-app`)
- `workflow_runs` extended with `test_status`, `test_command`, `test_output`, `retry_count`, `files_changed_count`, `merge_status`, `merged_at`
- New `workflow_attempts` table added for per-attempt tracking

---

### Iteration 1 — Test Discovery and Execution

**Goal:** Detect and run pytest after every code change.

**New file: `app/test_runner.py`**
- Detects pytest via repo-root indicators: `pytest.ini`, `pyproject.toml`, `setup.cfg`, `conftest.py`, `tests/`
- Runs `pip install -r requirements.txt` then `pytest -q --tb=short`
- Returns `NOT_RUN | PASSED | FAILED | ERROR`
- 120-second timeout; output truncated to 4000 chars
- `NOT_RUN` is transparent — never fakes success for untestable repos

`workflows.py` updated to run tests after `apply_changes` and persist results to DB. Telegram now includes test status on every run.

---

### Iteration 2 — Sandbox Test Fixture

**Goal:** Ensure the sandbox repo is realistic enough for Phase 5 validation.

Confirmed `suyog19/sandbox-fastapi-app` contained:
- `conftest.py` with `autouse=True` fixture resetting in-memory storage between tests
- 17 active tests covering full CRUD cycle
- A `@pytest.mark.skip` test for PATCH partial update (unimplemented at that point)

Added PATCH endpoint (`ItemUpdate` model with optional fields, `PATCH /items/{item_id}`) to sandbox, enabling `test_update_item_name` to pass. Test count went from 17 pass + 1 fail to 18 passing.

---

### Iteration 3 — Fix Loop (Single Retry)

**Goal:** Allow exactly one controlled fix attempt when tests fail after implementation.

**`app/workflows.py`** extended with the fix loop:
- Detects `FAILED` or `ERROR` test status after attempt 1
- Calls `fix_change(repo_path, analysis, previous_changes, test_output)` — passes full test failure output to Claude
- Applies fix changes via `apply_changes`
- Reruns tests
- If still failing: `fail_run()` + Telegram alert, no PR
- If passing: continues to push + PR as normal

`workflow_attempts` table populated with one row per coding pass (implement / fix), with `model_used`, `test_status`, `files_touched`, `failure_summary`.

**`FIX_PROMPT` rules:**
- Change as few lines as possible
- Do not break currently passing tests
- Only modify files that were part of the original implementation
- Remove imports for non-existent types rather than inventing them

---

### Iteration 4 — Multi-File Suggestion Contract

**Goal:** Move from single-file edits to small, controlled multi-file changes (≤3 files).

**`app/claude_client.py`** — replaced `_CHANGE_TOOL` (single file) with `_CHANGES_TOOL` (array of up to 3 changes):
```json
{
  "changes": [
    { "file": "...", "description": "...", "original": "...", "replacement": "..." }
  ],
  "summary": "..."
}
```

`tool_choice = {"type": "tool", "name": "apply_code_changes"}` forces structured output — eliminates non-JSON response failures.

**`app/file_modifier.py`** — `apply_changes` rewritten with atomic validate-then-write:
- Groups changes by file (in order of first appearance)
- For each file: applies all its changes sequentially in memory
- Python syntax check (`ast.parse`) on final content
- Writes all files only after all pass validation — no partial commits

`SUGGEST_PROMPT` tightened: "Each entry in the changes array must target a DIFFERENT file — never repeat the same file path twice."

**Intermediate fix (dedup):** Run 51 revealed Claude returning two changes for the same file. `apply_changes` now composes same-file changes sequentially in memory before writing.

---

### Iteration 5 — Import-Aware Context Selection

**Goal:** Include files that are likely affected because they are imported by top-ranked files.

**`app/claude_client.py`** — `_select_files_for_story` rewritten:
- `_extract_python_imports(abs_path, repo_path)` — AST walk handling `ast.Import`, `ast.ImportFrom` (absolute and relative), resolves to repo-relative paths
- Top 2 non-test keyword-scored anchors
- Up to 2 import deps from anchors (Python only)
- Always appends the best test file (separate scoring pool)
- Selection logged with reason per file: `keyword`, `entry-point`, `baseline`, `import-dep:X`, `path:test; content:Nhits; test-file`

Example log:
```
File selection for 'add helper utility function':
keywords=['helper','utility','function']
selected: [('main.py','entry-point'), ('app/storage.py','import-dep:main.py'), ('tests/test_items.py','test-file')]
```

---

### Iteration 6 — Auto-Merge Policy

**Goal:** Prevent endless accumulation of open sandbox PRs under strict safety conditions.

**`app/github_api.py`** — added `merge_pull_request(repo_name, pr_number, commit_title)`:
- `PUT /repos/{slug}/pulls/{n}/merge` with `merge_method: squash`
- Raises `RuntimeError` on 405 (not mergeable) or 409 (conflict)

**`app/workflows.py`** — auto-merge gate applied after PR creation:
```python
auto_merge_ok = (
    mapping.get("auto_merge_enabled")
    and final_test_result["status"] == "PASSED"
    and applied.get("applied", False)
    and applied.get("count", 0) <= MAX_FILES_FOR_AUTOMERGE  # 3
)
```

`merge_status` set to `MERGED` | `SKIPPED` | `FAILED` with explicit reason logged and sent to Telegram.

**Bug fixed:** `upsert_seed_mappings` was INSERT-only — existing rows were silently skipped, so `auto_merge_enabled` was never updated from its column default of `FALSE`. Changed to a true upsert that UPDATEs `base_branch`, `notes`, `auto_merge_enabled` on existing rows.

---

### Iteration 7 — Workflow Run Inspection API

**`app/main.py`** — list and detail endpoints for workflow runs:
- `GET /debug/workflow-runs?limit=N` — list with `error_detail` truncated to 300 chars
- `GET /debug/workflow-runs/{id}` — full run detail including all `workflow_attempts` rows embedded

Bug fixed: the list endpoint had a phantom extra SELECT column for the truncated error detail. `_run_row_to_dict` was zipping against the column list without the alias, silently returning the full error_detail. Fixed by placing `left(error_detail, 300) AS error_detail` inline in the projection.

---

### Iteration 8 — Recovery Path Under Real Failure

**Goal:** Prove that `recover_stale_runs()` works under realistic mid-run interruption.

**Test procedure:**
1. Triggered live workflow (KAN-9)
2. `docker restart ai-dev-orchestrator-worker-1` 8 seconds after run started
3. Worker restarted → `recover_stale_runs()` fired on startup
4. Worker logs: `Startup recovery: marked 1 stale RUNNING run(s) as FAILED`
5. Telegram: `[startup / RECOVERY] 1 stale run(s) recovered — were left RUNNING before restart`
6. DB: run 56 — `status=FAILED`, `error_detail="Interrupted by worker restart before completion"`, `current_step=pushing`
7. Follow-up workflow (run 57) ran to completion normally

No code changes needed — Iteration 8 was a pure operational validation.

---

### Iteration 9 — End-to-End Phase 5 Validation

**Goal:** Verify all four required scenarios with live runs.

During validation, three bugs were found and fixed:

#### Bug 1 — COMPLETED overwrites FAILED (worker.py)

**Root cause:** When `story_implementation` called `fail_run()` and returned normally (not via exception), `_execute` still called `_update_run_status(COMPLETED)` — overwriting the FAILED status. Runs 49 and 50 showed `status=COMPLETED` with `test=FAILED` and `retry=1`.

**Fix:** After handler returns, query DB status; if already `FAILED`, skip the COMPLETED transition and return immediately.

#### Bug 2 — SUGGEST_PROMPT allowed test file modification

**Root cause:** Claude used all 3 change slots on source + test + README in one pass, bypassing the fix loop entirely by updating test expectations alongside the implementation.

**Fix:** Added to `SUGGEST_PROMPT`: *"Do not modify test files (any file under tests/ or named test_*.py). Tests define expected behaviour — if your implementation breaks a test, the fix loop will handle it."*

#### Bug 3 — Reverted-to-base PR creation failure (git_ops.py / workflows.py)

**Root cause:** When the fix attempt fully reverted all changes, `git status --porcelain` was empty — nothing pushed. But `create_pull_request` was still called for the unpushed branch, GitHub returned 422, `_get_existing_pr` found no open PR, raised `RuntimeError`.

**Fix:** `commit_and_push` returns `None` when nothing to commit. `workflows.py` checks for `None` after push: sets `merge_status=SKIPPED`, sends `pr_skipped` Telegram notification, exits cleanly.

#### Scenario Results

| Run | Issue | Scenario | Status | Test | Retry | Merge |
|-----|-------|----------|--------|------|-------|-------|
| 55 | KAN-9 | **S1 — Straight success** | COMPLETED | PASSED | 0 | MERGED |
| 63 | KAN-15 | **S2 — Fix-loop success** | COMPLETED | PASSED | 1 | MERGED |
| 59 | KAN-11 | **S3 — Hard failure** | FAILED | FAILED | 1 | — |
| 64 | ORCH-1 | **S4 — Tests not run** | COMPLETED | NOT_RUN | 0 | SKIPPED |

All four scenarios behave predictably. DB, Telegram, PR, and APIs agree on outcome for each.

---

## Files Changed in Phase 5

| File | Nature of change |
|------|-----------------|
| `app/test_runner.py` | New — pytest discovery and execution |
| `app/claude_client.py` | Rewrote `_CHANGES_TOOL` (multi-file); `_select_files_for_story` with AST import traversal; `_extract_python_imports`; `suggest_change` / `fix_change` refactored; `SUGGEST_PROMPT` and `FIX_PROMPT` hardened |
| `app/file_modifier.py` | `apply_changes` rewritten — group-by-file, compose in memory, atomic validate-then-write; `MAX_CHANGED_FILES = 3` |
| `app/workflows.py` | Fix loop; auto-merge block; `commit_and_push` `None` guard; PR body with diff block and test section; attempt tracking |
| `app/github_api.py` | `merge_pull_request` — squash merge via PUT; 405/409 error handling |
| `app/git_ops.py` | `commit_and_push` returns `None` on nothing-to-commit |
| `app/worker.py` | `recover_stale_runs()` on startup; COMPLETED-overwrites-FAILED guard |
| `app/repo_mapping.py` | `upsert_seed_mappings` — true upsert; updates existing rows on restart |
| `app/database.py` | `recover_stale_runs()`, `record_attempt()`, `complete_attempt()`, `fail_run()`, `update_run_field()` |
| `app/main.py` | `GET /debug/workflow-runs` list (error_detail truncation fix); `GET /debug/workflow-runs/{id}` with embedded `workflow_attempts` |
| `app/webhooks.py` | No change (already correct) |
| DB schema | New columns on `workflow_runs`; new `workflow_attempts` table |
| `suyog19/sandbox-fastapi-app` | PATCH endpoint (`ItemUpdate` model); search endpoint; soft-delete; updated healthz; accumulated 4 auto-merged PRs |

---

## Run Statistics (Phase 5 era, runs 43–64)

| Metric | Value |
|--------|-------|
| Total runs | 22 |
| COMPLETED | 15 |
| FAILED | 7 |
| Test PASSED | 14 |
| Test FAILED | 5 |
| Test NOT_RUN | 1 |
| Fix loop triggered | 6 |
| Fix loop succeeded | 1 (run 63) |
| Auto-merges | 4 (runs 55, 60, 61, 63) |
| PRs created on sandbox | 10+ |

---

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Max files per suggestion | 3 | Keeps diffs reviewable; prevents runaway multi-file changes |
| Fix attempts per run | 1 | Bounded loop; second failure is signal the story needs human review |
| Test files in suggestion | Forbidden | Tests are the spec; implementation must satisfy them, not rewrite them |
| Auto-merge method | GitHub squash merge via API | No branch protection rules required; avoids GitHub's auto-merge feature which needs branch protection |
| Same-file multi-change handling | Compose sequentially in memory | Prevents second write from silently overwriting first within one Claude response |
| Reverted-to-base case | Skip PR, mark COMPLETED/SKIPPED | Honest outcome — nothing to merge; avoids confusing 422 error |
| Context selection | Keyword scoring + AST import traversal + test file | Balances relevance without exploding prompt size |
| Stale run recovery | On worker startup, transition RUNNING → FAILED | Safe and deterministic; no race with active runs |

---

## Phase 5 Definition of Done — Status

| Criterion | Status |
|-----------|--------|
| Phase 4 baseline deployed and validated in prod | ✓ |
| Telegram messages environment-prefixed | ✓ |
| Workflow runs inspectable via HTTP API | ✓ |
| Test discovery and execution for pytest repos | ✓ |
| Test output persisted and visible | ✓ |
| One bounded fix loop | ✓ |
| Multi-file changes supported safely (≤3 files, atomic) | ✓ |
| Context selection includes import-aware dependency traversal | ✓ |
| Sandbox PR lifecycle does not accumulate stale PRs | ✓ (auto-merge) |
| Restart recovery tested under real interruption | ✓ (Iteration 8) |
| End-to-end validation: all 4 scenarios | ✓ (Iteration 9) |

**Phase 5 complete.**
