
---

# PHASE 15 EXECUTION GUIDE — Multi-Stack Capability Profiles

## 1. Objective

Add a **Repo Capability Profile** layer so the orchestrator can detect, store, and use stack-specific behavior.

The orchestrator should know:

```text
What kind of repo is this?
How do I install dependencies?
How do I run tests?
How do I run lint/build?
What files are source vs test?
What context selection strategy should I use?
Can auto-merge be allowed?
```

---

## 2. Target Capabilities

Support these profiles initially:

```text
python_fastapi
java_maven
java_gradle
node_react
generic_unknown
```

Phase 15 should not fully perfect every stack. It should create the framework and add practical first support.

---

## 3. Key Design Principle

Capability detection must be explicit and conservative.

If repo type is unknown:

```text
profile = generic_unknown
tests = NOT_RUN
auto_merge = disabled
release_gate = skipped/manual review required
```

No guessing bravely. Brave guessing is how machines become interns with root access.

---

# 4. Data Model Changes

## 4.1 New table: `repo_capability_profiles`

```sql
id SERIAL PRIMARY KEY,
repo_slug VARCHAR(200) NOT NULL,
profile_name VARCHAR(100) NOT NULL,
primary_language VARCHAR(50),
framework VARCHAR(100),
package_manager VARCHAR(100),
test_command TEXT,
build_command TEXT,
lint_command TEXT,
source_patterns_json TEXT,
test_patterns_json TEXT,
capabilities_json TEXT,
auto_detected BOOLEAN DEFAULT TRUE,
is_active BOOLEAN DEFAULT TRUE,
created_at TIMESTAMP DEFAULT NOW(),
updated_at TIMESTAMP DEFAULT NOW()
```

Example `capabilities_json`:

```json
{
  "supports_tests": true,
  "supports_lint": true,
  "supports_build": true,
  "supports_import_graph": true,
  "supports_auto_merge": true
}
```

## 4.2 Extend `repo_mappings`

Add:

```text
capability_profile_id
```

or allow lookup by `repo_slug`.

Recommendation:

```text
Lookup active capability profile by repo_slug.
```

Keep `repo_mappings` simple for now.

## 4.3 Extend `workflow_runs`

Add:

```text
capability_profile_name
build_status
lint_status
dependency_install_status
```

---

# 5. Detection Rules

## 5.1 Python / FastAPI

Detect if repo has:

```text
pyproject.toml
requirements.txt
app/main.py
fastapi in requirements/pyproject
tests/
pytest.ini
```

Profile:

```text
python_fastapi
```

Commands:

```text
test: pytest -q
lint: optional initially
build: none initially
```

## 5.2 Java Maven

Detect:

```text
pom.xml
src/main/java
src/test/java
```

Profile:

```text
java_maven
```

Commands:

```text
test: mvn test
build: mvn package -DskipTests
```

## 5.3 Java Gradle

Detect:

```text
build.gradle
settings.gradle
gradlew
src/main/java
```

Profile:

```text
java_gradle
```

Commands:

```text
test: ./gradlew test
build: ./gradlew build
```

## 5.4 Node / React

Detect:

```text
package.json
src/
vite.config.*
next.config.*
react dependency
```

Profile:

```text
node_react
```

Commands:

```text
test: npm test -- --run
build: npm run build
lint: npm run lint
```

Only run commands if scripts exist.

## 5.5 Unknown

Fallback:

```text
generic_unknown
```

Commands:

```text
test: none
build: none
lint: none
auto_merge: false
```

---

# 6. New Services

## 6.1 Repo profile detector

Create:

```python
detect_repo_capability_profile(workspace_path, repo_slug) -> dict
```

Returns:

```json
{
  "profile_name": "java_maven",
  "primary_language": "java",
  "framework": "spring_boot",
  "package_manager": "maven",
  "test_command": "mvn test",
  "build_command": "mvn package -DskipTests",
  "lint_command": null,
  "source_patterns": ["src/main/java/**/*.java"],
  "test_patterns": ["src/test/java/**/*.java"],
  "capabilities": {
    "supports_tests": true,
    "supports_build": true,
    "supports_lint": false,
    "supports_auto_merge": false
  }
}
```

## 6.2 Command runner abstraction

Create:

```python
run_repo_command(workspace_path, command, timeout_seconds, profile_name) -> dict
```

Returns:

```json
{
  "status": "PASSED|FAILED|NOT_RUN|ERROR",
  "command": "...",
  "exit_code": 0,
  "output_excerpt": "..."
}
```

## 6.3 File classifier abstraction

Create:

```python
classify_repo_files(files, profile) -> dict
```

Should classify:

```text
source_files
test_files
config_files
docs_files
build_files
unknown_files
```

---

# 7. Update Existing Workflow Areas

## 7.1 Test discovery

Replace Python-only detection with profile-based detection.

Current:

```text
pytest-centric
```

New:

```text
profile.test_command
```

If no test command:

```text
test_status = NOT_RUN
```

## 7.2 Test Quality Agent package

Make test file detection profile-aware.

Examples:

