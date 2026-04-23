# Phase 1 Summary and Feedback — AI Dev Orchestrator

## Part 1: Phase 1 Summary

### What Was Built

A fully working orchestration backbone running on AWS EC2 (Ubuntu 22.04) with Docker Compose. The system receives Jira webhook events, persists them in PostgreSQL, dispatches stub workflows through a Redis-backed queue with concurrency control, and notifies the user at every step via Telegram.

**Infrastructure:**
- AWS EC2 t3.small (Ubuntu 22.04 LTS)
- Docker Compose stack: FastAPI app + PostgreSQL 16 + Redis 7 + worker process
- nginx reverse proxy with Let's Encrypt SSL (`https://orchestrator.suyogjoshi.com`)
- GitHub Actions self-hosted runner on the same EC2 VM
- Auto-deploy on push to `dev`; PR-gated deploy on `main`

**Application (9 modules):**

| Module | Responsibility |
|---|---|
| `main.py` | FastAPI app, startup hook, debug endpoints |
| `database.py` | PostgreSQL connection pool, table init with retry |
| `webhooks.py` | `POST /webhooks/jira` — receive, filter, persist, dispatch |
| `dispatcher.py` | Maps (issue_type, status) → workflow, creates queue entry |
| `queue.py` | Redis list-based job queue |
| `worker.py` | Background process, threading + semaphore for MAX_WORKERS=2 |
| `workflows.py` | `story_implementation` stub (log → sleep 5s → log) |
| `telegram.py` | Notification sender (startup, webhook, RUNNING, COMPLETED) |
| `repo_mapping.py` | Store/retrieve issue→repo mappings (prep for Phase 2) |

**Database (3 tables):**
- `workflow_events` — every Jira webhook event received
- `workflow_runs` — lifecycle of each workflow: QUEUED → RUNNING → COMPLETED
- `repo_mappings` — issue key to repo/branch mapping (Phase 2 prep)

**End-to-end flow that works today:**
```
Jira issue moved to "READY FOR DEV"
  → POST https://orchestrator.suyogjoshi.com/webhooks/jira
  → stored in workflow_events
  → Telegram: "Story status change / READY FOR DEV"
  → workflow_runs row created (QUEUED)
  → job pushed to Redis
  → worker picks up job → RUNNING → Telegram
  → story_implementation runs (stub) → COMPLETED → Telegram
```

**API endpoints:**

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | Health check |
| POST | `/webhooks/jira` | Jira event receiver |
| GET | `/debug/send-telegram` | Manual Telegram test |
| GET/POST | `/debug/repo-mappings` | Inspect/create repo mappings |
| GET | `/debug/repo-mappings/{key}` | Lookup single mapping |

**Scripts:**
- `scripts/setup-vm.sh` — installs Docker on EC2
- `scripts/setup-runner.sh` — installs GitHub Actions runner as systemd service
- `scripts/setup-ssl.sh` — installs nginx + certbot, issues SSL cert

---

### Planned But Not Achieved

Nothing from the original 12-task plan was left incomplete. Two items were added beyond the original scope:
- **Repo mapping model** — added as a Phase 2 prep step
- **Main branch deploy workflow + PR protection** — the original spec mentioned it but gave no implementation details; it was fleshed out and implemented

---

### Task Completion Status

| Task | Description | Status |
|---|---|---|
| 1 | Project skeleton + `/healthz` + logging | ✅ |
| 2 | Docker + Docker Compose | ✅ |
| 3 | VM setup instructions (AWS EC2 Ubuntu) | ✅ |
| 4 | GitHub self-hosted runner setup | ✅ |
| 5 | Dev branch auto-deploy workflow | ✅ |
| 6 | PostgreSQL integration | ✅ |
| 7 | Telegram bot integration | ✅ |
| 8 | Jira webhook endpoint | ✅ |
| 9 | Event persistence | ✅ |
| 10 | Workflow dispatcher (stub) | ✅ |
| 11 | Redis queue + worker process | ✅ |
| 12 | Stub workflow execution | ✅ |
| — | Repo mapping model (bonus) | ✅ |
| — | Main branch deploy + PR protection (bonus) | ✅ |

