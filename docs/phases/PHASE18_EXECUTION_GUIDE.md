# PHASE 18 EXECUTION GUIDE — Graduation, Dogfooding & Self-Improvement Readiness

## 1. Objective

Phase 18 is the final build phase.

Goal:

```text
Turn the orchestrator from “platform under construction”
into “product ready for daily use on real projects.”
```

Phase 18 should prepare the system for:

1. improving existing real projects
2. bootstrapping a new project from scratch
3. improving the orchestrator itself through Jira Epic → Story workflow

---

## 2. Phase 18 Scope

### In scope

1. Final readiness checklist
2. Project activation workflow
3. Real repo command normalization
4. Project knowledge refresh
5. New project bootstrap workflow — minimal version
6. Orchestrator self-dogfooding setup
7. Final operational runbook
8. Final “stop building, start using” dashboard marker

### Out of scope

Do NOT build:

* full SaaS product UI
* marketplace features
* advanced RAG
* automatic rollback
* cloud deployment orchestration
* complex project templates
* enterprise auth
* more agents

---

# 3. Key Final Phase Principle

Phase 18 should not add another large subsystem.

It should make the existing system **usable, repeatable, and self-sustaining**.

The question is:

```text
Can Suyog now use this orchestrator for real work without needing more platform-building?
```

---

# 4. Final Target State

After Phase 18:

```text
Existing repo:
  onboard → activate → create Epic → Stories → implement safely

New project:
  create bootstrap Epic → generate repo skeleton → first PR

Orchestrator itself:
  create Epic → decompose → implement improvements through its own pipeline
```

---

# 5. Iteration Plan

## Iteration 0 — Final readiness review baseline

### Goal

Create a final readiness checklist in the repo.

### Tasks

Create:

```text
docs/runbooks/phase18-product-readiness.md
```

Include checklist:

```text
[ ] Jira webhook configured for real project
[ ] Repo onboarded
[ ] Repo mapping active
[ ] Capability profile valid
[ ] Test/build/lint commands available
[ ] Deployment profile configured or intentionally disabled
[ ] Branch protection checked
[ ] Release gate status published
[ ] Admin dashboard accessible
[ ] Pause/resume verified
[ ] First safe PR completed
```

### Acceptance criteria

* document committed
* checklist specific to actual orchestrator setup
* includes `suyogjoshi-com` and Learning Platform placeholders

Then STOP.

---

## Iteration 1 — Project activation workflow

### Goal

Formalize:

```text
onboarded repo → active managed project
```

### Tasks

Add endpoint:

```text
POST /admin/project-onboarding/{repo_slug}/activate
```

Payload:

```json
{
  "jira_project_key": "KAN",
  "base_branch": "main",
  "environment": "dev",
  "auto_merge": false
}
```

Behavior:

1. verify onboarding completed
2. verify capability profile exists
3. create/update repo mapping
4. verify mapping health
5. check branch protection
6. check deployment profile
7. return activation report

### Acceptance criteria

* activation works for `suyogjoshi-com`
* inactive/deactivated duplicate mapping problem does not return
* activation report visible in JSON

Then STOP.

---

## Iteration 2 — Dashboard activation view

### Goal

Make project readiness visible.

### Tasks

Update:

```text
/admin/ui/projects/{repo_slug}
```

Add section:

```text
Project Activation Status
```

Show:

```text
onboarding status
repo mapping status
Jira project key
capability profile
deployment profile
branch protection status
recommended next action
```

Add button:

```text
Activate Project
```

### Acceptance criteria

* operator can tell whether repo is ready
* missing Jira mapping is obvious
* missing branch protection is obvious
* deployment disabled is shown as intentional or warning

Then STOP.

---

## Iteration 3 — Real repo command normalization helper

### Goal

Fix the `generic_unknown` issue for real repos like `suyogjoshi-com`.

Phase 17 showed `suyogjoshi-com` was detected as `generic_unknown` because it is a microservices monorepo without root-level standard commands. 

