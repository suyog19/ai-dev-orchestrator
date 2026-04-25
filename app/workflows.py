import difflib
import logging
import os
from datetime import datetime, timezone
from app.repo_mapping import get_mapping
from app.git_ops import clone_repo, commit_and_push
from app.github_api import create_pull_request, ensure_label, add_label_to_pr, merge_pull_request, post_pr_comment, get_pr_diff, get_pr_details
from app.repo_analysis import analyze_repo, format_telegram_summary
from app.claude_client import summarize_repo, suggest_change, fix_change, plan_epic_breakdown, detect_epic_missing_specifics, MAX_STORIES_PER_EPIC, review_pr, review_test_quality, review_architecture
from app.jira_client import get_issue_details
from app.file_modifier import apply_suggestion, apply_changes, modify_file
from app.telegram import send_message
from app.database import (
    update_run_step, update_run_field, fail_run,
    record_attempt, complete_attempt,
    add_planning_output, get_planning_outputs, update_planning_output_status,
    request_planning_approval, set_run_waiting_for_approval, complete_planning_run,
    get_created_children_for_epic, store_planning_metadata,
    record_planning_feedback, record_execution_feedback,
    get_planning_memory, get_execution_memory, get_project_knowledge_for_prompt,
    store_agent_review,
    store_test_quality_review,
    store_architecture_review,
    get_active_clarification,
    get_run_state,
    upsert_capability_profile,
)
from app.repo_profiler import detect_repo_capability_profile
from app.feedback import ReviewStatus, TestQualityStatus, ArchitectureStatus, ReleaseDecision, ClarificationContextKey
from app.test_runner import run_tests
from app.security import ensure_github_writes_allowed
from app.clarification import pause_for_clarification, ClarificationRequested, is_clarification_enabled
from app.deployment_validator import run_deployment_validation

AI_LABEL = "ai-generated"
AI_LABEL_COLOR = "6f42c1"  # purple
MAX_FILES_FOR_AUTOMERGE = 3

logger = logging.getLogger("worker")


def _build_review_package(
    issue_key: str,
    summary: str,
    story_details: dict,
    mapping: dict,
    branch: str,
    pr: dict,
    commit_message: str,
    final_changes: list,
    diff_block: str,
    final_test_result: dict,
    retry_count: int,
    execution_memory: str,
) -> dict:
    """Assemble the full context package the Reviewer Agent will need.

    Returns a dict with keys: story_context, pr_context, diff, test_result, memory_context.
    Structured to unpack directly into review_pr(**package) in Iteration 3.
    No Claude call, no DB write, no secrets included.
    """
    files_changed = [ch.get("file", "") for ch in final_changes if ch.get("file")]
    output = (final_test_result.get("output") or "").strip()
    output_excerpt = "\n".join(output.splitlines()[-30:]) if output else ""

    return {
        "story_context": {
            "key": issue_key,
            "summary": summary,
            "description": story_details.get("description"),
            "acceptance_criteria": story_details.get("acceptance_criteria") or [],
        },
        "pr_context": {
            "number": pr["number"],
            "url": pr["url"],
            "title": pr.get("title", ""),
            "repo_slug": mapping["repo_slug"],
            "base_branch": mapping["base_branch"],
            "working_branch": branch,
            "files_changed": files_changed,
            "files_changed_count": len(files_changed),
            "commit_message": commit_message,
            "retry_count": retry_count,
        },
        "diff": diff_block,
        "test_result": {
            "status": final_test_result["status"],
            "command": final_test_result.get("command", ""),
            "output_excerpt": output_excerpt,
        },
        "memory_context": execution_memory,
    }


def _format_review_comment(verdict: dict) -> str:
    """Render a Reviewer Agent verdict as a GitHub PR comment in markdown."""
    status = verdict.get("review_status", "UNKNOWN")
    risk = verdict.get("risk_level", "UNKNOWN")
    summary = verdict.get("summary", "")
    findings = verdict.get("findings") or []
    blocking = verdict.get("blocking_reasons") or []
    recommendations = verdict.get("recommendations") or []

    status_emoji = {"APPROVED_BY_AI": "✅", "NEEDS_CHANGES": "⚠️", "BLOCKED": "🚫", "ERROR": "❌"}.get(status, "❓")

    findings_lines = "\n".join(
        f"- [{f.get('severity', 'INFO')}] **{f.get('category', '')}**: {f.get('message', '')}"
        for f in findings
    ) or "_None_"

    blocking_lines = "\n".join(f"- {r}" for r in blocking) or "_None_"
    rec_lines = "\n".join(f"- {r}" for r in recommendations) or "_None_"

    return (
        f"## {status_emoji} Reviewer Agent Verdict: `{status}`\n\n"
        f"**Risk:** {risk}\n\n"
        f"### Summary\n{summary}\n\n"
        f"### Findings\n{findings_lines}\n\n"
        f"### Blocking Reasons\n{blocking_lines}\n\n"
        f"### Recommendations\n{rec_lines}\n\n"
        f"---\n"
        f"_🤖 [AI Dev Orchestrator](https://github.com/suyog19/ai-dev-orchestrator) — Reviewer Agent_"
    )


_TEST_FILE_PATTERNS = ("tests/", "test_", "_test.py", "/test")

def _is_test_file(path: str) -> bool:
    """Return True if a file path looks like a test file (legacy — no profile context)."""
    return any(p in path for p in _PYTHON_TEST_PATTERNS)


# Phase 10 (Python) + Phase 15 (Java/Node) skip patterns
_PYTHON_SKIP_PATTERNS = ("@pytest.mark.skip", "pytest.skip(", "skipTest(")
_JAVA_SKIP_PATTERNS   = ("@Disabled", "@Ignore", "@IgnoreRest")
_NODE_SKIP_PATTERNS   = ("it.skip(", "test.skip(", "describe.skip(", "xit(", "xdescribe(", "xtest(", ".todo(")
_SKIP_PATTERNS = _PYTHON_SKIP_PATTERNS + _JAVA_SKIP_PATTERNS + _NODE_SKIP_PATTERNS


def _detect_skipped_tests(diff: str, test_output: str, profile_name: str | None = None) -> bool:
    """Return True if skipped tests are detected in the diff or test output.

    Uses profile-aware patterns: adds Java (@Disabled/@Ignore) and Node (it.skip, xtest, etc.)
    patterns on top of the base Python patterns. The 'skipped' keyword check is preserved
    for test runner output that already reports skipped counts.
    """
    combined = (diff or "") + (test_output or "")
    if profile_name in ("java_maven", "java_gradle"):
        patterns = _PYTHON_SKIP_PATTERNS + _JAVA_SKIP_PATTERNS
    elif profile_name == "node_react":
        patterns = _PYTHON_SKIP_PATTERNS + _NODE_SKIP_PATTERNS
    else:
        patterns = _PYTHON_SKIP_PATTERNS
    if "skipped" in combined.lower():
        return True
    return any(p in combined for p in patterns)


def _build_test_quality_package(
    issue_key: str,
    summary: str,
    story_details: dict,
    mapping: dict,
    pr: dict,
    final_changes: list,
    diff_block: str,
    final_test_result: dict,
    retry_count: int,
    execution_memory: str,
    profile_name: str | None = None,
) -> dict:
    """Assemble the full context package for the Test Quality Agent.

    Returns a dict with keys that unpack directly into review_test_quality(**pkg).
    No Claude call, no DB write, no secrets included.
    """
    all_files = [ch.get("file", "") for ch in final_changes if ch.get("file")]
    source_files = [f for f in all_files if not _is_test_file(f, profile_name)]
    test_files = [f for f in all_files if _is_test_file(f, profile_name)]

    output = (final_test_result.get("output") or "").strip()
    output_excerpt = "\n".join(output.splitlines()[-30:]) if output else ""
    skipped = _detect_skipped_tests(diff_block, output, profile_name)

    return {
        "story_context": {
            "key": issue_key,
            "summary": summary,
            "description": story_details.get("description"),
            "acceptance_criteria": story_details.get("acceptance_criteria") or [],
        },
        "pr_context": {
            "number": pr["number"],
            "url": pr["url"],
            "title": pr.get("title", ""),
            "body": pr.get("body", ""),
        },
        "diff_context": {
            "full_diff": diff_block,
            "changed_files": all_files,
        },
        "test_context": {
            "status": final_test_result["status"],
            "command": final_test_result.get("command", ""),
            "output_excerpt": output_excerpt,
            "test_files_changed": test_files,
            "skipped_tests_detected": skipped,
            "profile_name": profile_name or "python_fastapi",
        },
        "implementation_context": {
            "files_changed_count": len(all_files),
            "retry_count": retry_count,
            "changed_source_files": source_files,
            "changed_test_files": test_files,
        },
        "memory_context": execution_memory,
    }


def _format_test_quality_comment(verdict: dict) -> str:
    """Render a Test Quality Agent verdict as a GitHub PR comment in markdown."""
    status = verdict.get("quality_status", "UNKNOWN")
    confidence = verdict.get("confidence_level", "UNKNOWN")
    summary = verdict.get("summary", "")
    findings = verdict.get("coverage_findings") or []
    missing = verdict.get("missing_tests") or []
    suspicious = verdict.get("suspicious_tests") or []
    recommendations = verdict.get("recommendations") or []

    emoji = {
        "TEST_QUALITY_APPROVED": "✅",
        "TESTS_WEAK": "⚠️",
        "TESTS_BLOCKING": "🚫",
        "ERROR": "❌",
    }.get(status, "❓")

    findings_lines = "\n".join(
        f"- [{f.get('status', '?').upper()}] **{f.get('criteria', '')}**: {f.get('evidence', '')}"
        for f in findings
    ) or "_None_"

    missing_lines = "\n".join(f"- {m}" for m in missing) or "_None_"
    suspicious_lines = "\n".join(f"- {s}" for s in suspicious) or "_None_"
    rec_lines = "\n".join(f"- {r}" for r in recommendations) or "_None_"

    return (
        f"## {emoji} Test Quality Agent Verdict: `{status}`\n\n"
        f"**Confidence:** {confidence}\n\n"
        f"### Summary\n{summary}\n\n"
        f"### Coverage Findings\n{findings_lines}\n\n"
        f"### Missing Tests\n{missing_lines}\n\n"
        f"### Suspicious Test Changes\n{suspicious_lines}\n\n"
        f"### Recommendations\n{rec_lines}\n\n"
        f"---\n"
        f"_🤖 [AI Dev Orchestrator](https://github.com/suyog19/ai-dev-orchestrator) — Test Quality Agent_"
    )


# --- Architecture / Impact Agent helpers (Phase 10 + Phase 15 profile-aware) ---

