# Token and Secret Permission Requirements

Minimum required permissions for each credential used by the AI Dev Orchestrator.

---

## GitHub Token (`GITHUB_TOKEN`)

**Type:** Personal Access Token (classic) or Fine-Grained PAT

**Minimum required scopes (classic PAT):**

| Scope | Purpose |
|---|---|
| `repo` | Clone, push, create PRs, merge, read/write issues for labels and comments |

If using a Fine-Grained PAT, the minimum repository permissions are:

| Permission | Level |
|---|---|
| Contents | Read and write (clone, push) |
| Pull requests | Read and write (create, merge, add labels) |
| Issues | Read and write (add labels) |
| Metadata | Read |

**Avoid:**
- Organization-wide access
- `admin:repo` (repo admin)
- `admin:org`
- `delete_repo`

**Storage:** env var `GITHUB_TOKEN` — never logged, never committed.

---

## Jira API Token (`JIRA_API_TOKEN`)

**Type:** Jira API Token (user-scoped, not OAuth app)

**Minimum required:**

| Permission | Purpose |
|---|---|
| Browse projects | Read issue details (summary, description, AC) |
| Create issues | Create Story children from epic_breakdown |
| Edit issues | Transition issue status if needed |
| View read-only fields | Read acceptance criteria, epic link |

**Avoid:**
- Project admin
- Global admin
- Bulk delete

**Storage:** env vars `JIRA_EMAIL` + `JIRA_API_TOKEN` — never logged.

---

## Telegram Bot Token (`TELEGRAM_BOT_TOKEN`)

**Type:** Bot token issued by @BotFather

**Minimum required:**
- Can send messages to the configured chat (`TELEGRAM_CHAT_ID`)
- Can receive webhook updates from Telegram

**Lock-down:**
- Configure bot to only accept messages (no inline mode, no group admin)
- `TELEGRAM_CHAT_ID` must be the private chat or group ID — only messages from this ID are processed

**Storage:** env var `TELEGRAM_BOT_TOKEN` — never logged.

---

## Anthropic API Key (`ANTHROPIC_API_KEY`)

**Type:** Anthropic API key

**Minimum required:**
- API call access (claude-sonnet-4-6)
- No additional scopes — key is scoped to API calls only

**Avoid:**
- Logging key values anywhere in code
- Including key in PR bodies, commit messages, or Telegram notifications

**Storage:** env var `ANTHROPIC_API_KEY` — never logged.

---

## Admin API Key (`ADMIN_API_KEY`)

**Type:** Shared secret, self-generated

**Purpose:** Protects all `/debug/*` and `/admin/*` HTTP endpoints.

**Requirements:**
- Must be at least 32 random hex characters (generated with `python3 -c "import secrets; print(secrets.token_hex(32))"`)
- Rotate if compromised
- Never share in logs, commits, or Telegram messages

**Storage:** env var `ADMIN_API_KEY` on each VM in `/home/ubuntu/.env.orchestrator`.

---

## Jira Webhook Secret (`JIRA_WEBHOOK_SECRET`)

**Type:** Shared secret, self-generated

**Purpose:** Validates that `/webhooks/jira` requests originate from the configured Jira instance.

**Configuration:** Add as a query param to the Jira webhook URL:
```
https://your-orchestrator.com/webhooks/jira?token=<JIRA_WEBHOOK_SECRET>
```

**Requirements:**
- At least 32 random characters
- Configure in Jira's webhook settings as part of the URL

**Storage:** env var `JIRA_WEBHOOK_SECRET` — never logged.

---

## Rotation Procedure

1. Generate a new secret value
2. Update `/home/ubuntu/.env.orchestrator` on the VM
3. Run `cp /home/ubuntu/.env.orchestrator .env && docker compose up -d --force-recreate`
4. Verify the new secret works via `GET /admin/control-status` with new key (for ADMIN_API_KEY)
5. For JIRA_WEBHOOK_SECRET: update the Jira webhook URL with the new token
6. Verify no security_events with `webhook_rejected` appear for valid Jira triggers