### Tasks

Add support for repo-level command hints.

Create:

```text
config/repo_command_hints.yaml
```

Example:

```yaml
repos:
  suyog19/suyogjoshi-com:
    profile_name: python_fastapi_monorepo
    test_command: make test
    build_command: make build
    lint_command: make lint
    source_patterns:
      - services/**/app/**/*.py
      - infra/**/*.py
    test_patterns:
      - services/**/tests/**/*.py
```

Add detector override:

```text
if repo_slug has command hints:
    apply hints after detection
```

### Acceptance criteria

* hint file can override/augment detected profile
* no arbitrary Jira-provided commands
* dashboard shows “profile source: detected / configured hint”
* existing profiles unaffected

Then STOP.

---

## Iteration 4 — Makefile recommendation generator

### Goal

Help repos become orchestrator-friendly.

### Tasks

Add onboarding recommendation:

If repo lacks root-level commands, suggest a `Makefile`.

Suggested targets:

```makefile
test:
	...

build:
	...

lint:
	...
```

Do **not** auto-commit Makefile unless a Story asks for it.

### Acceptance criteria

* onboarding retrospective recommends Makefile when needed
* project dashboard shows recommendation
* no repo mutation during onboarding

Then STOP.

---

## Iteration 5 — Project knowledge refresh implementation

### Goal

Complete placeholder endpoint from Phase 17.

Existing endpoint:

```text
POST /debug/project-knowledge/{repo_slug}/refresh
```

is currently placeholder. 

### Tasks

Implement refresh behavior:

1. create new onboarding run or refresh run
2. clone repo
3. re-run structure scan
4. regenerate architecture snapshot
5. regenerate coding conventions snapshot
6. update deployment snapshot
7. store previous snapshot history or overwrite with updated timestamp

Recommended simple approach:

```text
reuse project_onboarding pipeline with mode=refresh
```

### Acceptance criteria

* refresh endpoint actually refreshes snapshots
* dashboard shows updated timestamp
* old workflow unaffected

Then STOP.

---

## Iteration 6 — Safe first-use mode

### Goal

Add conservative defaults for newly activated real projects.

For first N runs of a repo:

```text
auto_merge=false
max_changed_files=3
deployment validation observational
clarification preferred on ambiguity
release gate still runs
```

Add config:

```text
FIRST_USE_MODE_ENABLED=true
FIRST_USE_RUN_COUNT=3
```

### Acceptance criteria

* newly activated repo starts conservative
* dashboard shows “First-use mode active”
* after N successful runs, still requires manual toggle to enable auto-merge

Then STOP.

---

## Iteration 7 — New project bootstrap minimal workflow

### Goal

Support a simple “build from scratch” starting point.

Add workflow:

```text
project_bootstrap
```

Triggered manually first, not from Jira automatically.

Endpoint:

```text
POST /admin/project-bootstrap/start
```

Payload:

```json
{
  "repo_slug": "suyog19/new-project",
  "project_type": "python_fastapi | node_react | static_site",
  "base_branch": "main",
  "description": "..."
}
```

Behavior:

1. validate repo exists and is empty or nearly empty
2. create bootstrap branch
3. generate minimal skeleton
4. add README
5. add basic test
6. commit and open PR
7. do not auto-merge

### Acceptance criteria

* works for one project type first, preferably `static_site` or `python_fastapi`
* creates PR, not direct push to main
* agents review PR
* release gate runs
* no deployment automation

Then STOP.

---

## Iteration 8 — Bootstrap templates

### Goal

Keep bootstrap deterministic.

Create templates:

```text
templates/bootstrap/static_site/
templates/bootstrap/python_fastapi/
```

Initial minimal static site:

```text
README.md
index.html
styles.css
```

Initial Python FastAPI:

```text
README.md
app/main.py
tests/test_health.py
requirements.txt
```

### Acceptance criteria