_API_PATTERNS     = ("main.py", "routes", "router", "endpoints", "api", "views")
_MODEL_PATTERNS   = ("model", "schema", "models", "schemas", "entity")
_STORAGE_PATTERNS = ("database", "db", "migration", "alembic", "repository", "repo")
_CONFIG_PATTERNS  = (".env", "config", "settings", "constants")
_DOC_PATTERNS     = ("readme", ".md", "docs/", "changelog")

# Phase 15: Java-specific classification patterns
_JAVA_BUILD_PATTERNS      = ("pom.xml", "build.gradle", "settings.gradle", "gradlew", "Makefile")
_JAVA_CONTROLLER_PATTERNS = ("Controller",)
_JAVA_SERVICE_PATTERNS    = ("Service", "ServiceImpl")
_JAVA_REPO_PATTERNS       = ("Repository", "Dao", "Mapper")
_JAVA_ENTITY_PATTERNS     = ("Entity", "Model", "Dto", "Request", "Response")
_JAVA_CONFIG_PATTERNS     = ("Config", "Configuration", "Properties", "application.yml", "application.properties")

# Phase 15: Node/React-specific classification patterns (lowercased for .lower() matching)
_NODE_BUILD_PATTERNS   = ("package.json", "yarn.lock", "pnpm-lock", "package-lock", ".lock")
_NODE_STATE_PATTERNS   = ("store", "slice", "context", "redux", "zustand", "recoil", "atom")
_NODE_HOOK_PATTERNS    = ("/hooks/", "/hook/")
_NODE_ROUTE_PATTERNS   = ("/routes/", "/route/", "/pages/", "/page/", "router")
_NODE_API_PATTERNS     = ("/api/", "/services/", "/service/", "client", "fetch", "axios")
_NODE_CONFIG_PATTERNS  = (".env", "vite.config", "tsconfig", "next.config", "jest.config", "vitest.config", ".eslintrc")

# Phase 15: Profile-aware test file patterns
_JAVA_TEST_PATTERNS   = ("src/test/", "Test.java", "Tests.java", "IT.java", "Spec.java")
_NODE_TEST_PATTERNS   = (".test.ts", ".test.tsx", ".test.js", ".spec.ts", ".spec.tsx", ".spec.js", "__tests__/")
_PYTHON_TEST_PATTERNS = ("tests/", "test_", "_test.py", "/test")


def _is_test_file(path: str, profile_name: str | None = None) -> bool:
    """Return True if a file path looks like a test file, using profile-aware patterns."""
    if profile_name in ("java_maven", "java_gradle"):
        return any(p in path for p in _JAVA_TEST_PATTERNS)
    if profile_name == "node_react":
        return any(p in path for p in _NODE_TEST_PATTERNS)
    return any(p in path for p in _PYTHON_TEST_PATTERNS)


def _classify_java_files(files: list[str]) -> dict:
    """Classify changed files using Java layer patterns."""
    groups: dict[str, list[str]] = {
        "controller": [], "service": [], "repository": [], "entity": [], "config": [], "test": [], "build": [], "other": [],
    }
    for f in files:
        if _is_test_file(f, "java_maven"):
            groups["test"].append(f)
        elif any(p in f for p in _JAVA_BUILD_PATTERNS):
            groups["build"].append(f)
        elif any(p in f for p in _JAVA_CONTROLLER_PATTERNS):
            groups["controller"].append(f)
        elif any(p in f for p in _JAVA_SERVICE_PATTERNS):
            groups["service"].append(f)
        elif any(p in f for p in _JAVA_REPO_PATTERNS):
            groups["repository"].append(f)
        elif any(p in f for p in _JAVA_ENTITY_PATTERNS):
            groups["entity"].append(f)
        elif any(p in f for p in _JAVA_CONFIG_PATTERNS):
            groups["config"].append(f)
        else:
            groups["other"].append(f)
    return {k: v for k, v in groups.items() if v}


def _classify_node_files(files: list[str]) -> dict:
    """Classify changed files using Node/React layer patterns."""
    groups: dict[str, list[str]] = {
        "component": [], "hook": [], "route": [], "state": [], "api": [], "config": [], "test": [], "build": [], "other": [],
    }
    for f in files:
        fl = f.lower()
        if _is_test_file(f, "node_react"):
            groups["test"].append(f)
        elif any(p in fl for p in _NODE_BUILD_PATTERNS):
            groups["build"].append(f)
        elif any(p in fl for p in _NODE_STATE_PATTERNS):
            groups["state"].append(f)
        elif any(p in fl for p in _NODE_HOOK_PATTERNS):
            groups["hook"].append(f)
        elif any(p in fl for p in _NODE_ROUTE_PATTERNS):
            groups["route"].append(f)
        elif any(p in fl for p in _NODE_API_PATTERNS):
            groups["api"].append(f)
        elif any(p in fl for p in _NODE_CONFIG_PATTERNS):
            groups["config"].append(f)
        elif f.endswith((".tsx", ".jsx")):
            groups["component"].append(f)
        else:
            groups["other"].append(f)
    return {k: v for k, v in groups.items() if v}


def _classify_python_files(files: list[str]) -> dict:
    """Classify changed files using Python/FastAPI layer patterns."""
    groups: dict[str, list[str]] = {
        "api": [], "model": [], "storage": [], "config": [], "test": [], "docs": [], "other": [],
    }
    for f in files:
        fl = f.lower()
        if _is_test_file(f, "python_fastapi"):
            groups["test"].append(f)
        elif any(p in fl for p in _DOC_PATTERNS):
            groups["docs"].append(f)
        elif any(p in fl for p in _API_PATTERNS):
            groups["api"].append(f)
        elif any(p in fl for p in _MODEL_PATTERNS):
            groups["model"].append(f)
        elif any(p in fl for p in _STORAGE_PATTERNS):
            groups["storage"].append(f)
        elif any(p in fl for p in _CONFIG_PATTERNS):
            groups["config"].append(f)
        else:
            groups["other"].append(f)
    return {k: v for k, v in groups.items() if v}


def _classify_changed_files(files: list[str], profile_name: str | None = None) -> dict:
    """Group changed files by architectural layer, profile-aware (Phase 15).

    Routes to stack-specific classification for java_* and node_react profiles;
    falls back to the original Python/FastAPI classification for everything else.
    """
    if profile_name in ("java_maven", "java_gradle"):
        return _classify_java_files(files)
    if profile_name == "node_react":
        return _classify_node_files(files)
    return _classify_python_files(files)


def _build_architecture_review_package(
    issue_key: str,
    summary: str,
    story_details: dict,
    mapping: dict,
    pr: dict,
    final_changes: list,
    diff_block: str,
    final_test_result: dict,
    verdict: dict,
    tq_verdict: dict,
    retry_count: int,
    execution_memory: str,
    repo_analysis: dict,
    profile_name: str | None = None,
) -> dict:
    """Assemble context for the Architecture Agent.

    Returns a dict that unpacks directly into review_architecture(**pkg).
    No Claude call, no DB write, no secrets included.
    """
    all_files = [ch.get("file", "") for ch in final_changes if ch.get("file")]
    lang = repo_analysis.get("primary_language", "unknown")
    framework = repo_analysis.get("framework", "unknown")
    file_classification = _classify_changed_files(all_files, profile_name)

    return {
        "story_context": {
            "key": issue_key,
            "summary": summary,
            "description": story_details.get("description"),
            "acceptance_criteria": story_details.get("acceptance_criteria") or [],
        },
        "repo_context": {
            "repo_slug": mapping.get("repo_slug", ""),
            "primary_language": lang,
            "framework": framework,
            "profile_name": profile_name or "python_fastapi",
        },
        "pr_context": {
            "number": pr["number"],
            "url": pr["url"],
            "title": pr.get("title", ""),
            "body": pr.get("body", ""),
        },
        "diff_context": {
            "full_diff": diff_block,
            "changed_files": all_files,
            "file_classification": file_classification,
        },
        "signal_context": {
            "test_status": final_test_result["status"],
            "review_status": verdict.get("review_status", "N/A"),
            "test_quality_status": tq_verdict.get("quality_status", "N/A"),
            "files_changed_count": len(all_files),
            "retry_count": retry_count,
        },
        "memory_context": execution_memory,
    }


def _format_architecture_comment(verdict: dict) -> str:
    """Render an Architecture Agent verdict as a GitHub PR comment in markdown."""
    status = verdict.get("architecture_status", "UNKNOWN")
    risk = verdict.get("risk_level", "UNKNOWN")
    summary = verdict.get("summary", "")
    impact_areas = verdict.get("impact_areas") or []
    blocking = verdict.get("blocking_reasons") or []
    recommendations = verdict.get("recommendations") or []

    emoji = {
        "ARCHITECTURE_APPROVED":      "✅",
        "ARCHITECTURE_NEEDS_REVIEW":  "⚠️",
        "ARCHITECTURE_BLOCKED":       "🚫",
        "ERROR":                      "❌",
    }.get(status, "❓")

    impact_lines = "\n".join(
        f"- **{a.get('area', '?').upper()}**: `{a.get('risk', '?')}` — {a.get('finding', '')}"
        for a in impact_areas
    ) or "_None_"
    blocking_lines = "\n".join(f"- {b}" for b in blocking) or "_None_"
    rec_lines = "\n".join(f"- {r}" for r in recommendations) or "_None_"

    return (
        f"## {emoji} Architecture Agent Verdict: `{status}`\n\n"
        f"**Risk Level:** {risk}\n\n"
        f"### Summary\n{summary}\n\n"
        f"### Impact Areas\n{impact_lines}\n\n"
        f"### Blocking Reasons\n{blocking_lines}\n\n"
        f"### Recommendations\n{rec_lines}\n\n"
        f"---\n"
        f"_🤖 [AI Dev Orchestrator](https://github.com/suyog19/ai-dev-orchestrator) — Architecture Agent_"
    )


def _check_epic_vagueness(summary: str, description: str | None) -> str | None:
    """Return a clarification question if the Epic is too vague for safe decomposition, else None."""
    summary_words = (summary or "").split()
    desc = (description or "").strip()

    if len(summary_words) < 4 and not desc:
        return (
            f"The Epic summary '{summary}' is very short and has no description. "
            f"What is the detailed scope and expected outcomes? "
            f"Options: 1. Describe the full goal  2. List key deliverables  3. Specify target users and success criteria"
        )

    if not desc or len(desc) < 50:
        return (
            f"The Epic '{summary}' has no detailed description or acceptance criteria. "
            f"What are the expected deliverables and how should completion be measured?"
        )

    return None


def _check_story_ambiguity(summary: str, story_details: dict) -> str | None:
    """Return a clarification question if the Story is too ambiguous for safe implementation, else None."""
    description = (story_details.get("description") or "").strip()
    acceptance_criteria = story_details.get("acceptance_criteria") or []
    summary_words = (summary or "").split()

    if len(summary_words) < 4 and not description and not acceptance_criteria:
        return (
            f"Story '{summary}' has a very short summary and no description or acceptance criteria. "
            f"What specific change should be made and what is the expected behaviour after implementation?"
        )

    if not acceptance_criteria and not description:
        return (
            f"Story '{summary}' has no acceptance criteria or description. "
            f"What is the expected behaviour after implementation, and how should it be tested?"
        )

    return None


