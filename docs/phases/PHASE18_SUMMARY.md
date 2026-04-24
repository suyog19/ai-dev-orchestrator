# Phase 18 — Product Readiness & First Real Projects

**Status:** COMPLETED  
**Date:** 2026-04-24  
**Branch:** dev → main

---

## Objective

Transition the AI Dev Orchestrator from a working prototype into a production-ready system that can manage real external projects. This phase established the full onboarding-to-execution lifecycle, added safety rails for new projects, and validated the system by dogfooding it on itself.

---

## What Was Built (12 Iterations)

### Iteration 0 — Readiness Runbook
Created `docs/runbooks/phase18-product-readiness.md`: an 11-item operational checklist covering the full lifecycle from Jira webhook through first safe PR. Includes project-specific status tables.

### Iteration 1 — Project Activation Endpoint
Added `POST /admin/project-onboarding/{repo_slug}/activate` — a 6-step activation report that verifies onboarding is complete, upserts the Jira repo mapping, checks capability profile, validates branch protection, and returns actionable recommendations. Returns `{activated, steps, recommendations}`.

### Iteration 2 — Project Activation Dashboard UI
Added the "Project Activation Status" section to `app/templates/admin/project_detail.html` with rows for: onboarding status, Jira mapping, capability profile, deployment profile, branch protection, self-modification guard (Phase 10), first-use mode (Phase 6), and recommended next action. Added a CSRF-protected "Activate / Update Project Mapping" form backed by a new UI-router route (`POST /admin/ui/projects/{repo_slug}/activate`).

### Iteration 3 — Repo Command Hints (YAML Override)
Added `config/repo_command_hints.yaml` — a per-repo override file that lets operators specify exact test/build/lint commands for repos whose auto-detection produces `generic_unknown` or incorrect results. The hints loader (`_load_hints()`, `_apply_hints()`) is called at the end of `detect_repo_capability_profile()` and stamps `profile_source = "configured_hint"` or `"auto_detected"` on the returned dict. The Dashboard "Profile Source" row shows which path was used.

### Iteration 4 — Makefile Recommendation
Added `_generate_makefile_recommendation()` in `app/onboarding.py` — triggered as Step 8 of onboarding for repos with `generic_unknown` profile and `NOT_RUN` tests. Generates a suggested `Makefile` and the corresponding `repo_command_hints.yaml` entry, stored as a `makefile_recommendation` knowledge snapshot. The project detail page renders this as a yellow warning box with the suggested content.

### Iteration 5 — Knowledge Refresh
Implemented `run_knowledge_refresh(repo_slug, base_branch)` in `app/onboarding.py` — re-runs Steps 4–8 of onboarding (structure scan, architecture summary, coding conventions, deployment check, makefile rec) without re-detecting the capability profile or re-running commands. Connected to `POST /debug/project-knowledge/{repo_slug}/refresh`. The endpoint was previously a placeholder stub.

### Iteration 6 — First-Use Safety Mode
Added `FIRST_USE_MODE_ENABLED` / `FIRST_USE_RUN_COUNT` env vars (defaults: `true` / `3`). Implemented `is_first_use_mode_active(repo_slug)` in `workflows.py` using `count_completed_workflow_runs_for_repo()` — counts successfully completed `story_implementation` runs for a repo's active Jira mappings. When active, `evaluate_release_decision()` adds a skip reason, making the release gate return `RELEASE_SKIPPED` regardless of agent verdicts. Operators must manually review and merge the first `N` PRs before auto-merge engages.

### Iteration 7 — Project Bootstrap Workflow
Created `app/bootstrap.py` with `run_project_bootstrap()` — clones an empty or near-empty repo, copies a template skeleton, customizes the README with the project description, creates a branch `ai/bootstrap-<type>-<run_id>`, commits, pushes, and opens a PR. Integrated as `POST /admin/project-bootstrap/start`. Supported project types: `python_fastapi`, `static_site`.

### Iteration 8 — Bootstrap Templates
Created template files under `templates/bootstrap/`:
- `python_fastapi/`: `app/main.py`, `tests/test_health.py`, `requirements.txt`, `README.md`
- `static_site/`: `index.html`, `styles.css`, `README.md`

Added `COPY templates/ ./templates/` to `Dockerfile` (was missing; templates were inaccessible in containers).

### Iteration 9 — Orchestrator Self-Dogfooding
Onboarded `suyog19/ai-dev-orchestrator` as a managed project:
- Profile: `python_fastapi` (auto-detected)
- Tests: `pytest -q --tb=short` — PASSED on onboarding
- Architecture + coding conventions snapshots generated
- Jira mapping created (ORCH project key, `dev` branch)

