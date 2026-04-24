# AI Dev Orchestrator — Operations Runbook

All curl examples assume the admin key is set:
```bash
export ADMIN_KEY="<your-ADMIN_API_KEY>"
export BASE="https://dev.orchestrator.suyogjoshi.com"  # or prod
```

---

## How to pause automation

Stops new Jira workflows from being dispatched and blocks Telegram approval commands. Running workflows are not killed.

```bash
curl -s -X POST "$BASE/admin/pause" -H "X-Orchestrator-Admin-Key: $ADMIN_KEY"
```

Expected response: `{"paused": true, "message": "..."}`

Verify:
```bash
curl -s "$BASE/admin/control-status" -H "X-Orchestrator-Admin-Key: $ADMIN_KEY"
```

---

## How to resume automation

Re-enables Jira webhook dispatch and Telegram commands.

```bash
curl -s -X POST "$BASE/admin/resume" -H "X-Orchestrator-Admin-Key: $ADMIN_KEY"
```

Expected response: `{"paused": false, "message": "..."}`

---

## How to stop auto-merge only

Set `ALLOW_AUTO_MERGE=false` in `/home/ubuntu/.env.orchestrator` on the VM:

```bash
ssh ubuntu@<VM_IP>
sed -i 's/ALLOW_AUTO_MERGE=.*/ALLOW_AUTO_MERGE=false/' ~/.env.orchestrator
cp ~/.env.orchestrator ~/actions-runner/_work/ai-dev-orchestrator/ai-dev-orchestrator/.env
cd ~/actions-runner/_work/ai-dev-orchestrator/ai-dev-orchestrator
docker compose up -d --force-recreate
```

PRs will still be created and agents will still review, but no auto-merge will occur.

To re-enable: set `ALLOW_AUTO_MERGE=true` and repeat the above.

---

## How to disable GitHub writes entirely

Set `ALLOW_GITHUB_WRITES=false`:

```bash
ssh ubuntu@<VM_IP>
sed -i 's/ALLOW_GITHUB_WRITES=.*/ALLOW_GITHUB_WRITES=false/' ~/.env.orchestrator
cp ~/.env.orchestrator ~/actions-runner/_work/ai-dev-orchestrator/ai-dev-orchestrator/.env
cd ~/actions-runner/_work/ai-dev-orchestrator/ai-dev-orchestrator
docker compose up -d --force-recreate
```

All GitHub operations (push, PR creation, merge, PR comments) will raise RuntimeError and mark runs as FAILED. Use this during GitHub outage or security incident.

---

## How to disable Jira webhook

**Option 1 — Pause orchestrator** (recommended for temporary stops):
```bash
curl -s -X POST "$BASE/admin/pause" -H "X-Orchestrator-Admin-Key: $ADMIN_KEY"
```

**Option 2 — Revoke webhook token** (for permanent/emergency disable):
- Remove or change `JIRA_WEBHOOK_SECRET` in `.env.orchestrator`
- Restart containers
- Jira webhooks with the old token will return 401 and not dispatch

**Option 3 — Disable webhook in Jira UI**:
- Jira Settings → System → Webhooks → disable or delete the webhook

---

## How to disable Telegram commands

Set `ALLOW_TELEGRAM_COMMANDS=false` is not implemented as a DB flag yet (Iteration 5 uses the pause mechanism). To fully disable Telegram command processing:

```bash
curl -s -X POST "$BASE/admin/pause" -H "X-Orchestrator-Admin-Key: $ADMIN_KEY"
```

This also blocks Jira dispatch. For Telegram-only disable, change `TELEGRAM_CHAT_ID` to an invalid value and restart containers — all commands will be rejected as wrong-chat.

---

## How to rotate secrets

### ADMIN_API_KEY
```bash
NEW_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
ssh ubuntu@<VM_IP>
sed -i "s/ADMIN_API_KEY=.*/ADMIN_API_KEY=$NEW_KEY/" ~/.env.orchestrator
cp ~/.env.orchestrator ~/actions-runner/_work/ai-dev-orchestrator/ai-dev-orchestrator/.env
docker compose up -d --force-recreate
# Test with new key:
curl -s "$BASE/admin/control-status" -H "X-Orchestrator-Admin-Key: $NEW_KEY"
```

