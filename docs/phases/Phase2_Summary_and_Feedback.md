# Phase 2 Summary and Feedback — AI Dev Orchestrator

## Part 1: Phase 2 Summary

### Goal

Transform the Phase 1 stub workflow into a real development automation pipeline: clone a GitHub repository, analyze its code, use Claude AI to summarize and suggest a change, apply that change, commit and push to a new branch, and open a pull request — all triggered by a single Jira webhook event.

---

### What Was Built

**6 new/modified modules:**

| Module | Role |
|---|---|
| `app/git_ops.py` | Clone repo at target branch, create `ai/issue-<key>` working branch, commit all changes, force-push to origin |
| `app/repo_analysis.py` | Walk repo tree, count file extensions, detect primary language, format Telegram summary |
| `app/file_modifier.py` | `apply_suggestion()` — applies Claude's string-replacement suggestion; `modify_file()` — timestamp-marker fallback |
| `app/github_api.py` | `POST /repos/{slug}/pulls` to create PR; handles 422 (PR already exists) by fetching the existing one |
| `app/claude_client.py` | `summarize_repo()` — 3–5 sentence technical summary via Claude Haiku with prompt caching; `suggest_change()` — structured JSON code improvement suggestion |
| `app/workflows.py` | Full pipeline wiring: clone → analyze → summarize → suggest → apply → commit → push → PR |

**`Dockerfile` change:** added `git` installation (`apt-get install -y git`) so the worker container can run git commands.

**`requirements.txt` additions:** `requests==2.32.3` (GitHub API), `anthropic==0.52.0` (Claude SDK).

**`.env.example` additions:** `GITHUB_TOKEN`, `ANTHROPIC_API_KEY`.

---

### End-to-End Pipeline (as implemented)

```
Jira issue moved to "READY FOR DEV"
  → POST /webhooks/jira (existing Phase 1 endpoint)
  → repo_mapping lookup → find GitHub repo + target branch
  → git clone --depth=1 → create ai/issue-<key> branch
  → Telegram: repo_analysis / COMPLETE (language, file count, extensions)
  → Claude Haiku: summarize_repo() → 3–5 sentence summary
  → Telegram: claude_summary / COMPLETE
  → Claude Haiku: suggest_change() → JSON {file, description, original, replacement}
  → Telegram: claude_suggestion / COMPLETE (shows original + replacement)
  → apply_suggestion() → string replace in source file (fallback: timestamp marker)
  → Telegram: file_apply / COMPLETE
  → git add -A → git commit (message = Claude suggestion description)
  → git push --force origin ai/issue-<key>
  → Telegram: git_push / COMPLETE
  → GitHub API: create or fetch PR (title + body include Claude summary and diff block)
  → Telegram: pr_created / COMPLETE (PR number + URL)
  → Telegram: workflow / COMPLETED
```

**Total Telegram notifications per workflow run: 8**

---

### Iteration Completion Status

| Iteration | Description | Status |
|---|---|---|
| 1 | Git clone + branch creation (`git_ops.py`) | ✅ |
| 2 | Repo analysis — file walk, language detection, Telegram summary | ✅ |
| 3 | File modification stub — append timestamp marker to README | ✅ |
| 4 | Commit and push working branch to GitHub | ✅ |
| 5 | GitHub PR creation via REST API | ✅ |
| 6 | Claude repo summary — Haiku with prompt caching | ✅ |
| 7 | Claude code suggestion — structured JSON response | ✅ |
| 8 | Apply Claude suggestion — string replacement in source file | ✅ |
| 9 | End-to-end polish — commit message and PR body use Claude's output | ✅ |

---

### Key Technical Decisions

**Git via subprocess, not GitPython.**
Used `subprocess.run(["git", ...])` throughout. Simpler, no extra dependency, and git must be installed in the container anyway. Raises `RuntimeError` on non-zero exit codes.

**Force push for AI-owned branches.**
Each workflow run recreates the branch from scratch (fresh clone). `git push --force` is safe here because `ai/issue-<key>` branches are exclusively owned by the orchestrator and are never the base for other work.

**Working directory isolation via run_id.**
Each workflow clones into `/tmp/workflows/<run_id>/repo` inside the worker container. Concurrent workflows (up to `MAX_WORKERS=2`) never share a directory.

**Claude Haiku for cost efficiency.**
Both `summarize_repo()` and `suggest_change()` use `claude-haiku-4-5-20251001`. The system prompt is marked with `cache_control: ephemeral` to benefit from prompt caching on repeated calls.

**Structured JSON suggestion with fallback.**
`suggest_change()` instructs Claude to return only valid JSON. If the JSON parses but the `original` text is not found in the file (e.g., Claude hallucinated a snippet), `apply_suggestion()` returns `applied: False` and the workflow falls back to `modify_file()` so there is always something to commit.