def _build_test_section(test_result: dict, attempt: int = 1) -> str:
    status = test_result["status"]
    output = (test_result.get("output") or "").strip()
    tail = "\n".join(output.splitlines()[-20:]) if output else ""
    label = "Tests (after fix)" if attempt > 1 else "Tests"

    if status == "PASSED":
        return (
            f"## {label}\n"
            f"- [x] `{test_result['command']}` — **PASSED**\n\n"
            f"<details><summary>Output</summary>\n\n```\n{tail}\n```\n</details>\n"
        )
    if status == "FAILED":
        return (
            f"## {label}\n"
            f"- [ ] `{test_result['command']}` — **FAILED** — review required\n\n"
            f"<details><summary>Output</summary>\n\n```\n{tail}\n```\n</details>\n"
        )
    if status == "ERROR":
        return (
            f"## {label}\n"
            f"- [ ] Test execution error: {output[:200]}\n"
        )
    return f"## {label}\n- Tests not run (no supported test framework detected)\n"


# Phase 15 — per-profile release gate policies
# allow_auto_merge: if False, auto-merge is always skipped regardless of agent verdicts
# require_tests:    if True, NOT_RUN tests = skip; if False, NOT_RUN is acceptable
# require_build:    if True, NOT_RUN build = skip; FAILED is always a hard block regardless
# require_lint:     if True, NOT_RUN lint = skip; FAILED is always a soft skip regardless
_PROFILE_RELEASE_POLICY: dict[str, dict] = {
    "python_fastapi":  {"allow_auto_merge": True,  "require_tests": True,  "require_build": False, "require_lint": False},
    "java_maven":      {"allow_auto_merge": False, "require_tests": True,  "require_build": True,  "require_lint": False},
    "java_gradle":     {"allow_auto_merge": False, "require_tests": True,  "require_build": True,  "require_lint": False},
    "node_react":      {"allow_auto_merge": False, "require_tests": False, "require_build": True,  "require_lint": False},
    "generic_unknown": {"allow_auto_merge": False, "require_tests": False, "require_build": False, "require_lint": False},
}
_DEFAULT_PROFILE_POLICY: dict = {"allow_auto_merge": True, "require_tests": True, "require_build": False, "require_lint": False}

# Phase 16 — deployment validation policy.
# deployment_validation_required=False for all profiles: validation is post-merge
# and observational only. It never retroactively alters the release_decision set
# before merge. Promote to required=True per profile when environments are stable.
_PROFILE_DEPLOYMENT_POLICY: dict[str, dict] = {
    "python_fastapi":  {"deployment_validation_required": False},
    "java_maven":      {"deployment_validation_required": False},
    "java_gradle":     {"deployment_validation_required": False},
    "node_react":      {"deployment_validation_required": False},
    "generic_unknown": {"deployment_validation_required": False},
}
_DEFAULT_DEPLOYMENT_POLICY: dict = {"deployment_validation_required": False}


def get_deployment_policy_for_profile(profile_name: str | None) -> dict:
    """Return the deployment validation policy for a capability profile."""
    return _PROFILE_DEPLOYMENT_POLICY.get(profile_name or "", _DEFAULT_DEPLOYMENT_POLICY)


def is_self_modification(repo_slug: str) -> bool:
    """Return True if this run would modify the orchestrator itself.

    Triggered when repo_slug matches ORCHESTRATOR_SELF_REPO env var.
    Defaults to 'suyog19/ai-dev-orchestrator' if not set.
    """
    self_repo = os.environ.get("ORCHESTRATOR_SELF_REPO", "suyog19/ai-dev-orchestrator")
    return repo_slug == self_repo


def is_first_use_mode_active(repo_slug: str) -> bool:
    """Return True if first-use safety mode applies to this repo.

    Active when:
    - FIRST_USE_MODE_ENABLED=true (default: true)
    - Repo has fewer than FIRST_USE_RUN_COUNT completed workflow runs (default: 3)
    """
    enabled = os.environ.get("FIRST_USE_MODE_ENABLED", "true").lower() == "true"
    if not enabled:
        return False
    threshold = int(os.environ.get("FIRST_USE_RUN_COUNT", "3"))
    from app.database import count_completed_workflow_runs_for_repo
    completed = count_completed_workflow_runs_for_repo(repo_slug)
    return completed < threshold


def evaluate_release_decision(
    mapping: dict,
    final_test_result: dict,
    applied: dict,
    review_status: str,
    test_quality_status: str,
    architecture_status: str,
    build_status: str = "NOT_RUN",
    lint_status: str = "NOT_RUN",
    capability_profile: dict | None = None,
    first_use_mode_active: bool = False,
) -> dict:
    """Evaluate all agent gates and return a unified release decision.

    Returns a dict with keys:
    - release_decision: RELEASE_APPROVED | RELEASE_SKIPPED | RELEASE_BLOCKED | RELEASE_ERROR
    - can_auto_merge: bool
    - reason: str
    - blocking_gates: list[str]
    - warnings: list[str]
    """
    profile_name = (capability_profile or {}).get("profile_name") if capability_profile else None
    policy = _PROFILE_RELEASE_POLICY.get(profile_name, _DEFAULT_PROFILE_POLICY)

    blocking_gates = []
    warnings = []

    # Hard blocks — any one alone prevents auto-merge entirely
    if final_test_result.get("status") == "FAILED":
        blocking_gates.append("tests failed")
    if review_status == ReviewStatus.BLOCKED:
        blocking_gates.append("reviewer blocked")
    if test_quality_status == TestQualityStatus.BLOCKING:
        blocking_gates.append("test quality blocking")
    if architecture_status == ArchitectureStatus.BLOCKED:
        blocking_gates.append("architecture blocked")
    if build_status == "FAILED":
        blocking_gates.append("build failed")

    if blocking_gates:
        return {
            "release_decision": ReleaseDecision.BLOCKED,
            "can_auto_merge": False,
            "reason": "Blocked by: " + "; ".join(blocking_gates),
            "blocking_gates": blocking_gates,
            "warnings": warnings,
        }

    # Soft skips — auto-merge disabled or agent concerns
    skip_reasons = []
    if first_use_mode_active:
        skip_reasons.append("first-use safety mode active (first N runs require manual review)")
    repo_slug = mapping.get("repo_slug", "")
    if is_self_modification(repo_slug):
        skip_reasons.append("self-modification guard: orchestrator repo requires manual merge always")
    if not mapping.get("auto_merge_enabled"):
        skip_reasons.append("auto_merge disabled for repo")
    # Phase 15: profile policy — no auto-merge for Java/Node/unknown stacks
    if not policy["allow_auto_merge"] and profile_name:
        skip_reasons.append(f"profile policy: no auto-merge for {profile_name}")
        warnings.append(f"auto-merge disabled by profile policy ({profile_name})")

    # Tests: profile controls whether NOT_RUN is acceptable
    test_status = final_test_result.get("status", "NOT_RUN")
    if test_status not in ("PASSED",):
        if test_status == "NOT_RUN" and not policy["require_tests"]:
            pass  # tests not required for this profile — NOT_RUN is acceptable
        else:
            skip_reasons.append(f"tests {test_status}")

    if not applied.get("applied", False):
        skip_reasons.append("fallback apply used")
    if applied.get("count", 0) > MAX_FILES_FOR_AUTOMERGE:
        skip_reasons.append(f"{applied.get('count')} files > {MAX_FILES_FOR_AUTOMERGE} limit")
    if review_status == ReviewStatus.NEEDS_CHANGES:
        skip_reasons.append("reviewer needs changes")
    elif review_status not in (ReviewStatus.APPROVED_BY_AI,):
        warnings.append(f"review status: {review_status}")
        skip_reasons.append(f"review status: {review_status}")
    if test_quality_status == TestQualityStatus.WEAK:
        skip_reasons.append("test quality weak")
    elif test_quality_status not in (TestQualityStatus.APPROVED,):
        skip_reasons.append(f"test quality status: {test_quality_status}")
    if architecture_status == ArchitectureStatus.NEEDS_REVIEW:
        skip_reasons.append("architecture needs review")
        warnings.append("architecture needs human review")
    elif architecture_status not in (ArchitectureStatus.APPROVED,):
        skip_reasons.append(f"architecture status: {architecture_status}")
    # Phase 15: build NOT_RUN when profile requires build
    if policy["require_build"] and build_status == "NOT_RUN" and profile_name:
        skip_reasons.append(f"build NOT_RUN (required for {profile_name})")
    # Phase 15: lint failure is a soft skip
    if lint_status == "FAILED":
        skip_reasons.append("lint failed")

    if skip_reasons:
        return {
            "release_decision": ReleaseDecision.SKIPPED,
            "can_auto_merge": False,
            "reason": "; ".join(skip_reasons),
            "blocking_gates": [],
            "warnings": warnings,
        }

    # All gates passed
    return {
        "release_decision": ReleaseDecision.APPROVED,
        "can_auto_merge": True,
        "reason": "All release gates passed",
        "blocking_gates": [],
        "warnings": [],
    }


