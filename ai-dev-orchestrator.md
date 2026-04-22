# AI Dev Orchestrator – Phase 1 Execution Guide

## 1. Objective

You (Claude) are responsible for implementing **Phase 1 (Foundation)** of an AI-assisted software development workflow system.

This system will:
- Receive events from Jira
- Process them via an orchestration service
- Communicate with the user via Telegram
- Prepare for future AI-driven development workflows

⚠️ IMPORTANT:
Phase 1 is NOT about full automation.
It is about building a **working backbone with stub implementations**.

---

## 2. Working Style Expectations

### 2.1 Iterative Development (MANDATORY)

You must follow this loop:

1. Implement small feature
2. Ensure it runs locally
3. Provide test instructions
4. Wait for user confirmation
5. Only then move forward

DO NOT:
- Implement multiple iterations in one go
- Build future features “just in case”
- Over-engineer abstractions

---

### 2.2 When to Ask Questions

You MUST ask the user when:

- A decision impacts architecture
- Multiple valid approaches exist
- Credentials / secrets are required
- External services need setup (Jira, Telegram, GitHub)

Ask questions in this format:

```

QUESTION: <clear question>

OPTIONS:

1. Option A
2. Option B

RECOMMENDATION:
<your recommendation + why>

```

---

### 2.3 Coding Guidelines

- Keep code SIMPLE and readable
- Avoid unnecessary abstraction layers
- Prefer explicit logic over generic frameworks
- Add logging for all important actions
- Every module must have a clear purpose

---

## 3. Phase 1 Scope

You will implement the following capabilities:

### 3.1 Core System
- FastAPI-based service
- Health endpoint (`/healthz`)
- Logging setup

### 3.2 Infrastructure
- Docker + Docker Compose
- Ubuntu VM deployment
- GitHub self-hosted runner

### 3.3 CI/CD
- Auto deploy on `dev` branch
- Basic deploy on `main` branch

### 3.4 Database
- PostgreSQL
- Minimal schema for workflow events

### 3.5 Messaging
- Telegram bot integration
- Send notifications to user

### 3.6 Integration
- Jira webhook receiver
- Store incoming events
- Notify via Telegram

### 3.7 Workflow Skeleton
- Event classification
- Workflow dispatcher (stub)
- Queue + concurrency control (stub)
- One stub workflow: `story_implementation`

---

## 4. Explicit NON-GOALS (Do NOT implement)

- Real code generation using Claude
- GitHub repo modifications
- PR creation or review
- Full Jira automation logic
- Feature/Epic breakdown workflows
- Advanced retry or failure recovery
- UI/dashboard
- Production-grade security hardening

---

## 5. System Architecture (Phase 1)

Components:

- FastAPI Service (Core orchestrator)
- PostgreSQL (event storage)
- Redis (queue)
- Worker process (background jobs)
- Telegram Bot (communication)
- GitHub Actions (deployment)
- Ubuntu VM (execution environment)

---

## 6. Data Model (Minimal)

### Table: workflow_events

Fields:
- id (UUID or auto)
- source (e.g., "jira")
- event_type (string)
- payload_json (text/json)
- status (string)
- created_at (timestamp)

---

### Table: workflow_runs (basic stub)

Fields:
- id
- workflow_type
- status (RECEIVED, QUEUED, RUNNING, COMPLETED)
- related_event_id
- created_at
- updated_at

---

## 7. API Endpoints (Phase 1)

### Health
```

GET /healthz

```

### Debug
```

GET /debug/send-telegram

```

### Jira Webhook
```

POST /webhooks/jira

```

Behavior:
- Accept payload
- Validate basic structure
- Store in DB
- Send Telegram notification

---

## 8. Workflow Logic (Stub)

### Event Flow

Jira Event → Webhook → DB → Dispatcher → Queue → Worker → Telegram

---

### Supported Triggers (Phase 1)

You only need to RECOGNIZE:

- Epic → Final
- Feature → Final
- Story → Final

---

### Workflow Mapping

| Event | Workflow |
|------|---------|
| Story Final | story_implementation |

---

### Stub Workflow Behavior

When `story_implementation` runs:

1. Mark workflow as RUNNING
2. Log message
3. Sleep (simulate work)
4. Mark COMPLETED
5. Send Telegram message

---

## 9. Concurrency Requirement

- Max parallel workflows: configurable (default = 2)
- If limit reached:
  - queue the job
  - log message
  - notify via Telegram (optional)

---

## 10. Telegram Behavior

You must implement:

- Send message function
- Notify on:
  - app startup
  - webhook received
  - workflow started
  - workflow completed

Message format:
```

[Orchestrator]
Event: <type>
Status: <status>
Details: <short summary>

```

---

## 11. Deployment Strategy

### Dev Branch
- Auto deploy on push
- Uses GitHub Actions + self-hosted runner

### Main Branch
- PR required (structure only for now)
- Deployment workflow exists (can be same as dev for Phase 1)

---

## 12. Iteration Plan (STRICT ORDER)

You must implement in this order:

1. Project skeleton + `/healthz`
2. Docker setup
3. VM setup instructions
4. GitHub self-hosted runner
5. Dev deployment workflow
6. PostgreSQL integration
7. Telegram bot integration
8. Jira webhook endpoint
9. Event persistence
10. Workflow dispatcher (stub)
11. Queue + worker
12. Stub workflow execution

DO NOT skip steps.

---

## 13. Definition of Done (Phase 1)

Phase 1 is complete when:

- Service runs on VM
- Dev deploy works via GitHub
- Telegram messages are received
- Jira webhook reaches system
- Events are stored in DB
- Workflow is triggered (stub)
- Workflow executes via queue
- Concurrency limit is enforced
- User gets updates via Telegram

---

## 14. Repo Strategy (Important)

- This system has its own repo:
  `ai-dev-orchestrator`

- Future systems (learning platform, etc.) will use separate repos

- DO NOT assume:
  "1 Epic = 1 Repo"

---

## 15. Your First Task

Start with:

### Task 1: Project Skeleton

Goal:
- Create FastAPI app
- Add `/healthz`
- Add logging
- Add README

After implementation:
- Provide steps to run locally
- Provide test instructions
- WAIT for user confirmation

---

## 16. Final Instruction

Do NOT try to be “smart”.

Be:
- incremental
- explicit
- predictable

When in doubt → ASK.

You are building infrastructure that will later run autonomous AI workflows.

Stability > intelligence.
```

---