**PR body includes Claude's full output.**
The PR body contains the repo summary, the file changed, the suggestion description, and a diff block showing original vs replacement — making the PR self-documenting without requiring the reviewer to read logs.

---

### Problems Encountered and Solutions

**1. Anthropic API: "credit balance too low" despite having balance**

- **Root cause (part 1):** The ANTHROPIC_API_KEY on EC2 was in the Anthropic **Default** workspace, but credits had been added to the **Claude Code** workspace (a separate billing pool). Credits are not shared between workspaces.
- **Root cause (part 2):** `platform.claude.com` and `console.anthropic.com` now redirect to the same portal, but the billing applies at the organization level — and the "credit grants" shown in invoice history were promotional/platform credits tied to Claude.ai/Claude Code usage, not API credits usable by custom keys. A new API key had to be created from the correct workspace after purchasing real API credits there.
- **Fix:** Created a new API key in the workspace that held purchased credits and updated EC2.

**2. New API key not picked up after restart**

- **Root cause:** `docker compose up -d` does not recreate running containers — only new containers pick up changed env vars. Additionally, the container reads from `docker-compose.yml`'s `env_file: .env` (the project directory), not from `/home/ubuntu/.env.orchestrator` passed via `--env-file` to docker compose (which only controls compose file variable substitution, not container environment).
- **Fix:** Update the correct `.env` file in the project directory AND use `docker compose up -d --force-recreate`.

**3. git push rejected on second run**

- **Root cause:** The first run had already pushed `ai/issue-TEST-1` to origin. A fresh clone on the second run has no history for that remote branch, so a normal push is rejected.
- **Fix:** Changed to `git push --force origin <branch>` — safe for AI-owned branches.

**4. Webhook payload missing `changelog.items`**

- **Root cause:** Test curl payloads initially omitted the `changelog` object. The dispatcher filters on `changelog.items[*].field == "status"`, so events without it were silently ignored.
- **Fix:** All test payloads must include `"changelog": {"items": [{"field": "status", "fromString": "...", "toString": "READY FOR DEV"}]}`.

**5. Two separate `.env` files with different purposes**

- `/home/ubuntu/.env.orchestrator` — persistent VM-level secrets, copied to the project directory during GitHub Actions deploy
- `/home/ubuntu/actions-runner/.../ai-dev-orchestrator/.env` — what containers actually read via `env_file:`
- Manual updates to `.env.orchestrator` do NOT propagate to running containers until the next deploy. This caused confusion when manually updating the API key mid-session.

---

## Part 2: Feedback on Phase 2 Instructions

### Strengths

**1. Iteration-by-iteration structure worked well.**
Nine focused iterations kept each change small, testable, and reversible. No single iteration felt overwhelming. The structure from Phase 1 carried over effectively.

**2. Clear module responsibilities.**
Each iteration had a single owner module. There was no ambiguity about where new code should go.

**3. Autonomous SSH testing was highly efficient.**
Once the EC2 key and IP were provided to the AI assistant, all test steps (deploy wait, webhook fire, log inspection) could be run without manual intervention. This dramatically reduced the back-and-forth per iteration. Phase 3 instructions should assume this pattern from the start.

---

### Gaps and Issues to Address in Phase 3 Instructions

**1. Anthropic API billing and workspace setup must be a prerequisite step.**
The most significant blocker in Phase 2 was Anthropic API credential confusion. The instructions should include an explicit prerequisite checklist:
- Which Anthropic workspace the API key belongs to
- Verify that workspace has purchased credits (not just promotional credit grants)
- Test the key with a minimal `curl` call before starting implementation
- Confirm the key is in the project `.env` on EC2, not just `.env.orchestrator`

**2. The two-file `.env` pattern must be documented explicitly.**
There are effectively two `.env` files: the persistent one on the VM (`.env.orchestrator`) and the one containers read (`.env` in the project directory, overwritten on each deploy). Any manual credential update mid-iteration must go into both files, or the container will silently use the stale value. This should be documented in CLAUDE.md.

**3. `docker compose up -d` does not reload env vars — must use `--force-recreate`.**
Instructions that say "restart the stack" are ambiguous. Phase 3 instructions should always say `docker compose up -d --force-recreate` when environment variable changes need to take effect.

**4. Repo mapping is hardcoded — Phase 3 needs a real mapping strategy.**
The current `repo_mapping` table has a single hardcoded entry (`TEST-1` → `suyog19/ai-dev-orchestrator`). For Phase 3 to handle real Jira projects and multiple repos, the mapping strategy needs to be specified: is it a DB table managed via API, a config file, or derived from Jira project key → GitHub org convention?

