# Endpoint Inventory — Security Classification

All HTTP endpoints exposed by the orchestrator, classified by auth requirement and mutation risk.

## Classification

| Level | Meaning |
|---|---|
| `none` | No auth required — public or webhook |
| `admin` | Requires `X-Orchestrator-Admin-Key` header |
| `signed` | Requires signature or token validation (webhook source) |

---

## Health

| Method | Path | Auth | Mutates | Notes |
|---|---|---|---|---|
| GET | `/healthz` | none | no | Public liveness probe |

---

## External Webhooks

| Method | Path | Auth | Mutates | Notes |
|---|---|---|---|---|
| POST | `/webhooks/jira` | signed (`JIRA_WEBHOOK_SECRET` query param) | yes | Inserts workflow_events, triggers dispatch |
| POST | `/webhooks/telegram` | signed (chat_id == `TELEGRAM_CHAT_ID`) | yes | Handles APPROVE/REJECT/REGENERATE; must reject wrong senders |

---

## Debug — Read-only

| Method | Path | Auth | Mutates | Notes |
|---|---|---|---|---|
| GET | `/debug/workflow-runs` | admin | no | Lists recent runs |
| GET | `/debug/workflow-runs/{run_id}` | admin | no | Full run detail |
| GET | `/debug/workflow-runs/{run_id}/reviews` | admin | no | Reviewer Agent verdicts |
| GET | `/debug/workflow-runs/{run_id}/test-quality` | admin | no | TQ Agent verdicts |
| GET | `/debug/workflow-runs/{run_id}/architecture` | admin | no | Architecture Agent verdicts |
| GET | `/debug/workflow-runs/{run_id}/release-decision` | admin | no | Release Gate decision |
| GET | `/debug/jira-events` | admin | no | Raw Jira webhook payloads |
| GET | `/debug/planning-runs` | admin | no | Epic planning runs |
| GET | `/debug/planning-runs/{run_id}` | admin | no | Full planning run |
| GET | `/debug/agent-reviews` | admin | no | Reviewer Agent list |
| GET | `/debug/test-quality-reviews` | admin | no | TQ Agent list |
| GET | `/debug/architecture-reviews` | admin | no | Architecture Agent list |
| GET | `/debug/repo-mappings` | admin | no | All repo mappings |
| GET | `/debug/repo-mappings/{id}` | admin | no | One mapping |
| GET | `/debug/mapping-health` | admin | no | Active mappings + fingerprint |
| GET | `/debug/feedback-events` | admin | no | Raw feedback events |
| GET | `/debug/memory` | admin | no | Memory snapshots |
| GET | `/debug/epic-outcomes/{epic_key}` | admin | no | Epic outcome rollup |

---

## Debug — Mutating

| Method | Path | Auth | Mutates | Notes |
|---|---|---|---|---|
| POST | `/debug/repo-mappings` | admin | yes | Creates mapping |
| PUT | `/debug/repo-mappings/{id}` | admin | yes | Updates mapping |
| DELETE | `/debug/repo-mappings/{id}` | admin | yes | Deactivates mapping |
| POST | `/debug/planning-runs/{run_id}/approve` | admin | yes | HTTP APPROVE |
| POST | `/debug/planning-runs/{run_id}/reject` | admin | yes | HTTP REJECT |
| POST | `/debug/epic-outcomes/{epic_key}` | admin | yes | Generate/refresh outcome rollup |
| POST | `/debug/memory` | admin | yes | Create/update manual memory note |
| POST | `/debug/memory/recompute` | admin | yes | Force-refresh derived snapshot |

---

## Debug — Utility

| Method | Path | Auth | Mutates | Notes |
|---|---|---|---|---|
| GET | `/debug/send-telegram` | admin | yes (sends message) | Manual Telegram test |
| GET | `/debug/telegram/set-webhook` | admin | yes (registers webhook) | Register bot webhook URL |

---

## Admin Control (Phase 11)

| Method | Path | Auth | Mutates | Notes |
|---|---|---|---|---|
| GET | `/admin/control-status` | admin | no | Current pause/flag state |
| POST | `/admin/pause` | admin | yes | Pause automation |
| POST | `/admin/resume` | admin | yes | Resume automation |
| GET | `/admin/security-events` | admin | no | Security audit log |
| GET | `/admin/github/branch-protection` | admin | no | Branch protection audit |