### JIRA_WEBHOOK_SECRET
```bash
NEW_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
# Update env on VM (same pattern as above)
# Then update the Jira webhook URL:
#   https://your-orchestrator.com/webhooks/jira?token=<NEW_SECRET>
```

### GITHUB_TOKEN / ANTHROPIC_API_KEY
Update in `/home/ubuntu/.env.orchestrator` and restart containers. Verify:
```bash
docker exec ai-dev-orchestrator-app-1 env | grep GITHUB_TOKEN
```

---

## How to recover stale runs

Runs stuck in `RUNNING` state are auto-recovered to `FAILED` on worker restart:

```bash
cd ~/actions-runner/_work/ai-dev-orchestrator/ai-dev-orchestrator
docker compose restart worker
# Worker logs will show: "Recovered N stale RUNNING runs → FAILED"
```

To inspect stale runs before recovery:
```bash
curl -s "$BASE/debug/workflow-runs?status=RUNNING&limit=20" -H "X-Orchestrator-Admin-Key: $ADMIN_KEY"
```

---

## How to inspect security events

```bash
# All recent security events
curl -s "$BASE/admin/security-events?limit=50" -H "X-Orchestrator-Admin-Key: $ADMIN_KEY"

# Filter by type
curl -s "$BASE/admin/security-events?event_type=admin_auth_failed" -H "X-Orchestrator-Admin-Key: $ADMIN_KEY"
curl -s "$BASE/admin/security-events?event_type=webhook_rejected" -H "X-Orchestrator-Admin-Key: $ADMIN_KEY"
curl -s "$BASE/admin/security-events?event_type=github_write_blocked" -H "X-Orchestrator-Admin-Key: $ADMIN_KEY"
```

---

## How to handle a bad auto-merge

If a PR was merged that shouldn't have been:

1. Revert the merge commit on the target repo:
   ```bash
   cd /path/to/sandbox-fastapi-app
   git revert -m 1 <merge-commit-sha>
   git push
   ```

2. Pause the orchestrator to prevent further merges while investigating:
   ```bash
   curl -s -X POST "$BASE/admin/pause" -H "X-Orchestrator-Admin-Key: $ADMIN_KEY"
   ```

3. Inspect the run that triggered the merge:
   ```bash
   curl -s "$BASE/debug/workflow-runs/<run_id>/release-decision" -H "X-Orchestrator-Admin-Key: $ADMIN_KEY"
   curl -s "$BASE/debug/workflow-runs/<run_id>/architecture" -H "X-Orchestrator-Admin-Key: $ADMIN_KEY"
   ```

4. If it was an Architecture Agent false negative, review and adjust prompts in `ARCHITECTURE_PROMPT` in `app/claude_client.py`.

5. Resume when safe:
   ```bash
   curl -s -X POST "$BASE/admin/resume" -H "X-Orchestrator-Admin-Key: $ADMIN_KEY"
   ```

---

## How to verify branch protection

```bash
curl -s "$BASE/admin/github/branch-protection?repo_slug=suyog19/sandbox-fastapi-app&branch=main" \
  -H "X-Orchestrator-Admin-Key: $ADMIN_KEY"
```

Review the `warnings` array. Warnings indicate missing protection rules.

---

## How to redeploy dev

```bash
git push origin dev
# CI/CD deploys automatically via GitHub Actions (self-hosted-dev runner)
# Monitor:
gh run watch --exit-status $(gh run list --branch dev --limit 1 --json databaseId --jq '.[0].databaseId')
# Verify:
curl -s https://dev.orchestrator.suyogjoshi.com/healthz
```

## How to redeploy prod

```bash
# Merge dev → main via GitHub PR, then CI/CD deploys to prod automatically
gh pr create --base main --head dev --title "..."
gh pr merge <PR_NUMBER> --merge
# Monitor prod deployment via self-hosted-prod runner
```