def _run_post_merge_validation(
    run_id: int,
    issue_key: str,
    repo_slug: str,
    commit_sha: str | None = None,
    pr_number: int | None = None,
    environment: str = "dev",
) -> None:
    """Run deployment validation after a successful merge.

    Non-fatal: any exception is caught and logged. Validation failure never
    rolls back the merge or alters the release_decision already stored — it is
    observational only. Policy is defined in _PROFILE_DEPLOYMENT_POLICY.
    """
    try:
        import os
        if os.environ.get("DEPLOYMENT_VALIDATION_ENABLED", "true").lower() != "true":
            logger.info("_run_post_merge_validation: disabled by env var — run_id=%s", run_id)
            return

        update_run_step(run_id, "deployment_validation")
        timeout_s = int(os.environ.get("DEPLOYMENT_VALIDATION_TIMEOUT_SECONDS", "120"))
        retry_n   = int(os.environ.get("DEPLOYMENT_VALIDATION_RETRY_COUNT", "3"))
        retry_d   = int(os.environ.get("DEPLOYMENT_VALIDATION_RETRY_DELAY_SECONDS", "10"))

        # Log the active deployment policy for this run (observational — required=False means
        # a FAILED result is recorded and surfaced but never retroactively alters release_decision)
        run_state_for_policy = get_run_state(run_id)
        profile_name_for_policy = (run_state_for_policy or {}).get("capability_profile_name") or "generic_unknown"
        dep_policy = _PROFILE_DEPLOYMENT_POLICY.get(profile_name_for_policy, _DEFAULT_DEPLOYMENT_POLICY)
        logger.info(
            "_run_post_merge_validation: profile=%s required=%s run_id=%s",
            profile_name_for_policy, dep_policy["deployment_validation_required"], run_id,
        )

        result = run_deployment_validation(
            run_id=run_id,
            repo_slug=repo_slug,
            environment=environment,
            commit_sha=commit_sha,
            pr_number=pr_number,
            timeout_seconds=timeout_s,
            retry_count=retry_n,
            retry_delay_seconds=retry_d,
        )

        status = result["status"]
        summary = result["summary"]
        passed = sum(1 for r in result.get("smoke_results", []) if r.get("status") == "PASSED")
        total  = len(result.get("smoke_results", []))

        if status == "PASSED":
            send_message(
                "deployment_validation_passed", "COMPLETE",
                f"{issue_key}: deployment validation passed\n"
                f"Smoke tests: {passed}/{total} passed\n{summary}",
            )
        elif status in ("FAILED", "ERROR"):
            failed_names = [
                r.get("name", "?")
                for r in result.get("smoke_results", [])
                if r.get("status") != "PASSED"
            ]
            send_message(
                "deployment_validation_failed", "FAILED",
                f"{issue_key}: deployment validation {status}\n"
                f"Failed: {', '.join(failed_names) or summary}",
            )
        elif status == "NOT_CONFIGURED":
            logger.info(
                "_run_post_merge_validation: no profile for %s — NOT_CONFIGURED (silent)",
                repo_slug,
            )
        # SKIPPED — no Telegram noise

        # Publish GitHub commit status for deployment validation (best-effort)
        try:
            from app.github_status_publisher import publish_deployment_validation_status
            from app.security import ensure_github_writes_allowed
            ensure_github_writes_allowed("status", repo_slug, run_id)
            pub = publish_deployment_validation_status(
                run_id=run_id,
                repo_slug=repo_slug,
                deployment_validation_status=status,
                pr_number=pr_number,
                commit_sha=commit_sha,
            )
            if pub["errors"]:
                logger.warning(
                    "_run_post_merge_validation: GitHub status publish failed — %s", pub["errors"]
                )
        except Exception as pub_exc:
            logger.warning(
                "_run_post_merge_validation: GitHub status publish error (non-fatal) — %s", pub_exc
            )

    except Exception as exc:
        logger.error(
            "_run_post_merge_validation: non-fatal error for run_id=%s — %s", run_id, exc,
        )


def _story_review_and_release(
    run_id: int,
    issue_key: str,
    summary: str,
    mapping: dict,
    run_state: dict,
    clarification_answer: str | None = None,
) -> None:
    """Run review agents and release gate for a story that already has a PR.

    Used on resume after a review-stage clarification is answered.
    Reconstructs context from DB + GitHub diff instead of re-running implementation.
    """
    pr_url = run_state["pr_url"]
    pr_number_match = __import__("re").search(r"/pull/(\d+)$", pr_url or "")
    if not pr_number_match:
        logger.error("_story_review_and_release: cannot extract PR number from url=%s", pr_url)
        fail_run(run_id, f"Review resume failed: cannot parse PR number from {pr_url}")
        return
    pr_number = int(pr_number_match.group(1))
    pr = {"number": pr_number, "url": pr_url, "title": f"ai: {issue_key} — {summary}", "body": ""}

    execution_memory = ""
    try:
        execution_memory = get_execution_memory(mapping["repo_slug"])
    except Exception:
        pass
    if clarification_answer:
        note = f"User clarification for review: {clarification_answer}"
        execution_memory = f"{execution_memory}\n\n{note}" if execution_memory else note

    story_details: dict = {"key": issue_key, "summary": summary, "description": None, "acceptance_criteria": []}
    try:
        story_details = get_issue_details(issue_key)
    except Exception as exc:
        logger.warning("_story_review_and_release: get_issue_details failed (non-fatal) — %s", exc)

    diff_block = ""
    try:
        diff_block = get_pr_diff(mapping["repo_slug"], pr_number)
    except Exception as exc:
        logger.warning("_story_review_and_release: get_pr_diff failed (non-fatal) — %s", exc)

    final_test_result = {
        "status": run_state.get("test_status") or "NOT_RUN",
        "command": run_state.get("test_command") or "",
        "output": run_state.get("test_output") or "",
    }

    review_package = _build_review_package(
        issue_key=issue_key,
        summary=summary,
        story_details=story_details,
        mapping=mapping,
        branch=run_state.get("working_branch", ""),
        pr=pr,
        commit_message=f"ai: {issue_key} — {summary}",
        final_changes=[],
        diff_block=diff_block,
        final_test_result=final_test_result,
        retry_count=0,
        execution_memory=execution_memory,
    )

    send_message(
        "review_resumed", "RUNNING",
        f"{issue_key}: re-running review agents after clarification answer",
    )

    # --- Reviewer Agent ---
    update_run_step(run_id, "reviewing")
    verdict: dict = {}
    try:
        verdict = review_pr(**review_package)
        store_agent_review(
            run_id=run_id, verdict=verdict, pr_number=pr_number,
            pr_url=pr_url, repo_slug=mapping["repo_slug"], story_key=issue_key,
        )
        try:
            post_pr_comment(mapping["repo_slug"], pr_number, _format_review_comment(verdict))
        except Exception:
            pass
        send_message(
            "review_completed", verdict.get("review_status", "UNKNOWN"),
            f"{issue_key}: PR #{pr_number} verdict={verdict.get('review_status')} risk={verdict.get('risk_level')}",
        )
    except Exception as exc:
        verdict = {
            "review_status": ReviewStatus.ERROR, "risk_level": "HIGH",
            "summary": f"Reviewer Agent error: {exc}",
            "findings": [], "blocking_reasons": [str(exc)], "recommendations": [],
        }
        logger.error("_story_review_and_release: Reviewer Agent failed — %s", exc)

    # --- Architecture Agent ---
    update_run_step(run_id, "architecture_review")
    arch_verdict: dict = {}
    try:
        arch_package = _build_architecture_review_package(
            issue_key=issue_key, summary=summary, story_details=story_details,
            mapping=mapping, pr=pr, final_changes=[], diff_block=diff_block,
            final_test_result=final_test_result, verdict=verdict, tq_verdict={},
            retry_count=0, execution_memory=execution_memory, repo_analysis={},
        )
        arch_verdict = review_architecture(**arch_package)
        store_architecture_review(
            run_id=run_id, verdict=arch_verdict, pr_number=pr_number,
            pr_url=pr_url, repo_slug=mapping["repo_slug"], story_key=issue_key,
        )
        try:
            post_pr_comment(mapping["repo_slug"], pr_number, _format_architecture_comment(arch_verdict))
        except Exception:
            pass
        send_message(
            "architecture_review_completed", arch_verdict.get("architecture_status", "UNKNOWN"),
            f"{issue_key}: arch={arch_verdict.get('architecture_status')} risk={arch_verdict.get('risk_level')}",
        )
    except Exception as exc:
        arch_verdict = {
            "architecture_status": ArchitectureStatus.ERROR, "risk_level": "HIGH",
            "summary": f"Architecture Agent error: {exc}",
            "impact_areas": [], "blocking_reasons": [str(exc)], "recommendations": [],
        }
        logger.error("_story_review_and_release: Architecture Agent failed — %s", exc)

    # --- Release Gate ---
    review_status = verdict.get("review_status", ReviewStatus.ERROR)
    test_quality_status = TestQualityStatus.ERROR
    architecture_status = arch_verdict.get("architecture_status", ArchitectureStatus.ERROR)

    update_run_step(run_id, "release_gate")
    _first_use = is_first_use_mode_active(mapping.get("repo_slug", ""))
    release = evaluate_release_decision(
        mapping=mapping,
        final_test_result=final_test_result,
        applied={"applied": True, "count": 0, "files": []},
        review_status=review_status,
        test_quality_status=test_quality_status,
        architecture_status=architecture_status,
        first_use_mode_active=_first_use,
    )
    update_run_field(
        run_id,
        release_decision=release["release_decision"],
        release_decision_reason=release["reason"],
        release_decided_at=datetime.now(timezone.utc),
    )

    # --- Publish GitHub commit statuses (Phase 13) ---
    update_run_step(run_id, "publishing_github_statuses")
    try:
        from app.github_status_publisher import publish_github_statuses_for_run
        ensure_github_writes_allowed("status", mapping["repo_slug"], run_id)
        pub_result = publish_github_statuses_for_run(run_id, mapping["repo_slug"], pr_number)
        if pub_result["failed"] > 0:
            send_message(
                "github_status_publish_failed", "WARNING",
                f"{issue_key}: {pub_result['failed']} GitHub statuses failed — {'; '.join(pub_result['errors'][:2])}",
            )
    except Exception as exc:
        logger.error("_story_review_and_release: github status publish error (non-fatal) — %s", exc)

    update_run_step(run_id, "merge_check")
    if release["can_auto_merge"]:
        try:
            ensure_github_writes_allowed("merge_pr", mapping["repo_slug"], run_id)
            merge_pull_request(mapping["repo_slug"], pr_number, pr["title"])
            update_run_field(run_id, merge_status="MERGED", merged_at=datetime.now(timezone.utc))
            send_message("pr_merged", "COMPLETE", f"{issue_key}: PR #{pr_number} auto-merged after clarification")
            _run_post_merge_validation(
                run_id=run_id,
                issue_key=issue_key,
                repo_slug=mapping["repo_slug"],
                pr_number=pr_number,
            )
        except Exception as exc:
            update_run_field(run_id, merge_status="FAILED")
            send_message("pr_merge_failed", "FAILED", f"{issue_key}: auto-merge failed — {exc}")
    else:
        update_run_field(run_id, merge_status="SKIPPED")
        send_message("pr_merge_skipped", "COMPLETE", f"{issue_key}: merge skipped ({release['reason']})")

    update_run_step(run_id, "done")