---

## Part 2: Feedback on Phase 1 Instructions

### Strengths

**1. Iterative structure was excellent.**
The 12-task breakdown with explicit "DO NOT skip steps" enforced discipline. Each task was small enough to implement, test, and confirm before moving forward.

**2. Working style expectations were clear.**
The instruction to ask before proceeding when credentials or architecture decisions are involved worked well — it prevented wrong assumptions about Telegram credentials, Jira status names, and domain setup.

**3. Non-goals list was valuable.**
Explicitly calling out what NOT to build (PR creation, real code gen, UI) prevented scope creep throughout.

**4. Data model was well-specified upfront.**
Having `workflow_events` and `workflow_runs` defined before implementation meant no redesign mid-build.

---

### Gaps and Issues to Address in Phase 2 Instructions

**1. HTTPS was not mentioned, but is mandatory for Jira Cloud.**
Jira Cloud webhooks reject HTTP URLs. SSL setup was not in the task list and was discovered mid-implementation. This added an unplanned nginx + Let's Encrypt step between Tasks 8 and testing.
> **Fix:** State SSL/HTTPS requirements upfront for any external-facing endpoints. Include SSL setup as an explicit task if a domain is involved.

**2. Trigger status name was underspecified.**
The spec said "Story → Final" as the trigger condition, but this was just an example. The actual status name ("READY FOR DEV") was decided during implementation and required creating a custom Jira status. This caused back-and-forth.
> **Fix:** Explicitly define all trigger conditions with exact values before implementation begins. If the value is user-defined, make that explicit and ask for it upfront.

**3. Tasks 8 and 9 were artificially split.**
"Jira webhook endpoint" and "Event persistence" are one atomic operation — you cannot receive a webhook without persisting it. They were implemented together and the split added no value.
> **Fix:** Avoid splitting tightly coupled concerns across tasks. Merge them into a single task.

**4. No mention of local dev limitations on Windows.**
`psycopg2-binary` has DLL issues on Python 3.14/Windows, which caused confusion during local testing. All meaningful testing ended up going through Docker.
> **Fix:** Explicitly state that local testing for anything touching DB or Redis should be done via `docker compose`, not local Python directly.

**5. GitHub repo visibility was not addressed.**
Setup scripts used raw GitHub URLs which returned 404 on a private repo. The repo had to be made public mid-implementation to unblock the VM setup.
> **Fix:** Specify repo visibility upfront, or provide an authentication method for private repos (e.g., use `gh` CLI to download scripts).

**6. Secrets and .env management on the VM was not designed upfront.**
The pattern of keeping `.env.orchestrator` as a persistent file on the VM and copying it during deploy was improvised. It works but was not part of the original design.
> **Fix:** Formally specify secrets management before implementation — options include a persistent `.env` file on the VM, GitHub Actions Secrets injected into the workflow, or AWS Parameter Store. Pick one and document it.

**7. Concurrency model was underspecified.**
The spec said "max 2 parallel workflows" but did not specify the mechanism. In-process threading with a semaphore was used, but other valid approaches exist (multiple worker containers, Redis-based locking).
> **Fix:** Specify the concurrency mechanism explicitly, not just the limit.

**8. No healthchecks for dependent services in Docker Compose.**
`depends_on` only waits for containers to start, not for PostgreSQL or Redis to be ready to accept connections. Retry logic in `init_db()` was added to compensate but was not in the spec.
> **Fix:** Include Docker Compose `healthcheck` definitions for `db` and `redis` services in the spec, so the app only starts when dependencies are truly ready.

**9. Branch strategy was not defined upfront.**
The `dev`/`main` structure and PR workflow were defined during implementation rather than specified at the start.
> **Fix:** Define the branching model (branch names, protection rules, merge strategy) as a prerequisite before any CI/CD tasks.

**10. Acceptance criteria lacked verifiable commands.**
Each task's definition of done was described in prose. Verification was done manually and inconsistently.
> **Fix:** For each acceptance criterion, provide an exact command that proves it passes — e.g., a `curl` command, a `psql` query, or a log grep — so there is no ambiguity about what "done" means.
