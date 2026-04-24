# GitHub Required Status Checks — Phase 13

This document explains how to configure GitHub branch protection to enforce the orchestrator's release gate on the sandbox repo.

## Why This Matters

The orchestrator evaluates five gates before merging a PR:

- `orchestrator/tests` — pytest outcome
- `orchestrator/reviewer-agent` — Reviewer Agent verdict
- `orchestrator/test-quality-agent` — Test Quality Agent verdict
- `orchestrator/architecture-agent` — Architecture Agent verdict
- `orchestrator/release-gate` — unified release decision (aggregates all four above)

Without branch protection, a human can manually merge a PR even when the orchestrator says it is unsafe. Requiring `orchestrator/release-gate` as a status check prevents accidental bypass.

## Recommended Configuration

Require **only one** context:

```
orchestrator/release-gate
```

The release gate already aggregates all other agent verdicts. Requiring it is sufficient — you do not need to require the individual agent contexts separately.

If you want maximum visibility in the GitHub PR UI (each agent shown separately), you may also require the individual contexts, but this is optional.

## How to Configure in GitHub UI

1. Go to **Settings → Branches** on the target repo (e.g., `suyog19/sandbox-fastapi-app`).
2. Click **Add branch protection rule** (or edit an existing rule for `main`).
3. Under **Protect matching branches**, set **Branch name pattern** to `main`.
4. Enable **Require status checks to pass before merging**.
5. Enable **Require branches to be up to date before merging**.
6. In the search box, type `orchestrator/release-gate` and select it.
7. Save the rule.

After the first real run completes and publishes statuses, `orchestrator/release-gate` will appear in the search suggestions.

## Verifying the Configuration

Use the admin endpoint:

```bash
curl -H "X-Orchestrator-Admin-Key: <key>" \
  "https://dev.orchestrator.suyogjoshi.com/admin/github/branch-protection?repo_slug=suyog19/sandbox-fastapi-app&branch=main"
```

Expected response when correctly configured:

```json
{
  "protected": true,
  "orchestrator_check_status": {
    "release_gate_required": true,
    "missing_required": [],
    "optional_configured": []
  },
  "warnings": []
}
```

If `release_gate_required` is `false`, the `warnings` array will contain a `CRITICAL` message.

## Status Context Reference

| Context | What it checks |
|---|---|
| `orchestrator/release-gate` | Final unified release decision — **require this one** |
| `orchestrator/tests` | pytest outcome for the PR branch |
| `orchestrator/reviewer-agent` | Reviewer Agent code review verdict |
| `orchestrator/test-quality-agent` | Test Quality Agent verdict |
| `orchestrator/architecture-agent` | Architecture Agent verdict |

## Validate Required Checks via API

The endpoint `POST /admin/github/branch-protection/validate-required-checks` returns a dry-run assessment without mutating anything:

```bash
curl -X POST \
  -H "X-Orchestrator-Admin-Key: <key>" \
  -H "Content-Type: application/json" \
  -d '{"repo_slug": "suyog19/sandbox-fastapi-app", "branch": "main"}' \
  "https://dev.orchestrator.suyogjoshi.com/admin/github/branch-protection/validate-required-checks"
```

## Warning: Manual Merge Bypass

If branch protection is **not** configured with `orchestrator/release-gate`:

- Any repository contributor with merge rights can bypass the AI release gate.
- GitHub will show a PR as mergeable even if the orchestrator marked it blocked.
- The orchestrator's `merge_status` in the DB will reflect the truth, but GitHub will not prevent the merge.

Configure branch protection before promoting to production use.