def story_implementation(run_id: int, issue_key: str, issue_type: str, summary: str) -> None:
    logger.info("story_implementation: starting %s (%s) — %s", issue_key, issue_type, summary)

    jira_project_key = issue_key.split("-")[0]

    update_run_step(run_id, "mapping_lookup")
    mapping = get_mapping(jira_project_key, issue_type)
    if not mapping:
        logger.warning("No repo mapping found for project=%s issue_type=%s — aborting", jira_project_key, issue_type)
        return

    # --- Skip to review if resuming after a review-stage clarification ---
    # If pr_url is already set in the run AND there's an answered PRE_REVIEW clarification,
    # skip re-implementation and jump straight to the review agents with injected answer.
    try:
        run_state = get_run_state(run_id)
        if run_state and run_state.get("pr_url"):
            review_clar = get_active_clarification(run_id)
            if review_clar and review_clar["status"] == "ANSWERED" and review_clar.get("context_key") == ClarificationContextKey.PRE_REVIEW:
                logger.info(
                    "story_implementation: review-stage resume detected (pr_url set, PRE_REVIEW answered) — skipping to review",
                )
                _story_review_and_release(
                    run_id=run_id,
                    issue_key=issue_key,
                    summary=summary,
                    mapping=mapping,
                    run_state=run_state,
                    clarification_answer=review_clar.get("answer_text"),
                )
                return
    except Exception as exc:
        logger.warning("story_implementation: review-resume pre-check failed (non-fatal) — %s", exc)

    # --- Memory context for execution prompts ---
    execution_memory = ""
    try:
        execution_memory = get_execution_memory(mapping["repo_slug"])
        if execution_memory:
            logger.info(
                "story_implementation: injecting %d chars of execution memory (run_id=%s)",
                len(execution_memory), run_id,
            )
    except Exception as mem_exc:
        logger.warning("story_implementation: get_execution_memory failed (non-fatal): %s", mem_exc)

    # Phase 17 — inject project knowledge (architecture + conventions + deployment) if available
    project_knowledge = ""
    try:
        project_knowledge = get_project_knowledge_for_prompt(mapping["repo_slug"])
        if project_knowledge:
            logger.info(
                "story_implementation: injecting %d chars of project knowledge (run_id=%s)",
                len(project_knowledge), run_id,
            )
    except Exception as pk_exc:
        logger.warning("story_implementation: get_project_knowledge_for_prompt failed (non-fatal): %s", pk_exc)

    update_run_step(run_id, "cloning")
    repo_path = clone_repo(
        run_id=run_id,
        issue_key=issue_key,
        repo_name=mapping["repo_slug"],
        target_branch=mapping["base_branch"],
    )
    logger.info("story_implementation: repo cloned to %s", repo_path)

    update_run_step(run_id, "analyzing")
    analysis = analyze_repo(repo_path)
    telegram_summary = format_telegram_summary(issue_key, mapping["repo_slug"], analysis)
    send_message("repo_analysis", "COMPLETE", telegram_summary)
    logger.info("story_implementation: analysis sent to Telegram")

    # Phase 15 — detect and persist capability profile (non-fatal)
    capability_profile: dict = {}
    try:
        capability_profile = detect_repo_capability_profile(repo_path, mapping["repo_slug"])
        upsert_capability_profile(mapping["repo_slug"], capability_profile)
        update_run_field(run_id, capability_profile_name=capability_profile.get("profile_name"))
        logger.info(
            "story_implementation: capability profile=%s for %s",
            capability_profile.get("profile_name"), mapping["repo_slug"],
        )
    except Exception as prof_exc:
        logger.warning("story_implementation: profile detection failed (non-fatal): %s", prof_exc)

    update_run_step(run_id, "summarizing")
    claude_summary = summarize_repo(repo_path, mapping["repo_slug"], analysis)
    send_message("claude_summary", "COMPLETE", f"{issue_key}:\n{claude_summary}")
    logger.info("story_implementation: Claude summary sent to Telegram")

    # --- Fetch story details early for clarification check ---
    early_story_details: dict = {"key": issue_key, "summary": summary, "description": None, "acceptance_criteria": []}
    try:
        early_story_details = get_issue_details(issue_key)
    except Exception as exc:
        logger.warning("story_implementation: early get_issue_details failed (non-fatal) — %s", exc)

    # --- Resume detection: inject answered clarification into suggestion context ---
    clarification_answer_text: str | None = None
    try:
        active_clar = get_active_clarification(run_id)
        if active_clar and active_clar["status"] == "ANSWERED":
            clarification_answer_text = active_clar.get("answer_text")
            logger.info(
                "story_implementation: resumed with answered clarification %s — injecting answer",
                active_clar["id"],
            )
            send_message(
                "clarification_resumed", "RUNNING",
                f"{issue_key}: resuming with clarification answer — {clarification_answer_text}",
            )
    except Exception as exc:
        logger.warning("story_implementation: clarification resume check failed (non-fatal) — %s", exc)

    # --- Clarification checkpoint: pause if Story is too ambiguous ---
    if clarification_answer_text is None and is_clarification_enabled():
        vague_question = _check_story_ambiguity(summary, early_story_details)
        if vague_question:
            logger.info("story_implementation: Story ambiguous — pausing for clarification (run_id=%s)", run_id)
            pause_for_clarification(
                run_id=run_id,
                question=vague_question,
                context_key=ClarificationContextKey.PRE_SUGGEST,
                context_summary=f"Story {issue_key}: {summary}",
                workflow_type="story_implementation",
                issue_key=issue_key,
                repo_slug=mapping["repo_slug"],
            )
            # pause_for_clarification raises ClarificationRequested — execution stops here

    # Inject clarification answer + project knowledge into memory context for Developer Agent
    suggest_memory = execution_memory
    if project_knowledge:
        pk_block = f"Project knowledge for {mapping['repo_slug']}:\n{project_knowledge}"
        suggest_memory = f"{suggest_memory}\n\n{pk_block}" if suggest_memory else pk_block
    if clarification_answer_text:
        answer_note = f"User clarification for this story: {clarification_answer_text}"
        suggest_memory = f"{suggest_memory}\n\n{answer_note}" if suggest_memory else answer_note

    update_run_step(run_id, "suggesting")
    suggestion_result = suggest_change(repo_path, analysis, issue_key=issue_key, issue_summary=summary, memory_context=suggest_memory)
    changes = suggestion_result.get("changes", [])
    suggestion_summary = suggestion_result.get("summary", "")
    files_str = ", ".join(c.get("file", "") for c in changes)
    suggestion_msg = f"{issue_key}: {len(changes)} change(s) in {files_str}\n{suggestion_summary}"
    send_message("claude_suggestion", "COMPLETE", suggestion_msg)
    logger.info("story_implementation: Claude suggestion sent to Telegram — %s", files_str)

    update_run_step(run_id, "applying")
    applied = apply_changes(repo_path, changes)
    if applied["applied"]:
        change_detail = f"{applied['count']} file(s): {', '.join(applied['files'])}"
        files_touched_str = ",".join(applied["files"])
        update_run_field(run_id, files_changed_count=applied["count"])
        logger.info("story_implementation: changes applied — %s", applied["files"])
    else:
        fallback = modify_file(repo_path)
        change_detail = f"{fallback['file']} — {fallback['change']} (fallback: {applied['reason']})"
        files_touched_str = fallback["file"]
        logger.info("story_implementation: apply_changes fallback — %s", applied["reason"])
    send_message("file_apply", "COMPLETE", f"{issue_key}: {change_detail}")

    # --- Attempt 1: run tests on the implementation ---
    _profile_test_cmd = capability_profile.get("test_command") if capability_profile else None
    _profile_name = capability_profile.get("profile_name") if capability_profile else None
    attempt_1_id = record_attempt(run_id, 1, "implement", "claude-sonnet-4-6")
    update_run_step(run_id, "testing")
    test_result = run_tests(
        repo_path,
        profile_command=_profile_test_cmd,
        profile_name=_profile_name,
    )
    update_run_field(
        run_id,
        test_status=test_result["status"],
        test_command=test_result["command"],
        test_output=test_result["output"],
        dependency_install_status=test_result.get("dependency_install", "NOT_RUN"),
    )
    send_message("tests", test_result["status"], f"{issue_key}: {test_result['status']}")
    logger.info("story_implementation: tests %s", test_result["status"])
    complete_attempt(
        attempt_1_id,
        status=test_result["status"],
        test_status=test_result["status"],
        files_touched=files_touched_str,
    )

    # --- Fix loop: one attempt if tests failed ---
    final_test_result = test_result
    fix_result: dict | None = None

    if test_result["status"] in ("FAILED", "ERROR"):
        update_run_field(run_id, retry_count=1)
        send_message(
            "fix_attempt_started", "RUNNING",
            f"{issue_key}: tests {test_result['status']} — attempting 1 fix",
        )
        logger.info("story_implementation: initiating fix attempt")

        attempt_2_id = record_attempt(run_id, 2, "fix", "claude-sonnet-4-6")
        update_run_step(run_id, "fixing")

        fix_result = fix_change(
            repo_path, analysis,
            issue_key=issue_key,
            issue_summary=summary,
            previous_changes=changes,
            test_output=test_result["output"],
            memory_context=execution_memory,
        )
        fix_applied = apply_changes(repo_path, fix_result.get("changes", []))
        if fix_applied["applied"]:
            fix_detail = f"{fix_applied['count']} file(s): {', '.join(fix_applied['files'])}"
            fix_files_str = ",".join(fix_applied["files"])
        else:
            fix_detail = f"fix not applied: {fix_applied.get('reason', 'unknown')}"
            fix_files_str = files_touched_str
        send_message("fix_apply", "COMPLETE", f"{issue_key}: {fix_detail}")
        logger.info("story_implementation: fix applied — %s", fix_detail)

        update_run_step(run_id, "retesting")
        retest_result = run_tests(
            repo_path,
            profile_command=_profile_test_cmd,
            profile_name=_profile_name,
        )
        update_run_field(
            run_id,
            test_status=retest_result["status"],
            test_command=retest_result["command"],
            test_output=retest_result["output"],
        )
        send_message(
            "tests", retest_result["status"],
            f"{issue_key}: retry — {retest_result['status']}",
        )
        logger.info("story_implementation: retest %s", retest_result["status"])

        complete_attempt(
            attempt_2_id,
            status=retest_result["status"],
            test_status=retest_result["status"],
            files_touched=fix_files_str,
        )

        if retest_result["status"] in ("FAILED", "ERROR"):
            complete_attempt(
                attempt_2_id,
                status="FAILED",
                failure_summary=f"Tests still failing after fix: {(retest_result.get('output') or '')[-300:]}",
                test_status=retest_result["status"],
                files_touched=fix_files_str,
            )
            send_message(
                "fix_attempt_failed", "FAILED",
                f"{issue_key}: tests still failing after fix — aborting",
            )
            logger.error("story_implementation: fix attempt failed — aborting")
            fail_run(
                run_id,
                f"Tests still failing after fix attempt. Status: {retest_result['status']}. "
                f"Output: {(retest_result.get('output') or '')[-400:]}",
            )
            return

        send_message("fix_attempt_passed", "COMPLETE", f"{issue_key}: fix succeeded — tests now passing")
        logger.info("story_implementation: fix attempt passed")
        final_test_result = retest_result

    # --- Phase 15: Build and lint (run after tests pass, non-fatal) ---
    _build_status = "NOT_RUN"
    _lint_status = "NOT_RUN"
    if capability_profile:
        from app.repo_profiler import get_build_command_for_profile, get_lint_command_for_profile
        from app.test_runner import run_build, run_lint
        _build_cmd = get_build_command_for_profile(capability_profile)
        _lint_cmd = get_lint_command_for_profile(capability_profile)
        if _build_cmd:
            update_run_step(run_id, "building")
            _build_result = run_build(
                repo_path=repo_path,
                build_command=_build_cmd,
                profile_name=_profile_name,
            )
            _build_status = _build_result["status"]
            update_run_field(run_id, build_status=_build_status)
            send_message("build", _build_status, f"{issue_key}: build {_build_status}")
            logger.info("story_implementation: build %s", _build_status)
        if _lint_cmd:
            update_run_step(run_id, "linting")
            _lint_result = run_lint(
                repo_path=repo_path,
                lint_command=_lint_cmd,
                profile_name=_profile_name,
            )
            _lint_status = _lint_result["status"]
            update_run_field(run_id, lint_status=_lint_status)
            send_message("lint", _lint_status, f"{issue_key}: lint {_lint_status}")
            logger.info("story_implementation: lint %s", _lint_status)

    # --- Commit and push ---
    suggestion_description = suggestion_summary or (changes[0].get("description", "") if changes else "") or summary
    commit_message = f"ai: {issue_key} — {suggestion_description}"

    update_run_step(run_id, "pushing")
    ensure_github_writes_allowed("push", mapping["repo_slug"], run_id)
    branch = commit_and_push(
        repo_path=repo_path,
        issue_key=issue_key,
        run_id=run_id,
        commit_message=commit_message,
    )

    if not branch:
        # Fix reverted all changes — working tree identical to base, nothing to push
        update_run_field(run_id, merge_status="SKIPPED")
        update_run_step(run_id, "done")
        send_message("pr_skipped", "COMPLETE", f"{issue_key}: fix reverted all changes — no PR created")
        logger.info("story_implementation: nothing to push (reverted to base) — skipping PR creation")
        return

    update_run_field(run_id, working_branch=branch)
    send_message("git_push", "COMPLETE", f"{issue_key}: branch {branch} pushed to GitHub")
    logger.info("story_implementation: pushed branch %s", branch)

    # Build per-file unified diffs — prefer fix changes if a fix was applied
    final_changes = fix_result.get("changes", []) if fix_result else changes
    diff_parts = []
    for ch in final_changes:
        orig = ch.get("original", "").splitlines(keepends=True)
        repl = ch.get("replacement", "").splitlines(keepends=True)
        file_diff = list(difflib.unified_diff(
            orig, repl,
            fromfile=f"a/{ch.get('file', 'unknown')}",
            tofile=f"b/{ch.get('file', 'unknown')}",
            lineterm="",
        ))
        if file_diff:
            diff_parts.append("".join(file_diff))
        else:
            diff_parts.append(
                f"--- a/{ch.get('file', 'unknown')}\n"
                f"+++ b/{ch.get('file', 'unknown')}\n"
                f"- {ch.get('original', '').strip()}\n"
                f"+ {ch.get('replacement', '').strip()}"
            )
    diff_block = "\n".join(diff_parts)

    # Validation checklist
    py_files = [ch.get("file", "") for ch in final_changes if ch.get("file", "").endswith(".py")]
    syntax_line = f"- [x] Python syntax check (ast.parse) — {', '.join(py_files)}\n" if py_files else ""
    validation_section = (
        "## Pre-apply validation\n"
        "- [x] Path traversal guard\n"
        "- [x] File exists in repo\n"
        "- [x] Original text found\n"
        "- [x] No-op guard (original ≠ replacement)\n"
        f"{syntax_line}"
    )

    attempt_number = 2 if fix_result else 1
    test_section = _build_test_section(final_test_result, attempt=attempt_number)

    retry_note = ""
    if fix_result:
        retry_note = (
            "**Note:** Initial implementation failed tests. "
            "One fix attempt was made and tests now pass.\n\n"
        )

    files_list = "\n".join(
        f"- `{ch.get('file', 'N/A')}`: {ch.get('description', '')}"
        for ch in final_changes
    )

    pr_body = (
        f"> 🤖 Automated PR — [AI Dev Orchestrator](https://github.com/suyog19/ai-dev-orchestrator)\n\n"
        f"**Issue:** {issue_key}  \n"
        f"**Story:** {summary}\n\n"
        f"---\n\n"
        f"## Summary\n{claude_summary}\n\n"
        f"---\n\n"
        f"## Changes\n"
        f"{retry_note}"
        f"{files_list}\n\n"
        f"```diff\n{diff_block}\n```\n\n"
        f"---\n\n"
        f"{test_section}\n"
        f"---\n\n"
        f"{validation_section}\n"
        f"---\n\n"
        f"## Review checklist\n"
        f"- [ ] Change matches the story intent\n"
        f"- [ ] No unintended side effects\n"
        f"- [ ] Tests added or updated if applicable\n"
        f"- [ ] Ready to merge\n"
    )

    update_run_step(run_id, "creating_pr")
    ensure_github_writes_allowed("create_pr", mapping["repo_slug"], run_id)
    ensure_label(mapping["repo_slug"], AI_LABEL, color=AI_LABEL_COLOR, description="Opened by AI Dev Orchestrator")
    pr = create_pull_request(
        repo_name=mapping["repo_slug"],
        head_branch=branch,
        base_branch=mapping["base_branch"],
        title=f"ai: {issue_key} — {suggestion_description}",
        body=pr_body,
    )
    add_label_to_pr(mapping["repo_slug"], pr["number"], AI_LABEL)
    update_run_field(run_id, pr_url=pr["url"])

    # Store the head SHA for GitHub commit status publishing (Phase 13)
    try:
        pr_details = get_pr_details(mapping["repo_slug"], pr["number"])
        update_run_field(run_id, head_sha=pr_details["head_sha"])
        logger.info("story_implementation: head_sha=%s stored for run %s", pr_details["head_sha"][:8], run_id)
    except Exception as exc:
        logger.warning("story_implementation: get_pr_details failed (non-fatal) — %s", exc)

    send_message("pr_created", "COMPLETE", f"{issue_key}: PR #{pr['number']} — {pr['url']}")
    logger.info("story_implementation: PR #%s at %s", pr["number"], pr["url"])

    # --- Build review input package (Reviewer Agent wired in Iteration 3) ---
    update_run_step(run_id, "building_review_package")
    story_details: dict = {"key": issue_key, "summary": summary, "description": None, "acceptance_criteria": []}
    try:
        story_details = get_issue_details(issue_key)
    except Exception as exc:
        logger.warning("story_implementation: get_issue_details failed (non-fatal) — %s", exc)

    review_retry_count = 1 if fix_result else 0
    review_package = _build_review_package(
        issue_key=issue_key,
        summary=summary,
        story_details=story_details,
        mapping=mapping,
        branch=branch,
        pr=pr,
        commit_message=commit_message,
        final_changes=final_changes,
        diff_block=diff_block,
        final_test_result=final_test_result,
        retry_count=review_retry_count,
        execution_memory=execution_memory,
    )
    logger.info(
        "story_implementation: review_package assembled — story=%s pr=#%s files=%s test=%s ac_items=%d",
        issue_key,
        review_package["pr_context"]["number"],
        review_package["pr_context"]["files_changed"],
        review_package["test_result"]["status"],
        len(review_package["story_context"]["acceptance_criteria"]),
    )

    # --- Run Reviewer Agent ---
    update_run_step(run_id, "reviewing")
    verdict: dict = {}
    try:
        verdict = review_pr(**review_package)
        store_agent_review(
            run_id=run_id,
            verdict=verdict,
            pr_number=pr["number"],
            pr_url=pr["url"],
            repo_slug=mapping["repo_slug"],
            story_key=issue_key,
        )
        logger.info(
            "story_implementation: review complete — status=%s risk=%s",
            verdict.get("review_status"), verdict.get("risk_level"),
        )
        try:
            post_pr_comment(mapping["repo_slug"], pr["number"], _format_review_comment(verdict))
            logger.info("story_implementation: review comment posted to PR #%s", pr["number"])
        except Exception as comment_exc:
            logger.warning("story_implementation: PR comment failed (non-fatal) — %s", comment_exc)
        send_message(
            "review_completed",
            verdict.get("review_status", "UNKNOWN"),
            (
                f"{issue_key}: PR #{pr['number']}\n"
                f"Verdict: {verdict.get('review_status')}\n"
                f"Risk: {verdict.get('risk_level')}\n"
                f"{verdict.get('summary', '')}"
            ),
        )
        # Reviewer requests clarification — pause workflow
        if verdict.get("needs_clarification") and is_clarification_enabled():
            pause_for_clarification(
                run_id=run_id,
                question=verdict.get("clarification_question", "Reviewer needs clarification to proceed."),
                context_key=ClarificationContextKey.PRE_REVIEW,
                context_summary=verdict.get("clarification_context_summary") or f"PR #{pr['number']} review",
                options=verdict.get("clarification_options"),
                workflow_type="story_implementation",
                issue_key=issue_key,
                repo_slug=mapping["repo_slug"],
            )
            # pause_for_clarification raises ClarificationRequested — execution stops here
    except Exception as exc:
        logger.error("story_implementation: Reviewer Agent failed — %s", exc)
        error_verdict = {
            "review_status": ReviewStatus.ERROR,
            "risk_level": "HIGH",
            "summary": f"Reviewer Agent error: {exc}",
            "findings": [],
            "blocking_reasons": [str(exc)],
            "recommendations": [],
        }
        try:
            store_agent_review(
                run_id=run_id,
                verdict=error_verdict,
                pr_number=pr["number"],
                pr_url=pr["url"],
                repo_slug=mapping["repo_slug"],
                story_key=issue_key,
            )
        except Exception as db_exc:
            logger.error("story_implementation: store_agent_review failed — %s", db_exc)
        try:
            post_pr_comment(mapping["repo_slug"], pr["number"], _format_review_comment(error_verdict))
        except Exception as comment_exc:
            logger.warning("story_implementation: error PR comment failed (non-fatal) — %s", comment_exc)
        send_message(
            "review_error", "ERROR",
            f"{issue_key}: Reviewer Agent error — {exc}",
        )
        verdict = error_verdict

    # --- Run Test Quality Agent ---
    update_run_step(run_id, "test_quality_review")
    tq_verdict: dict = {}
    try:
        tq_package = _build_test_quality_package(
            issue_key=issue_key,
            summary=summary,
            story_details=story_details,
            mapping=mapping,
            pr=pr,
            final_changes=final_changes,
            diff_block=diff_block,
            final_test_result=final_test_result,
            retry_count=review_retry_count,
            execution_memory=execution_memory,
            profile_name=_profile_name,
        )
        tq_verdict = review_test_quality(**tq_package)
        store_test_quality_review(
            run_id=run_id,
            verdict=tq_verdict,
            pr_number=pr["number"],
            pr_url=pr["url"],
            repo_slug=mapping["repo_slug"],
            story_key=issue_key,
        )
        logger.info(
            "story_implementation: test_quality complete — status=%s confidence=%s",
            tq_verdict.get("quality_status"), tq_verdict.get("confidence_level"),
        )
        try:
            post_pr_comment(mapping["repo_slug"], pr["number"], _format_test_quality_comment(tq_verdict))
            logger.info("story_implementation: test quality comment posted to PR #%s", pr["number"])
        except Exception as comment_exc:
            logger.warning("story_implementation: TQ PR comment failed (non-fatal) — %s", comment_exc)
        send_message(
            "test_quality_completed",
            tq_verdict.get("quality_status", "UNKNOWN"),
            (
                f"{issue_key}: PR #{pr['number']}\n"
                f"Test Quality: {tq_verdict.get('quality_status')}\n"
                f"Confidence: {tq_verdict.get('confidence_level')}\n"
                f"{tq_verdict.get('summary', '')}"
            ),
        )
    except Exception as exc:
        logger.error("story_implementation: Test Quality Agent failed — %s", exc)
        tq_verdict = {
            "quality_status": TestQualityStatus.ERROR,
            "confidence_level": "LOW",
            "summary": f"Test Quality Agent error: {exc}",
            "coverage_findings": [],
            "missing_tests": [str(exc)],
            "suspicious_tests": [],
            "recommendations": [],
        }
        try:
            store_test_quality_review(
                run_id=run_id,
                verdict=tq_verdict,
                pr_number=pr["number"],
                pr_url=pr["url"],
                repo_slug=mapping["repo_slug"],
                story_key=issue_key,
            )
        except Exception as db_exc:
            logger.error("story_implementation: store_test_quality_review failed — %s", db_exc)
        try:
            post_pr_comment(mapping["repo_slug"], pr["number"], _format_test_quality_comment(tq_verdict))
        except Exception as comment_exc:
            logger.warning("story_implementation: TQ error comment failed (non-fatal) — %s", comment_exc)
        send_message(
            "test_quality_error", "ERROR",
            f"{issue_key}: Test Quality Agent error — {exc}",
        )

    # --- Run Architecture Agent ---
    update_run_step(run_id, "architecture_review")
    arch_verdict: dict = {}
    try:
        arch_package = _build_architecture_review_package(
            issue_key=issue_key,
            summary=summary,
            story_details=story_details,
            mapping=mapping,
            pr=pr,
            final_changes=final_changes,
            diff_block=diff_block,
            final_test_result=final_test_result,
            verdict=verdict,
            tq_verdict=tq_verdict,
            retry_count=review_retry_count,
            execution_memory=execution_memory,
            repo_analysis=analysis,
            profile_name=_profile_name,
        )
        arch_verdict = review_architecture(**arch_package)
        store_architecture_review(
            run_id=run_id,
            verdict=arch_verdict,
            pr_number=pr["number"],
            pr_url=pr["url"],
            repo_slug=mapping["repo_slug"],
            story_key=issue_key,
        )
        logger.info(
            "story_implementation: architecture_review complete — status=%s risk=%s",
            arch_verdict.get("architecture_status"), arch_verdict.get("risk_level"),
        )
        try:
            post_pr_comment(mapping["repo_slug"], pr["number"], _format_architecture_comment(arch_verdict))
            logger.info("story_implementation: architecture comment posted to PR #%s", pr["number"])
        except Exception as comment_exc:
            logger.warning("story_implementation: architecture PR comment failed (non-fatal) — %s", comment_exc)
        send_message(
            "architecture_review_completed",
            arch_verdict.get("architecture_status", "UNKNOWN"),
            (
                f"{issue_key}: PR #{pr['number']}\n"
                f"Architecture: {arch_verdict.get('architecture_status')}\n"
                f"Risk: {arch_verdict.get('risk_level')}\n"
                f"{arch_verdict.get('summary', '')}"
            ),
        )
        # Architecture Agent requests clarification — pause workflow
        if arch_verdict.get("needs_clarification") and is_clarification_enabled():
            pause_for_clarification(
                run_id=run_id,
                question=arch_verdict.get("clarification_question", "Architecture Agent needs clarification."),
                context_key=ClarificationContextKey.PRE_REVIEW,
                context_summary=arch_verdict.get("clarification_context_summary") or f"PR #{pr['number']} arch review",
                options=arch_verdict.get("clarification_options"),
                workflow_type="story_implementation",
                issue_key=issue_key,
                repo_slug=mapping["repo_slug"],
            )
            # pause_for_clarification raises ClarificationRequested — execution stops here
    except Exception as exc:
        logger.error("story_implementation: Architecture Agent failed — %s", exc)
        arch_verdict = {
            "architecture_status": ArchitectureStatus.ERROR,
            "risk_level": "HIGH",
            "summary": f"Architecture Agent error: {exc}",
            "impact_areas": [],
            "blocking_reasons": [str(exc)],
            "recommendations": [],
        }
        try:
            store_architecture_review(
                run_id=run_id,
                verdict=arch_verdict,
                pr_number=pr["number"],
                pr_url=pr["url"],
                repo_slug=mapping["repo_slug"],
                story_key=issue_key,
            )
        except Exception as db_exc:
            logger.error("story_implementation: store_architecture_review failed — %s", db_exc)
        try:
            post_pr_comment(mapping["repo_slug"], pr["number"], _format_architecture_comment(arch_verdict))
        except Exception as comment_exc:
            logger.warning("story_implementation: architecture error comment failed (non-fatal) — %s", comment_exc)
        send_message(
            "architecture_review_error", "ERROR",
            f"{issue_key}: Architecture Agent error — {exc}",
        )

    # --- Unified Release Gate ---
    pr_title = f"ai: {issue_key} — {suggestion_description}"
    review_status = verdict.get("review_status", ReviewStatus.ERROR)
    test_quality_status = tq_verdict.get("quality_status", TestQualityStatus.ERROR)
    architecture_status = arch_verdict.get("architecture_status", ArchitectureStatus.ERROR)

    update_run_step(run_id, "release_gate")
    _first_use = is_first_use_mode_active(mapping.get("repo_slug", ""))
    release = evaluate_release_decision(
        mapping=mapping,
        final_test_result=final_test_result,
        applied=applied,
        review_status=review_status,
        test_quality_status=test_quality_status,
        architecture_status=architecture_status,
        build_status=_build_status,
        lint_status=_lint_status,
        capability_profile=capability_profile,
        first_use_mode_active=_first_use,
    )
    update_run_field(
        run_id,
        release_decision=release["release_decision"],
        release_decision_reason=release["reason"],
        release_decided_at=datetime.now(timezone.utc),
    )
    logger.info(
        "story_implementation: release_gate — decision=%s reason=%s first_use=%s",
        release["release_decision"], release["reason"], _first_use,
    )

    # --- Publish GitHub commit statuses (Phase 13) ---
    update_run_step(run_id, "publishing_github_statuses")
    try:
        from app.github_status_publisher import publish_github_statuses_for_run
        ensure_github_writes_allowed("status", mapping["repo_slug"], run_id)
        pub_result = publish_github_statuses_for_run(run_id, mapping["repo_slug"], pr["number"])
        if pub_result["skipped"]:
            logger.warning("story_implementation: github status publish skipped — %s", pub_result["errors"])
        elif pub_result["failed"] > 0:
            send_message(
                "github_status_publish_failed", "WARNING",
                f"{issue_key}: {pub_result['failed']}/{pub_result['published'] + pub_result['failed']} "
                f"GitHub statuses failed — {'; '.join(pub_result['errors'][:2])}",
            )
        else:
            logger.info("story_implementation: %d GitHub statuses published", pub_result["published"])
    except Exception as exc:
        logger.error("story_implementation: github status publish error (non-fatal) — %s", exc)
        send_message("github_status_publish_failed", "WARNING", f"{issue_key}: GitHub status publish error — {exc}")

    update_run_step(run_id, "merge_check")
    if release["can_auto_merge"]:
        try:
            ensure_github_writes_allowed("merge_pr", mapping["repo_slug"], run_id)
            merge_pull_request(mapping["repo_slug"], pr["number"], pr_title)
            update_run_field(run_id, merge_status="MERGED", merged_at=datetime.now(timezone.utc))
            send_message("pr_merged", "COMPLETE", f"{issue_key}: PR #{pr['number']} auto-merged (squash)")
            logger.info("story_implementation: PR #%s auto-merged", pr["number"])
            _run_post_merge_validation(
                run_id=run_id,
                issue_key=issue_key,
                repo_slug=mapping["repo_slug"],
                commit_sha=get_run_state(run_id).get("head_sha"),
                pr_number=pr["number"],
            )
        except Exception as exc:
            update_run_field(run_id, merge_status="FAILED")
            send_message("pr_merge_failed", "FAILED", f"{issue_key}: auto-merge failed — {exc}")
            logger.error("story_implementation: auto-merge failed — %s", exc)
    elif release["release_decision"] == ReleaseDecision.BLOCKED:
        blocking_gates = release.get("blocking_gates") or []
        # Determine the most specific merge_status for observability
        if review_status == ReviewStatus.BLOCKED:
            merge_status_val = "BLOCKED_BY_REVIEW"
        elif test_quality_status == TestQualityStatus.BLOCKING:
            merge_status_val = "BLOCKED_BY_TEST_QUALITY"
        elif architecture_status == ArchitectureStatus.BLOCKED:
            merge_status_val = "BLOCKED_BY_ARCHITECTURE"
        else:
            merge_status_val = "BLOCKED_BY_RELEASE_GATE"
        update_run_field(run_id, merge_status=merge_status_val)
        send_message(
            "merge_blocked_by_release_gate", "BLOCKED",
            f"{issue_key}: PR #{pr['number']} merge blocked\n"
            f"Gates: {'; '.join(blocking_gates)}",
        )
        logger.info("story_implementation: merge blocked — %s — %s", merge_status_val, blocking_gates)
    else:
        update_run_field(run_id, merge_status="SKIPPED")
        send_message("pr_merge_skipped", "COMPLETE", f"{issue_key}: auto-merge skipped ({release['reason']})")
        logger.info("story_implementation: auto-merge skipped — %s", release["reason"])

    update_run_step(run_id, "done")


