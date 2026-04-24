# Phase 18 — Product Readiness Checklist

Use this checklist before treating any repo as an active managed project in the orchestrator.
Work through it top-to-bottom. Every item must be confirmed or explicitly waived.

---

## How to use

For each repo you want to manage, copy this section and fill in the blanks.

```
Repo:    <repo_slug>
Date:    <YYYY-MM-DD>
Env:     dev | prod
Checked by: <name>
```

---

## Readiness Checklist

### 1. Jira Integration

- [ ] **Jira webhook configured for this project key**
  - Jira webhook JQL filter includes the project (e.g. `project = KAN`)
  - If not: simulate manually via `POST /webhooks/jira?token=$JIRA_WEBHOOK_SECRET`
  - Verify: move a Jira issue to `Ready for Dev` and confirm a workflow run appears

- [ ] **Jira project key mapped to repo slug**
  - Confirm via `GET /debug/repo-mappings` or `/admin/ui/overview`
  - `is_active = true`, correct `base_branch`, `auto_merge_enabled` set intentionally

### 2. Repo Onboarding

- [ ] **Repo onboarded**
  - `/admin/ui/projects/{repo_slug}` shows a completed onboarding run
  - `current_step = completed`, `onboarding_status = COMPLETED`

- [ ] **Capability profile valid**
  - Profile name shows in dashboard (e.g. `python_fastapi`, `node_react`)
  - If `generic_unknown`: either add entry to `config/repo_command_hints.yaml` or accept blind runs

- [ ] **Test/build/lint commands available**
  - Dashboard shows test/build/lint command status from the last onboarding run
  - `NOT_RUN` for all three = no automated validation; accept this consciously for real repos

- [ ] **Architecture and coding conventions snapshots present**
  - `/admin/ui/projects/{repo_slug}` shows Architecture Summary and Coding Conventions sections
  - If missing: re-run onboarding

### 3. Deployment

- [ ] **Deployment profile configured or intentionally disabled**
  - Check `GET /debug/deployment-profiles?repo_slug=<slug>`
  - If `DRAFT_CREATED` or `NOT_CONFIGURED`: deployment validation will be `SKIPPED` (non-fatal; acceptable)
  - If `CONFIGURED_ENABLED`: verify `base_url` is set and smoke tests are defined

### 4. GitHub

- [ ] **Branch protection checked**
  - `GET /admin/github/branch-protection` for `repo_slug`
  - Required check `orchestrator/release-gate` present? If not: add via repo settings (see `docs/security/github-required-checks.md`)
  - Missing branch protection means auto-merge could bypass release gate

- [ ] **Release gate status published on at least one run**
  - Verify at least one run shows all 5 GitHub commit status contexts
  - Check via `/admin/ui/github` or `GET /debug/github-status-updates?run_id=<id>`

### 5. Orchestrator Health

- [ ] **Admin dashboard accessible**
  - `/admin/ui/login` loads and authenticates with `ADMIN_API_KEY`
  - Overview page shows recent runs, stats, no unexpected errors

- [ ] **Pause/resume verified**
  - `POST /admin/pause` pauses the orchestrator (Telegram notified if configured)
  - `POST /admin/resume` resumes
  - Jira events received while paused queue properly

- [ ] **First safe PR completed**
  - At least one Story has been implemented end-to-end: PR created, agents ran, release gate ran
  - No auto-merge on first run (set `auto_merge_enabled=false` until confident)
  - PR reviewed manually before merging

---

## Project-Specific Status

### suyogjoshi-com (`suyog19/suyogjoshi-com`)

| Item | Status | Notes |
|---|---|---|
| Jira webhook | ⚠️ Partial | KAN project not in Jira webhook JQL filter; must simulate manually |
| Repo onboarded | ✅ | Run id=10, COMPLETED 2026-04-24 |
| Repo mapping active | ✅ | id=12, KAN → suyog19/suyogjoshi-com, auto_merge=false |
| Capability profile | ⚠️ `generic_unknown` | Monorepo; no root-level test/build/lint commands |
| Test commands | ❌ NOT_RUN | `generic_unknown` has no test command |
| Architecture snapshot | ✅ | Mangum/Lambda adapter, CDK, OTP auth, sparse tests identified |
| Coding conventions | ✅ | Sync handlers, HTTPException, sub-package routers |
| Deployment profile | ⚠️ DRAFT_CREATED | `github_actions` type inferred; base_url not set |
| Branch protection | ❓ Unchecked | Check required before enabling auto-merge |
| Release gate status | ✅ | Run for KAN-29 published all 5 statuses |
| First PR completed | ✅ | KAN-29 → PR #10, manually reviewed |
| Auto-merge | ❌ Disabled | By policy for `generic_unknown`; leave disabled |

**Next actions:**
1. Update Jira webhook filter to include `project = KAN`
2. Add `config/repo_command_hints.yaml` entry for `suyog19/suyogjoshi-com` once a root Makefile exists
3. Set `base_url` in deployment profile when a stable URL is available
4. Check branch protection settings on GitHub

---

### Learning Platform (placeholder)

| Item | Status | Notes |
|---|---|---|
| Jira webhook | ❌ Not configured | Add project key to Jira webhook JQL filter |
| Repo onboarded | ❌ Not started | Run `POST /admin/project-onboarding/start` |
| Repo mapping active | ❌ Not created | Create after onboarding |
| Capability profile | ❓ Unknown | Will be detected on first onboarding |
| Test commands | ❓ Unknown | Depends on profile |
| Architecture snapshot | ❌ Not started | Generated during onboarding |
| Coding conventions | ❌ Not started | Generated during onboarding |
| Deployment profile | ❌ Not configured | Configure after onboarding |
| Branch protection | ❌ Unchecked | Check before enabling auto-merge |
| Release gate status | ❌ No runs yet | |
| First PR completed | ❌ No runs yet | |
| Auto-merge | ❌ Disabled | Keep disabled until first PR reviewed |

**Next actions:**
1. Identify Jira project key
2. Run `POST /admin/project-onboarding/start` with correct `repo_slug` and `base_branch`
3. Follow this checklist after onboarding completes

---

## Readiness Summary Template

After completing the checklist, record the final status:

```
Repo: <slug>
Date: <YYYY-MM-DD>
Ready for normal Epic → Story use: YES / NO

Blockers (must fix before use):
- ...

Accepted gaps (won't fix now):
- ...

First Epic recommendation:
- ...
```