* template files are deterministic
* Claude may customize README/description
* skeleton does not depend on vague generation

Then STOP.

---

## Iteration 9 — Orchestrator self-dogfooding setup

### Goal

Prepare this orchestrator repo to improve itself using Jira Epic → Story.

Tasks:

1. onboard orchestrator repo itself
2. create capability profile
3. create project knowledge snapshots
4. create Jira mapping for orchestrator project key
5. keep auto_merge=false
6. branch protection check
7. first dogfooding Epic recommendation

Suggested first dogfooding Epic:

```text
Improve project knowledge refresh UX and documentation
```

or

```text
Add pagination to admin dashboard list pages
```

### Acceptance criteria

* orchestrator repo onboarded
* dashboard shows it as active project
* first improvement Epic can be created manually
* no self-auto-merge

Then STOP.

---

## Iteration 10 — Dogfooding safety guard

### Goal

Prevent dangerous self-modification.

For orchestrator repo:

```text
auto_merge=false always unless explicitly overridden
max_changed_files=3
deployment_validation optional
manual review required
self_modification=true flag
```

Add policy:

```text
if repo_slug == orchestrator_repo:
    force manual merge
```

### Acceptance criteria

* self-dogfooding cannot auto-merge by default
* release gate may approve but merge skipped by policy
* dashboard clearly shows self-modification guard

Then STOP.

---

## Iteration 11 — Final usage runbook

### Goal

Create final operator guide.

Create:

```text
docs/runbooks/using-orchestrator-for-real-projects.md
```

Sections:

1. How to onboard a repo
2. How to activate repo
3. How to create first Epic
4. How to approve generated Stories
5. How to review PR
6. How to handle clarification
7. How to handle blocked release
8. How to run deployment validation
9. How to refresh project knowledge
10. How to use orchestrator to improve itself

### Acceptance criteria

* guide is practical
* includes dashboard paths
* includes safe first-use advice

Then STOP.

---

## Iteration 12 — Final E2E validation

### Required scenarios

#### Scenario A — Existing project activation

```text
suyogjoshi-com or Learning Platform repo
→ onboarding exists
→ activate
→ mapping health passes
```

#### Scenario B — Knowledge refresh

```text
refresh project knowledge
→ snapshot timestamps update
→ dashboard reflects update
```

#### Scenario C — Safe real Story

```text
create small Story
→ implementation PR
→ agents run
→ release gate runs
→ auto-merge skipped due first-use/manual policy
```

#### Scenario D — New project bootstrap

```text
empty repo
→ bootstrap workflow
→ PR created with skeleton
→ agents run
→ no auto-merge
```

#### Scenario E — Self-dogfooding readiness

```text
orchestrator repo onboarded
→ mapping configured
→ self-modification guard visible
```

### Acceptance criteria

* all scenarios pass
* no platform-breaking regression
* dashboard supports daily use
* final runbook complete

Then STOP.

---

# 6. Final Definition of Done

Phase 18 is complete when:

* real project activation exists
* project dashboard shows activation readiness
* repo command hints are supported
* project knowledge refresh works
* first-use safety mode exists
* minimal project bootstrap works
* orchestrator repo can be onboarded for self-dogfooding
* self-modification guard prevents auto-merge
* final usage runbook exists
* one real project is ready for normal Epic → Story use

---

# 7. Final Instruction to Claude

Build Phase 18 as the **graduation phase**.

Do not add more platform ambition.

The goal is:

```text
Suyog can now stop building the orchestrator
and start using it safely on real projects.
```

Optimize for:

* real project readiness
* safe first use
* project activation
* refreshable knowledge
* minimal bootstrap
* self-dogfooding with guardrails
* clear runbooks

Do not optimize for:

* more agents
* more dashboards
* large greenfield generation
* auto-merge for real projects
* broad RAG
* deployment automation

The final standard:

> After Phase 18, further improvements to this orchestrator should happen through the orchestrator itself using Jira Epic → Story workflow.