# ---------------------------------------------------------------------------
# Phase 6 — Planning workflows
# ---------------------------------------------------------------------------

def epic_breakdown(run_id: int, issue_key: str, issue_type: str, summary: str) -> None:
    """Epic → Story breakdown workflow — Phase 6."""
    logger.info("epic_breakdown: starting for %s — %s", issue_key, summary)

    update_run_step(run_id, "planning")
    update_run_field(run_id, parent_issue_key=issue_key)
    send_message("epic_breakdown_started", "RUNNING", f"{issue_key}: {summary}")

    # --- Idempotency guard: block if a prior run already created children for this Epic ---
    existing = get_created_children_for_epic(issue_key, exclude_run_id=run_id)
    if existing:
        logger.warning(
            "epic_breakdown: duplicate blocked — %s already has %d Stories from run %s",
            issue_key, existing["count"], existing["run_id"],
        )
        fail_run(
            run_id,
            f"Duplicate breakdown blocked: {issue_key} already has {existing['count']} "
            f"Stories created by run {existing['run_id']}.",
        )
        record_planning_feedback(run_id)
        send_message(
            "planning_duplicate_blocked", "BLOCKED",
            f"{issue_key}: already has {existing['count']} Stories from run {existing['run_id']}.\n"
            f"To rebuild from scratch, send:\n  REGENERATE {existing['run_id']}",
        )
        return

    # --- Memory context for planning prompt ---
    jira_project_key = issue_key.split("-")[0]
    memory_context = ""
    try:
        story_mapping = get_mapping(jira_project_key, "Story")
        if story_mapping:
            repo_slug = story_mapping["repo_slug"]
            memory_context = get_planning_memory(repo_slug, issue_key)
            if memory_context:
                logger.info(
                    "epic_breakdown: injecting %d chars of memory context (run_id=%s)",
                    len(memory_context), run_id,
                )
    except Exception as mem_exc:
        logger.warning("epic_breakdown: get_planning_memory failed (non-fatal): %s", mem_exc)

    # Phase 17 — inject project knowledge into epic planning if available
    try:
        if story_mapping:
            pk = get_project_knowledge_for_prompt(story_mapping["repo_slug"])
            if pk:
                pk_block = f"Project knowledge for {story_mapping['repo_slug']}:\n{pk}"
                memory_context = f"{memory_context}\n\n{pk_block}" if memory_context else pk_block
                logger.info(
                    "epic_breakdown: injecting %d chars of project knowledge (run_id=%s)",
                    len(pk), run_id,
                )
    except Exception as pk_exc:
        logger.warning("epic_breakdown: get_project_knowledge_for_prompt failed (non-fatal): %s", pk_exc)

    # --- Fetch Epic details for vagueness/ambiguity check and full breakdown context ---
    epic_description: str | None = None
    epic_acceptance_criteria: list[str] = []
    try:
        epic_details = get_issue_details(issue_key)
        epic_description = epic_details.get("description")
        epic_acceptance_criteria = epic_details.get("acceptance_criteria") or []
    except Exception as exc:
        logger.warning("epic_breakdown: get_issue_details failed (non-fatal) — %s", exc)

    # --- Resume detection: inject answered clarification into planning context ---
    clarification_answer_text: str | None = None
    try:
        active_clar = get_active_clarification(run_id)
        if active_clar and active_clar["status"] == "ANSWERED":
            clarification_answer_text = active_clar.get("answer_text")
            logger.info(
                "epic_breakdown: resumed with answered clarification %s — injecting answer",
                active_clar["id"],
            )
            send_message(
                "clarification_resumed", "RUNNING",
                f"{issue_key}: resuming epic planning with clarification answer — {clarification_answer_text}",
            )
    except Exception as exc:
        logger.warning("epic_breakdown: clarification resume check failed (non-fatal) — %s", exc)

    # --- Clarification checkpoint: pause if Epic is too vague or missing specifics ---
    if clarification_answer_text is None and is_clarification_enabled():
        # Check 1: structural vagueness (short/empty description)
        vague_question = _check_epic_vagueness(summary, epic_description)
        if vague_question:
            logger.info("epic_breakdown: Epic vague — pausing for clarification (run_id=%s)", run_id)
            pause_for_clarification(
                run_id=run_id,
                question=vague_question,
                context_key=ClarificationContextKey.PRE_PLANNING,
                context_summary=f"Epic {issue_key}: {summary}",
                workflow_type="epic_breakdown",
                issue_key=issue_key,
            )
            # pause_for_clarification raises ClarificationRequested — execution stops here

        # Check 2: content ambiguity — missing specifics Claude would have to assume
        else:
            try:
                missing_qs = detect_epic_missing_specifics(
                    issue_key, summary, epic_description, epic_acceptance_criteria,
                )
                if missing_qs:
                    question = (
                        f"Before breaking down Epic {issue_key} ('{summary}') into Stories, "
                        f"I need a few specifics to avoid making assumptions:\n\n"
                        + "\n".join(f"{i+1}. {q}" for i, q in enumerate(missing_qs))
                        + "\n\nPlease reply with answers to each point."
                    )
                    logger.info(
                        "epic_breakdown: %d missing specifics detected — pausing (run_id=%s)",
                        len(missing_qs), run_id,
                    )
                    pause_for_clarification(
                        run_id=run_id,
                        question=question,
                        context_key=ClarificationContextKey.PRE_PLANNING,
                        context_summary=f"Epic {issue_key}: {summary}",
                        workflow_type="epic_breakdown",
                        issue_key=issue_key,
                    )
                    # pause_for_clarification raises ClarificationRequested — execution stops here
            except Exception as exc:
                logger.warning("epic_breakdown: ambiguity check failed (non-fatal) — %s", exc)

    # Inject clarification answer into memory context for planning prompt
    plan_memory = memory_context
    if clarification_answer_text:
        answer_note = f"User clarification for this epic: {clarification_answer_text}"
        plan_memory = f"{plan_memory}\n\n{answer_note}" if plan_memory else answer_note

    # --- Claude decomposition ---
    update_run_step(run_id, "decomposing")
    try:
        plan = plan_epic_breakdown(
            issue_key, summary,
            description=epic_description,
            acceptance_criteria=epic_acceptance_criteria,
            memory_context=plan_memory,
        )
    except Exception as exc:
        logger.error("epic_breakdown: Claude decomposition failed — %s", exc)
        fail_run(run_id, f"Epic decomposition failed: {exc}")
        record_planning_feedback(run_id)
        send_message("epic_breakdown_failed", "FAILED", f"{issue_key}: {exc}")
        return

    items = plan.get("items", [])
    logger.info("epic_breakdown: Claude proposed %d Stories for %s", len(items), issue_key)

    # --- Persist proposals ---
    output_ids = []
    for i, item in enumerate(items, start=1):
        ac_list = item.get("acceptance_criteria", [])
        ac_text = "\n".join(f"- {c}" for c in ac_list) if ac_list else None
        oid = add_planning_output(
            run_id=run_id,
            parent_issue_key=issue_key,
            parent_issue_type=issue_type,
            proposed_issue_type="Story",
            sequence_number=i,
            title=item.get("title", f"Story {i}"),
            description=item.get("description"),
            acceptance_criteria=ac_text,
            rationale=item.get("rationale"),
            dependency_notes=item.get("dependency_notes") or None,
            risk_notes=item.get("risk_notes") or None,
            confidence=item.get("confidence"),
        )
        output_ids.append(oid)

    logger.info("epic_breakdown: stored %d proposals (run_id=%s)", len(output_ids), run_id)

    # --- Persist metadata for API inspection ---
    assumptions = plan.get("assumptions") or []
    open_questions = plan.get("open_questions") or []
    store_planning_metadata(run_id, assumptions, open_questions)

    # --- Request approval ---
    request_planning_approval(run_id)
    set_run_waiting_for_approval(run_id)

    story_lines = "\n".join(
        f"  {i}. {item.get('title', '?')} [{item.get('confidence', '?')}]"
        for i, item in enumerate(items, 1)
    )
    extra = ""
    if assumptions:
        extra += "\nAssumptions:\n" + "\n".join(f"  - {a}" for a in assumptions)
    if open_questions:
        extra += "\nOpen questions:\n" + "\n".join(f"  - {q}" for q in open_questions)

    approval_msg = (
        f"Epic: {issue_key}\n"
        f"Summary: {summary}\n\n"
        f"Proposed Stories ({len(items)}):\n{story_lines}"
        f"{extra}\n\n"
        f"Run ID: {run_id}\n\n"
        f"Reply with:\n"
        f"  APPROVE {run_id}\n"
        f"  REJECT {run_id}\n"
        f"  REGENERATE {run_id}"
    )
    send_message("epic_breakdown_proposed", "PENDING", approval_msg)
    logger.info("epic_breakdown: approval request sent to Telegram (run_id=%s)", run_id)