### Iteration 10 — Self-Modification Safety Guard
Added `is_self_modification(repo_slug)` in `workflows.py` — checks `ORCHESTRATOR_SELF_REPO` env var. When the active repo equals the orchestrator itself, `evaluate_release_decision()` appends a skip reason, permanently blocking auto-merge regardless of all other gates. The dashboard shows a "Self-Modification Guard: ACTIVE" badge for the orchestrator repo.

### Iteration 11 — Operations Runbook
Created `docs/runbooks/using-orchestrator-for-real-projects.md` — a 10-section day-to-day operations guide covering: onboard, activate, create first Epic, approve Stories, review PRs, handle clarification, handle blocked releases, run deployment validation, refresh project knowledge, and self-dogfooding setup. Includes curl commands, dashboard paths, and a pre-auto-merge safety checklist.

### Iteration 12 — Final E2E Validation
All 5 validation scenarios passed on the dev EC2 instance:

| Scenario | Result |
|---|---|
| A — Project Activation (suyogjoshi-com) | PASS (6 steps, mapping verified) |
| B — Knowledge Refresh | PASS (5 snapshots refreshed via Claude) |
| C — First-use Safety Mode | PASS (self_mod guard + first-use logic correct) |
| D — Release Gate Logic (5 cases) | PASS (APPROVED / SKIPPED / BLOCKED all correct) |
| E — Self-dogfooding Readiness | PASS (profile=python_fastapi, ORCH mapping, guard active) |

---

## Files Changed

| File | Change |
|---|---|
| `app/main.py` | `POST /admin/project-onboarding/{repo_slug}/activate`, `POST /admin/project-bootstrap/start`, real knowledge refresh implementation |
| `app/ui.py` | UI project detail: activation_result, first-use mode, self-mod guard; `POST /admin/ui/projects/{repo_slug}/activate` |
| `app/workflows.py` | `is_self_modification()`, `is_first_use_mode_active()`, `evaluate_release_decision()` extended with both safety gates |
| `app/onboarding.py` | `_generate_makefile_recommendation()` (Step 8), `run_knowledge_refresh()` |
| `app/database.py` | `count_completed_workflow_runs_for_repo()`, `profile_source` in capability profile return |
| `app/repo_profiler.py` | `_load_hints()`, `_apply_hints()` — YAML command hint override system |
| `app/bootstrap.py` | New: `run_project_bootstrap()` — full skeleton bootstrap workflow |
| `config/repo_command_hints.yaml` | New: per-repo test/build/lint command overrides |
| `templates/bootstrap/python_fastapi/` | New: 4 template files |
| `templates/bootstrap/static_site/` | New: 3 template files |
| `Dockerfile` | Added `COPY templates/ ./templates/` |
| `.env.example` | Added `FIRST_USE_MODE_ENABLED`, `FIRST_USE_RUN_COUNT`, `ORCHESTRATOR_SELF_REPO` |
| `requirements.txt` | Added `PyYAML==6.0.3` |
| `app/templates/admin/project_detail.html` | Full "Project Activation Status" section, activate form, makefile recommendation, profile source |
| `docs/runbooks/phase18-product-readiness.md` | New: onboarding readiness checklist |
| `docs/runbooks/using-orchestrator-for-real-projects.md` | New: 10-section operations guide |

---

## Key Design Decisions

**Activation vs. raw mapping creation:** The new `/activate` endpoint runs a structured 6-step verification report rather than a raw DB insert. This gives operators confidence that the system is ready before any Jira events are processed.

**Knowledge refresh skips profile re-detection:** Re-running command detection on an already-onboarded repo would wipe manually overridden hints. The refresh path intentionally starts at Step 4 (structure scan) to preserve the operator-configured profile.

**First-use mode uses completed run count, not a per-repo flag:** This avoids a manual "graduation" step. As soon as the operator has reviewed and merged the threshold number of PRs, the system graduates automatically.

**Self-modification guard is always active, never configurable:** It's a hard-coded check in `evaluate_release_decision()`. Operators cannot accidentally enable auto-merge for the orchestrator repo — even if `auto_merge_enabled=True` is set on the mapping.

**`ORCHESTRATOR_SELF_REPO` env var needed on EC2:** The `.env.example` was updated but the env var also needed to be appended to `/home/ubuntu/.env.orchestrator` on the dev VM. This was done during Iteration 12 validation.

---

## What Phase 18 Enables

- Onboard any GitHub repo in 2–5 minutes
- Activate it with one API call or dashboard form
- Get architecture + conventions snapshots automatically
- First N PRs require manual review — safe graduation to auto-merge
- Orchestrator manages itself safely (no accidental self-merge)
- Bootstrap new projects from templates in one API call
- Refresh project knowledge without re-running commands
- Override auto-detected profiles via YAML for atypical repos

---

## Next Phases (Not in Scope)

- Real ORCH Jira project setup (ORCH project key needs to exist in Jira)
- Learning Platform project onboarding and activation
- Pagination on admin dashboard list pages (Phase 19 candidate)
- Global-scope memory (deferred)
