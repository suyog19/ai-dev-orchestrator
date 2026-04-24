# Using the Orchestrator for Real Projects

Operational guide for running the AI Dev Orchestrator on real projects day-to-day.

---

## 1. How to onboard a repo

**When:** First time setting up a repo to be managed by the orchestrator.

**Steps:**
1. Confirm the repo exists on GitHub and your `GITHUB_TOKEN` has write access.
2. POST to start onboarding:
   ```bash
   curl -X POST https://<orchestrator>/admin/project-onboarding/start \
     -H "X-Orchestrator-Admin-Key: $ADMIN_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"repo_slug": "owner/repo", "base_branch": "main"}'
   ```
3. Poll `GET /admin/project-onboarding/runs/<id>` until `status=COMPLETED` (typically 2–5 minutes).
4. Check the dashboard: `/admin/ui/projects/owner%2Frepo`
5. Review the architecture summary, coding conventions, and Makefile recommendation (if any).

**Dashboard path:** `/admin/ui/projects`

**Safe defaults:** Onboarding is read-only. No code changes are made.

---

## 2. How to activate a repo

**When:** Onboarding is complete and you want the orchestrator to start managing Stories.

**Steps:**
1. Open the project dashboard: `/admin/ui/projects/<repo_slug>`
2. Review the "Project Activation Status" section:
   - Onboarding: COMPLETED
   - Capability profile: detected (ideally not `generic_unknown`)
   - Branch protection: check link (requires `orchestrator/release-gate`)
3. Use the "Activate / Update Project Mapping" form:
   - Enter your Jira project key (e.g. `KAN`)
   - Set base branch (usually `main`)
   - Leave auto-merge **off** until first PR is reviewed
4. Click **Activate Project**
5. Verify mapping shows `ACTIVE` in the status table

**Or via API:**
```bash
curl -X POST https://<orchestrator>/admin/project-onboarding/owner%2Frepo/activate \
  -H "X-Orchestrator-Admin-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jira_project_key": "KAN", "base_branch": "main", "environment": "dev", "auto_merge": false}'
```

**Safe defaults:** `auto_merge=false`. Enable only after multiple safe PRs.

---

## 3. How to create the first Epic

**When:** Repo is activated and you have a feature to build.

**Steps:**
1. In Jira, create an Epic in the mapped project.
2. Add a clear description (≥50 chars) and at least 3 words in the title.
3. Move the Epic to `Ready for Breakdown` status.
4. If the Jira webhook filter covers the project: orchestrator will pick it up automatically.
5. If not: simulate the webhook manually:
   ```bash
   curl -X POST "https://<orchestrator>/webhooks/jira?token=$JIRA_WEBHOOK_SECRET" \
     -H "Content-Type: application/json" \
     -d '{"webhookEvent":"jira:issue_updated",...,"changelog":{"items":[{"field":"status","toString":"READY FOR BREAKDOWN"}]}}'
   ```

**Tip:** Vague Epics trigger the clarification loop — you'll get a Telegram message asking for details.

---

## 4. How to approve generated Stories

**When:** After `epic_breakdown` runs, you receive a Telegram message with story proposals.

**Commands:**
- `APPROVE <run_id>` — accept all stories, create them in Jira, queue for implementation
- `REJECT <run_id>` — discard proposals
- `REGENERATE <run_id>` — ask Claude to try again

**Via dashboard:** `/admin/ui/planning/<run_id>` — shows all proposals; approve/reject buttons.

**Tip:** Review proposals against your architecture snapshot. They use it for context.

---

## 5. How to review a PR

**When:** A Story has been implemented and a PR was created.

**What to look for:**
- PR comment from `AI Dev Orchestrator` with agent verdicts (Reviewer, Test Quality, Architecture)
- GitHub commit statuses (5 contexts: tests, reviewer, test-quality, architecture, release-gate)
- The release gate status (`orchestrator/release-gate`) determines if merge is allowed

**If `RELEASE_APPROVED` and auto-merge is on:** PR merges automatically.
**If `RELEASE_SKIPPED`:** PR requires manual merge. This is the safe default for new repos.
**If `RELEASE_BLOCKED`:** PR should not be merged. Fix the issue first.