def create_jira_stories_for_run(run_id: int, issue_key: str) -> None:
    """Create Jira Story issues for all PROPOSED planning_outputs of an approved run.

    Called inline from the Telegram APPROVE handler. Creates Stories sequentially,
    persisting each created key before moving to the next. If any creation fails,
    marks the run as FAILED with details of what was already created.
    """
    from app.jira_client import create_story_under_epic

    project_key = issue_key.split("-")[0]
    outputs = get_planning_outputs(run_id)
    proposed = [o for o in outputs if o["status"] == "PROPOSED"]

    if not proposed:
        logger.warning("create_jira_stories_for_run: no PROPOSED outputs for run %s", run_id)
        complete_planning_run(run_id, 0)
        send_message("epic_breakdown_complete", "COMPLETE", f"{issue_key}: 0 Stories to create (none proposed)")
        return

    update_run_step(run_id, "creating_jira_issues")
    send_message(
        "epic_breakdown_approved", "RUNNING",
        f"{issue_key}: creating {len(proposed)} Stories in Jira (run_id={run_id})",
    )

    created_pairs: list[tuple[str, str]] = []  # (jira_key, title)
    for output in proposed:
        try:
            jira_key = create_story_under_epic(
                project_key=project_key,
                epic_key=issue_key,
                title=output["title"],
                run_id=run_id,
                description=output.get("description"),
                acceptance_criteria=output.get("acceptance_criteria"),
                rationale=output.get("rationale"),
                dependency_notes=output.get("dependency_notes"),
                risk_notes=output.get("risk_notes"),
            )
            update_planning_output_status(output["id"], "CREATED", jira_key)
            created_pairs.append((jira_key, output["title"]))
            logger.info(
                "create_jira_stories_for_run: created %s — %s",
                jira_key, output["title"][:60],
            )
        except Exception as exc:
            partial = ", ".join(k for k, _ in created_pairs) or "none"
            logger.error(
                "create_jira_stories_for_run: failed at seq %s — %s",
                output["sequence_number"], exc,
            )
            fail_run(
                run_id,
                f"Jira creation failed at Story {output['sequence_number']}: {exc}. "
                f"Already created: {partial}",
            )
            send_message(
                "epic_breakdown_failed", "FAILED",
                f"{issue_key}: Jira creation failed at Story {output['sequence_number']}\n"
                f"Error: {exc}\n"
                f"Already created: {partial}",
            )
            return

    complete_planning_run(run_id, len(created_pairs))
    n_events = record_planning_feedback(run_id)
    logger.info("create_jira_stories_for_run: recorded %d feedback events (run_id=%s)", n_events, run_id)
    story_lines = "\n".join(f"  {k}: {t}" for k, t in created_pairs)
    send_message(
        "epic_breakdown_complete", "COMPLETE",
        f"{issue_key}: {len(created_pairs)} Stories created (run_id={run_id})\n{story_lines}",
    )
    logger.info(
        "create_jira_stories_for_run: %d Stories created for %s — %s",
        len(created_pairs), issue_key, ", ".join(k for k, _ in created_pairs),
    )
