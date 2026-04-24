
---

# Phase 15 Summary — Multi-Stack Capability Profiles

## Objective

Add a **Repo Capability Profile** layer so the orchestrator can detect, store, and apply stack-specific behavior for Python, Java (Maven/Gradle), Node/React, and unknown repos — without pretending they are all the same.

---

## What Was Built

### New Files

| File | Purpose |
|---|---|
| `app/repo_profiler.py` | Detects repo stack from cloned workspace; returns structured profile dict |
| `app/command_runner.py` | Safe, injection-proof command execution (`run_repo_command()`) using `shlex.split()` |
| `docs/phases/PHASE15_EXECUTION_GUIDE.md` | Iteration-by-iteration execution guide |

### Modified Files

| File | Changes |
|---|---|
| `app/feedback.py` | Added `CapabilityProfile`, `CommandStatus`, `BuildStatus`, `LintStatus`, `DependencyInstallStatus`, `FeedbackTypeP15` constants |
| `app/database.py` | Added `repo_capability_profiles` table; Phase 15 columns on `workflow_runs`; `upsert_capability_profile()`, `get_active_capability_profile()`, `list_capability_profiles()` |
| `app/test_runner.py` | Profile-aware: `run_tests()` now takes profile command/name; added `run_build()`, `run_lint()` delegating to `run_repo_command()` |
| `app/workflows.py` | Profile detection after clone; build/lint pipeline; profile-aware `_classify_changed_files()`; profile-aware `_detect_skipped_tests()`; `_PROFILE_RELEASE_POLICY` dict; `evaluate_release_decision()` now profile-aware |
| `app/claude_client.py` | `review_test_quality()` includes stack/profile context (`_STACK_TEST_HINTS`) in user message |
| `app/main.py` | Added `/debug/repo-capability-profiles` and `/debug/repo-capability-profiles/{repo_slug}` endpoints |
| `app/ui.py` | Run detail route loads capability profile and passes to template |
| `app/database.py` | `get_workflow_run_detail()` adds `repo_slug` subquery from `agent_reviews` |
| `app/templates/admin/run_detail.html` | Capability Profile card showing profile, language, framework, package manager, commands, auto-merge policy |
| `Dockerfile` | Added Java 21, Maven 3.9.9, Gradle 8.5, Node 20, npm to container image |

---

## Capability Profiles

| Profile | Detected by | Test command | Build | Lint | Auto-merge |
|---|---|---|---|---|---|
| `python_fastapi` | `requirements.txt`/`pyproject.toml` + FastAPI or `app/main.py` | `pytest -q` | — | — | Allowed |
| `java_maven` | `pom.xml` | `mvn test -q` | `mvn package -DskipTests` | — | Disabled |
| `java_gradle` | `build.gradle` or `gradlew`+`settings.gradle` | `./gradlew test` or `gradle test` | `./gradlew build` | — | Disabled |
| `node_react` | `package.json` + vite/next config or react dep | From `package.json` scripts | From `package.json` scripts | From `package.json` scripts | Disabled |
| `generic_unknown` | fallback | None | None | None | Disabled |

**Detection order:** Gradle > Maven > Node > Python > Unknown (conservative, explicit, no guessing).

---

## Release Gate Profile Policies

`_PROFILE_RELEASE_POLICY` in `workflows.py`:

| Profile | allow_auto_merge | require_tests | require_build |
|---|---|---|---|
| `python_fastapi` | True | True | False |
| `java_maven` | False | True | True |
| `java_gradle` | False | True | True |
| `node_react` | False | False | True |
| `generic_unknown` | False | False | False |

- Build `FAILED` = `RELEASE_BLOCKED` (hard block, all profiles)
- Lint `FAILED` = `RELEASE_SKIPPED` (soft skip, all profiles)
- Build `NOT_RUN` = `RELEASE_SKIPPED` when `require_build=True`
- Tests `NOT_RUN` = acceptable when `require_tests=False` (node_react, generic_unknown)

---

## Profile-Aware File Classification

`_classify_changed_files(files, profile_name)` dispatches to stack-specific classifiers:

| Profile | Groups |
|---|---|
| Python | api, model, storage, config, test, docs, other |
| Java | controller, service, repository, entity, config, test, build, other |
| Node | component, hook, route, state, api, config, test, build, other |

Used in Architecture Agent (`diff_context.file_classification`) and Test Quality Agent (accurate source/test split).

---

## Profile-Aware Skip Detection

`_detect_skipped_tests(diff, output, profile_name)`:

| Profile | Patterns |
|---|---|
| Python | `@pytest.mark.skip`, `pytest.skip(`, `skipTest(` |
| Java | `@Disabled`, `@Ignore`, `@IgnoreRest` |
| Node | `it.skip(`, `test.skip(`, `describe.skip(`, `xtest(`, `xdescribe(`, `.todo(` |

The `'skipped'` keyword in test output is caught regardless of profile.

---

## Test Quality Agent (Iteration 10)

`review_test_quality()` now includes stack-aware context in the user message:

```python
_STACK_TEST_HINTS = {
    "java_maven":   "JUnit 4/5 (annotations: @Test, @Disabled, @Ignore; assertions: assertEquals, assertThat)",
    "java_gradle":  "JUnit 4/5 ...",
    "node_react":   "Jest or Vitest (it(), test(), describe(), expect(); skip: it.skip, xtest, describe.skip)",
    "python_fastapi": "pytest (def test_*, @pytest.mark.skip, pytest.raises, assert)",
}
```

---

## Sandbox Repos Created

| Repo | Stack | Tests |
|---|---|---|
| `suyog19/sandbox-java-maven` | Java 17, JUnit 4.13.2, Maven | 2 JUnit tests (testGreet, testAdd) |
| `suyog19/sandbox-java-gradle` | Java 17, JUnit 4.13.2, Gradle 8.5 | 2 JUnit tests (testGreet, testMultiply) |
| `suyog19/sandbox-node-react` | React 18, Vitest, Vite | 2 Vitest tests (greet, add) |

---

## Iterations Completed

| Iteration | What |
|---|---|
| 0 | Schema: `repo_capability_profiles` table, Phase 15 columns on `workflow_runs`, constants in `feedback.py` |
| 1 | Repo profile detector: `detect_repo_capability_profile()` with all 5 profiles |
| 2 | Store and expose: upsert to DB, `capability_profile_name` on run, API endpoints |
| 3 | Profile-based test command selection: `get_test_command_for_profile()`, `run_tests()` profile-aware |
| 4 | Safe command execution abstraction: `run_repo_command()` (shlex.split, timeout, no shell=True) |
| 5 | Java Maven support: sandbox repo, detection, `mvn test` execution validated |
| 6 | Java Gradle support: sandbox repo, gradlew/gradle fallback, `gradle test` validated |
| 7 | Node/React support: sandbox repo, package manager detection, vitest via npm test validated |
| 8 | Build and lint pipeline: `run_build()`, `run_lint()`, build/lint in workflow after tests, release gate updated |
| 9 | Profile-aware file classification: Java/Node/Python-specific groups, classification in agent packages |
| 10 | Profile-aware Test Quality Agent: stack-aware skip patterns, stack hint in TQ user message |
| 11 | Release Gate profile policy: `_PROFILE_RELEASE_POLICY`, evaluate_release_decision() profile-aware |
| 12 | Dashboard integration: run detail loads and displays full capability profile card |
| 13 | E2E validation: all 6 scenarios (Python, Maven, Gradle, Node, Unknown, BadCommand) pass on EC2 |

---

## E2E Validation Results (Iteration 13, EC2)

| Scenario | Result |
|---|---|
| A: Python FastAPI | python_fastapi detected; pytest PASSED; RELEASE_APPROVED + can_auto_merge |
| B: Java Maven | java_maven detected; mvn test PASSED; RELEASE_SKIPPED (profile policy) |
| C: Java Gradle | java_gradle detected; gradle test PASSED; profile policy blocks auto-merge |
| D: Node/React | node_react detected; npm test PASSED; tests NOT_RUN acceptable per policy |
| E: Unknown repo | generic_unknown detected; tests NOT_RUN; RELEASE_SKIPPED (conservative) |
| F: Bad command | command not found → ERROR captured; build FAILED → RELEASE_BLOCKED |

---

## Dockerfile Changes

The container now includes:

- **Java 21** (OpenJDK via apt)
- **Maven 3.9.9** (via apt)
- **Gradle 8.5** (direct binary download from services.gradle.org; apt version is 4.x, too old)
- **Node 20.19.2** + **npm 9.2.0** (via apt Bookworm)
- Build time: ~3.5 minutes (vs ~40s before)

---

## Definition of Done (from execution guide)

- [x] Capability profiles exist
- [x] Python path still works (zero regression)
- [x] Java Maven repo can run tests
- [x] Java Gradle repo can run tests or fail clearly
- [x] Node/React repo can detect scripts and run supported commands
- [x] Unknown repo fails safely
- [x] Test Quality and Architecture Agents receive stack-aware context
- [x] Release Gate is profile-aware
- [x] Dashboard shows profile/test/build/lint info
- [x] No auto-merge for new stacks by default
