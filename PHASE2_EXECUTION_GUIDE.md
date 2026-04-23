
# PHASE 2 EXECUTION GUIDE — AI DEV ORCHESTRATOR

## 1. Objective

Phase 2 introduces real development automation.

In Phase 1, you built the backbone.
In Phase 2, the system will:

- Clone repositories
- Create branches
- Use Claude to implement code changes
- Commit and push to GitHub
- Open Pull Requests
- Notify via Telegram

IMPORTANT:
This phase remains controlled and iterative.

---

## 2. Key Principle

Do NOT jump to full autonomy.

Build in this order:
1. Read repo
2. Modify small file
3. Commit safely
4. Create PR
5. Then introduce Claude changes

---

## 3. Iteration Plan

### Iteration 1 — Repo Clone

Goal:
Clone repository using repo mapping.

Acceptance:
Repo is cloned locally and branch checked out.

Verify:
ls /tmp/workflows/<run_id>/

---

### Iteration 2 — Repo Analysis

Goal:
List structure and detect language.

Acceptance:
Telegram message shows summary.

---

### Iteration 3 — File Modification

Goal:
Modify one file safely.

Acceptance:
File updated locally.

---

### Iteration 4 — Commit + Push

Goal:
Push change to GitHub.

Acceptance:
New branch visible on GitHub.

---

### Iteration 5 — PR Creation

Goal:
Create pull request.

Acceptance:
PR exists in GitHub.

---

### Iteration 6 — Claude Read-only

Goal:
Claude summarizes repo.

Acceptance:
Telegram shows summary.

---

### Iteration 7 — Claude Suggestion

Goal:
Claude suggests change.

Acceptance:
Diff generated.

---

### Iteration 8 — Apply Changes

Goal:
Apply Claude changes.

Acceptance:
Files updated correctly.

---

### Iteration 9 — End-to-End Flow

Goal:
Jira → Clone → Claude → Commit → PR

Acceptance:
PR created + Telegram notified.

---

## 4. Security Rules

- Work only inside /tmp/workflows/
- No destructive commands
- No secret access

---

## 5. Done Criteria

Phase complete when:
- Repo clone works
- Commit works
- PR works
- Claude modifies code
- End-to-end flow works