```text
Python: tests/, test_*.py
Java: src/test/java/**/*.java
Node: *.test.tsx, *.spec.ts, __tests__/
```

## 7.3 Architecture Agent package

Use profile-aware file classification.

Examples:

```text
Java:
- controller
- service
- repository
- entity
- config

Node React:
- component
- hook
- route
- state
- test
```

## 7.4 Release Gate

Auto-merge should be disabled unless profile explicitly allows it.

Initial recommendation:

```text
python_fastapi: allow existing behavior
java_maven: no auto-merge initially
java_gradle: no auto-merge initially
node_react: no auto-merge initially
generic_unknown: no auto-merge
```

---

# 8. Iteration Plan

## Iteration 0 — Capability profile schema

### Goal

Add data model and constants.

### Tasks

* Add `repo_capability_profiles`
* Add workflow columns:

  * `capability_profile_name`
  * `build_status`
  * `lint_status`
  * `dependency_install_status`
* Add constants:

  * `python_fastapi`
  * `java_maven`
  * `java_gradle`
  * `node_react`
  * `generic_unknown`

### Acceptance criteria

* migrations idempotent
* existing Python workflow unaffected

Then STOP.

---

## Iteration 1 — Repo profile detector

### Goal

Detect repo type from cloned workspace.

### Tasks

* Implement `detect_repo_capability_profile()`
* Add detection rules for all five profiles
* Add safe fallback to `generic_unknown`
* Add unit/static tests using fake folder structures

### Acceptance criteria

* Python/FastAPI detected
* Maven detected
* Gradle detected
* Node/React detected
* unknown fallback works

Then STOP.

---

## Iteration 2 — Store and expose capability profile

### Goal

Persist detected profile per repo/run.

### Tasks

* After repo clone, detect profile
* Store/update active profile in `repo_capability_profiles`
* Store `capability_profile_name` on `workflow_runs`
* Add admin UI display on run detail page
* Add API:

```text
GET /debug/repo-capability-profiles
GET /debug/repo-capability-profiles/{repo_slug}
```

### Acceptance criteria

* run detail shows profile
* DB has profile row
* API returns active profile

Then STOP.

---

## Iteration 3 — Profile-based test command selection

### Goal

Replace hardcoded pytest assumption.

### Tasks

* Create `get_test_command_for_profile(profile)`
* Update test execution to use profile command
* If command unavailable, mark `NOT_RUN`
* Preserve existing Python behavior

### Acceptance criteria

* Python still runs `pytest -q`
* Maven returns `mvn test`
* Gradle returns `./gradlew test`
* Node only runs if test script exists
* unknown marks `NOT_RUN`

Then STOP.

---

## Iteration 4 — Safe command execution abstraction

### Goal

Centralize running tests/build/lint.

### Tasks

* Implement `run_repo_command()`
* Add timeout
* capture stdout/stderr
* truncate output
* prevent shell injection
* use argument list where possible

### Acceptance criteria

* command timeout works
* failure output captured
* no unsafe arbitrary shell execution
* existing pytest flow uses this helper

Then STOP.

---

## Iteration 5 — Java Maven support

### Goal

Add first non-Python test support.

### Tasks

* Create/choose a small Maven sandbox repo
* Add repo mapping
* Detect `java_maven`
* Run `mvn test`
* Capture result
* Feed result into existing agents/release gate

### Acceptance criteria

* Maven repo detected
* `mvn test` runs
* test output stored
* auto-merge disabled initially for Java
* PR still created and reviewed

Then STOP.

---

## Iteration 6 — Java Gradle support

### Goal

Add Gradle test support.

### Tasks

* Detect Gradle repo
* Prefer `./gradlew test`
* fallback to `gradle test` only if documented
* handle permission issue:

  * if `gradlew` not executable, run `chmod +x gradlew` only inside workspace
* Capture test output

### Acceptance criteria

* Gradle repo detected
* Gradle test command works or fails clearly
* no auto-merge initially

Then STOP.

---

## Iteration 7 — Node/React support

### Goal

Add Node/React command support.

### Tasks

* Detect package manager:

  * pnpm-lock.yaml → pnpm
  * yarn.lock → yarn
  * package-lock.json → npm
  * default npm
* Read package.json scripts
* Select:

  * test command if `test` script exists
  * build command if `build` script exists
  * lint command if `lint` script exists
* Do not run missing scripts

### Acceptance criteria

* React repo detected
* package manager detected
* test/build/lint commands selected only when scripts exist
* missing test script → `NOT_RUN`

Then STOP.

---

## Iteration 8 — Build and lint support

### Goal

Add optional build/lint pipeline.

### Tasks

For supported profiles:

* run tests first
* if tests pass, run lint if configured
* if lint passes or not configured, run build if configured
* store:

  * `lint_status`
  * `build_status`

Release Gate update:

* failed build should block
* failed lint should skip or block based on config

Initial policy:

```text
build failure = RELEASE_BLOCKED
lint failure = RELEASE_SKIPPED
```

### Acceptance criteria

* Python unchanged if no build/lint
* Node build can run
* Java build can run
* statuses visible in run detail