**Manual merge:**
1. Review the agent comments on the PR
2. If satisfied, merge via GitHub UI
3. Run deployment validation if configured: `POST /debug/workflow-runs/<id>/run-deployment-validation`

---

## 6. How to handle clarification

**When:** The orchestrator sends a Telegram message asking a question.

**Story clarification:**
- `ANSWER <id> <your answer>` — provide the answer, story resumes
- `CANCEL <id>` — cancel; story fails with reason
- `CLARIFY <id>` — resend the question

**Via dashboard:** `/admin/ui/clarifications` — see all pending clarifications with inline answer form.

**Timeout:** 24 hours by default (`CLARIFICATION_TIMEOUT_HOURS`). After timeout, the run fails automatically.

---

## 7. How to handle a blocked release

**When:** Release gate shows `RELEASE_BLOCKED` on a PR.

**Diagnose:**
```bash
GET /debug/workflow-runs/<id>/release-decision
```
Returns which gates failed and why.

**Common issues:**
- Tests failed → fix the generated code (create a new Story to fix the bug)
- Reviewer blocked → check the reviewer comment on the PR for what's wrong
- Architecture blocked → check architecture agent comment; this usually means scope is too large

**Never merge a BLOCKED PR without understanding why.**

---

## 8. How to run deployment validation

**When:** Post-merge smoke testing is configured.

**Automatic:** After a successful merge, `run_deployment_validation()` runs automatically (observational only — failures don't revert the merge).

**Manual re-run:**
```bash
curl -X POST "https://<orchestrator>/debug/workflow-runs/<id>/run-deployment-validation?repo_slug=owner/repo&environment=dev" \
  -H "X-Orchestrator-Admin-Key: $ADMIN_API_KEY"
```

**Configure smoke tests:** `PUT /debug/deployment-profiles/<id>` with `base_url` and `smoke_tests` array.

**Dashboard:** `/admin/ui/deployments`

---

## 9. How to refresh project knowledge

**When:** Significant code changes have been made since onboarding, or you want updated architecture/conventions snapshots.

```bash
curl -X POST "https://<orchestrator>/debug/project-knowledge/owner%2Frepo/refresh?base_branch=main" \
  -H "X-Orchestrator-Admin-Key: $ADMIN_API_KEY"
```

Takes 60–120 seconds. Re-runs structure scan, architecture analysis, and coding conventions detection.

**Dashboard:** `/admin/ui/projects/<repo>` — check "Updated" timestamp on each snapshot.

---

## 10. How to use the orchestrator to improve itself

**Setup required:**
1. Orchestrator repo is onboarded: `suyog19/ai-dev-orchestrator` (done in Phase 18)
2. A dedicated Jira project key for orchestrator work (e.g. `ORCH`) mapped to the repo
3. Jira webhook must cover the `ORCH` project key
4. `ORCHESTRATOR_SELF_REPO=suyog19/ai-dev-orchestrator` env var set

**What's different:**
- Self-modification guard is always active: PRs are **never** auto-merged
- Release gate still runs — agents still review the code
- You get agent feedback before deciding whether to merge

**First dogfooding Epic suggestions:**
- "Add pagination to admin dashboard list pages"
- "Improve project knowledge refresh UX and documentation"
- "Add test for first-use mode and self-modification guard"

**Process:**
1. Create Epic in `ORCH` Jira project with description
2. Move to `Ready for Breakdown`
3. Review generated Stories via Telegram
4. Approve Stories
5. Review each PR carefully before merging manually

**Safe first use:** Keep auto-merge off, first-use mode will add an extra skip reason.

---

## First-use safety checklist before enabling auto-merge

- [ ] At least 3 successful PRs reviewed manually
- [ ] No unexpected file changes in any PR (all ≤3 files)
- [ ] Tests passing consistently
- [ ] Branch protection has `orchestrator/release-gate` as required check
- [ ] Deployment validation configured (or explicitly accepted as disabled)
- [ ] You understand what the agents approve and don't approve of

Only then: `PUT /debug/repo-mappings/<id>` with `{"auto_merge_enabled": true}`.