**5. Claude's suggestions target the orchestrator's own repo — this is circular.**
In Phase 2, the workflow clones `ai-dev-orchestrator` itself and applies suggestions to its own source code. This works as a test but is not the intended production behavior. Phase 3 should use a separate target repo (e.g., a sandbox project) to test real code generation against a codebase that Claude has not seen during training.

**6. Claude Haiku suggestion quality is limited — Phase 3 should use a stronger model.**
Claude Haiku consistently suggests trivial changes (duplicate import removal, missing docstring). For Phase 3 to generate meaningful code from a Jira story description, a stronger model (Sonnet or Opus) with extended thinking should be used for the code generation step, with Haiku retained for cheaper analysis tasks.

**7. No error handling at the workflow level.**
If any step in `story_implementation` raises an exception, the worker catches it but the workflow run stays in `RUNNING` state indefinitely (no `FAILED` status). Phase 3 should add a `try/except` wrapper that transitions the run to `FAILED`, sends a Telegram error notification, and records the traceback.

**8. The PR always targets the same `ai/issue-TEST-1` branch — no uniqueness per run.**
Because the branch name is derived from the issue key (not the run ID), every test run overwrites the same branch and updates the same PR. In production, if a Jira story triggers multiple workflow runs (e.g., after re-opening), they collide. Phase 3 instructions should define a branch naming strategy that includes the run ID or a timestamp for uniqueness.

**9. Acceptance criteria should include PR content verification.**
Test steps checked Telegram messages and logs but not the actual GitHub PR content (title, body, diff). Phase 3 acceptance criteria should include verifying the PR via `gh pr view <number>` to confirm the body, the commit message, and that the diff shows the correct file change.

**10. Dev and production must run on separate VMs — set this up as a Phase 3 prerequisite.**

Currently both `dev` and `main` branch workflows deploy to the same EC2 instance (`65.2.140.4`) via the same self-hosted GitHub Actions runner, overwriting the same Docker Compose stack. Whichever branch deployed last is what is running. This is acceptable while both environments are development-only, but becomes a problem in Phase 3 when the system will be connected to real Jira, making real commits to real repos, and sending real Telegram notifications.

**Why a second VM, not Docker Compose profiles on the same VM:**

- The workflow has real side effects (git pushes, GitHub PRs, Telegram messages). A dev test accidentally targeting the prod stack — or vice versa — is not a port conflict, it is corrupted state requiring manual cleanup.
- A t3.small running two full stacks (app + worker + PostgreSQL + Redis, twice over) plus the GitHub Actions runner is tight on memory. Resource contention would require tuning limits instead of building features.
- A shared runner means a stuck dev workflow job can block prod deploys. Separate VMs get separate runners.
- Two VMs mirrors real-world production architecture, which is valuable if this project is also a DevOps learning exercise.

**Why not before Phase 3:**
As of the end of Phase 2, `main` has never been deployed in any meaningful way — it carries the same code as `dev` and has no real traffic or Jira connection to protect. Adding a second VM now would be overhead with no payoff.

**Recommended environment split for Phase 3:**

| Environment | VM | Branch | Runner label | Purpose |
|---|---|---|---|---|
| Dev | Current EC2 (`65.2.140.4`) | `dev` | `self-hosted-dev` | Development and iteration |
| Production | New EC2 (t3.micro) | `main` | `self-hosted-prod` | Stable, Jira-connected instance |

**Implementation notes for Phase 3 instructions:**

- Add the new VM as the first task in Phase 3, before any feature work. It is a prerequisite, not an optional step.
- The new VM reuses existing scripts from Phase 1: `scripts/setup-vm.sh` (Docker install), `scripts/setup-runner.sh` (GitHub Actions runner), `scripts/setup-ssl.sh` (nginx + Let's Encrypt). The only change is registering the runner with a distinct label (e.g. `self-hosted-prod`) so workflow files can target it explicitly.
- Update `deploy-dev.yml` to use `runs-on: self-hosted-dev` and `deploy-main.yml` to use `runs-on: self-hosted-prod` so the two environments are fully isolated.
- Each VM needs its own `.env.orchestrator` with environment-appropriate values. In particular, consider using a separate Telegram bot or chat ID for prod notifications so dev noise does not pollute prod alerts.
- A t3.micro (~$8–9/month) is sufficient for the production VM at this scale. The dev VM (current t3.small) retains its existing runner and stack unchanged.
- SSL and the domain should be updated: e.g. `orchestrator.suyogjoshi.com` points to prod; `dev.orchestrator.suyogjoshi.com` (or a separate subdomain) points to dev. This ensures Jira can be configured to send webhooks to the correct environment independently.