Then STOP.

---

## Iteration 9 — Profile-aware file classification

### Goal

Improve agent context for multiple stacks.

### Tasks

Update file classification for:

## Java

```text
controller
service
repository
entity/model
config
test
build
```

## Node/React

```text
component
hook
route
state
api_client
test
config
build
```

## Python

Keep existing:

```text
api
model
storage
config
test
doc
```

Use classification in:

* Architecture Agent package
* Test Quality Agent package
* Reviewer Agent package if useful

### Acceptance criteria

* architecture package shows meaningful file categories per stack
* agents receive stack-aware context
* no prompt explosion

Then STOP.

---

## Iteration 10 — Profile-aware Test Quality Agent

### Goal

Make Test Quality Agent understand non-Python test conventions.

### Tasks

Update Test Quality package:

* Java test file detection
* Node test file detection
* skipped-test detection:

  * JUnit: `@Disabled`
  * Jest/Vitest: `.skip`, `describe.skip`, `it.skip`, `test.skip`
  * pytest existing

Update Test Quality prompt:

* mention stack/profile
* evaluate tests according to framework conventions

### Acceptance criteria

* Java tests recognized
* Node tests recognized
* skipped tests detected across stacks
* existing Python behavior unchanged

Then STOP.

---

## Iteration 11 — Release Gate profile policy

### Goal

Make release decisions stack-aware.

Add config per profile:

```json
{
  "python_fastapi": {
    "allow_auto_merge": true,
    "require_tests": true,
    "require_build": false,
    "require_lint": false
  },
  "java_maven": {
    "allow_auto_merge": false,
    "require_tests": true,
    "require_build": true,
    "require_lint": false
  },
  "node_react": {
    "allow_auto_merge": false,
    "require_tests": false,
    "require_build": true,
    "require_lint": false
  }
}
```

### Tasks

* Add profile policy resolver
* Update Release Gate to consider profile policy
* Add reason/warning when auto-merge blocked by profile policy

### Acceptance criteria

* Java/Node do not auto-merge initially
* release decision explains profile policy
* Python behavior unchanged

Then STOP.

---

## Iteration 12 — Dashboard integration

### Goal

Expose profile data in admin UI.

Update dashboard pages:

## Run detail

Show:

* capability profile
* primary language
* framework
* package manager
* test/build/lint commands
* test/build/lint status

## GitHub/repo page

Show:

* repo profiles
* profile detection result
* last detected timestamp

Add page:

```text
/admin/ui/repos
```

Optional if time allows.

### Acceptance criteria

* run detail shows profile info
* operator can see why tests/build/lint did or did not run
* no secrets exposed

Then STOP.

---

## Iteration 13 — E2E validation

### Required scenarios

#### Scenario A — Existing Python repo

Expected:

```text
profile=python_fastapi
pytest runs
existing full workflow still works
```

#### Scenario B — Maven repo

Expected:

```text
profile=java_maven
mvn test runs
PR created
agents run
auto-merge skipped by profile policy
```

#### Scenario C — Gradle repo

Expected:

```text
profile=java_gradle
gradle test runs or fails clearly
auto-merge skipped
```

#### Scenario D — Node/React repo

Expected:

```text
profile=node_react
scripts detected
test/build run if available
auto-merge skipped
```

#### Scenario E — Unknown repo

Expected:

```text
profile=generic_unknown
tests NOT_RUN
release skipped/manual required
no crash
```

#### Scenario F — Bad command

Expected:

```text
command failure captured
workflow ends honestly
release blocked/skipped according to policy
```

### Acceptance criteria

* no regression in existing Python path
* all new profiles detected correctly
* agents receive stack-aware context
* dashboard displays profile info
* release decisions are conservative

Then STOP.

---

# 9. Security Notes

* Commands must only run inside cloned workspace
* No arbitrary command from Jira text
* Use predefined commands from profile
* Apply timeout to all commands
* Do not log secrets or env values
* Auto-merge disabled for new stacks until confidence is built

---

# 10. Definition of Done

Phase 15 is complete when:

* capability profiles exist
* Python path still works
* Java Maven repo can run tests
* Java Gradle repo can run tests or fail clearly
* Node/React repo can detect scripts and run supported commands
* unknown repo fails safely
* Test Quality and Architecture Agents receive stack-aware context
* Release Gate is profile-aware
* dashboard shows profile/test/build/lint info
* no auto-merge for new stacks by default

---

# 11. Final Instruction to Claude

Build Phase 15 as a **capability expansion framework**, not a pile of one-off scripts.

The goal is:

```text
Before touching a repo,
the orchestrator knows what kind of repo it is,
what it can safely do,
and what it must refuse or skip.
```

Optimize for:

* conservative detection
* profile-based behavior
* safe command execution
* no regression in Python path
* clear operator visibility

Do not optimize for:

* supporting every framework perfectly
* auto-merging new stacks immediately
* running arbitrary commands
* complex plugin architecture too early

The standard for Phase 15:

> The orchestrator should safely understand and operate on Python, Java, Node, and unknown repos without pretending they are all the same.
