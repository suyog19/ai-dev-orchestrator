import difflib
import logging
from datetime import datetime, timezone
from app.repo_mapping import get_mapping
from app.git_ops import clone_repo, commit_and_push
from app.github_api import create_pull_request, ensure_label, add_label_to_pr, merge_pull_request, post_pr_comment
from app.repo_analysis import analyze_repo, format_telegram_summary
from app.claude_client import summarize_repo, suggest_change, fix_change, plan_epic_breakdown, MAX_STORIES_PER_EPIC, review_pr, review_test_quality, review_architecture
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
    get_planning_memory, get_execution_memory,
    store_agent_review,
    store_test_quality_review,
    store_architecture_review,
)
from app.feedback import ReviewStatus, TestQualityStatus, ArchitectureStatus, ReleaseDecision
from app.test_runner import run_tests
from app.security import ensure_github_writes_allowed

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
    """Return True if a file path looks like a test file."""
    return any(p in path for p in _TEST_FILE_PATTERNS)


_SKIP_PATTERNS = ("@pytest.mark.skip", "pytest.skip(", ".skip(", "skipTest(")

def _detect_skipped_tests(diff: str, test_output: str) -> bool:
    """Return True if skipped tests are detected in the diff or test output."""
    combined = (diff or "") + (test_output or "")
    lower = combined.lower()
    if "skipped" in lower:
        return True
    return any(p in combined for p in _SKIP_PATTERNS)


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
) -> dict:
    """Assemble the full context package for the Test Quality Agent.

    Returns a dict with keys that unpack directly into review_test_quality(**pkg).
    No Claude call, no DB write, no secrets included.
    """
    all_files = [ch.get("file", "") for ch in final_changes if ch.get("file")]
    source_files = [f for f in all_files if not _is_test_file(f)]
    test_files = [f for f in all_files if _is_test_file(f)]

    output = (final_test_result.get("output") or "").strip()
    output_excerpt = "\n".join(output.splitlines()[-30:]) if output else ""
    skipped = _detect_skipped_tests(diff_block, output)

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


# --- Architecture / Impact Agent helpers (Phase 10) ---

_API_PATTERNS     = ("main.py", "routes", "router", "endpoints", "api", "views")
_MODEL_PATTERNS   = ("model", "schema", "models", "schemas", "entity")
_STORAGE_PATTERNS = ("database", "db", "migration", "alembic", "repository", "repo")
_CONFIG_PATTERNS  = (".env", "config", "settings", "constants")
_DOC_PATTERNS     = ("readme", ".md", "docs/", "changelog")


def _classify_changed_files(files: list[str]) -> dict:
    """Group changed files by architectural layer for the Architecture Agent."""
    groups: dict[str, list[str]] = {
        "api": [], "model": [], "storage": [], "config": [], "test": [], "docs": [], "other": [],
    }
    for f in files:
        fl = f.lower()
        if _is_test_file(f):
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
) -> dict:
    """Assemble context for the Architecture Agent.

    Returns a dict that unpacks directly into review_architecture(**pkg).
    No Claude call, no DB write, no secrets included.
    """
    all_files = [ch.get("file", "") for ch in final_changes if ch.get("file")]
    lang = repo_analysis.get("primary_language", "unknown")
    framework = repo_analysis.get("framework", "unknown")

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


def evaluate_release_decision(
    mapping: dict,
    final_test_result: dict,
    applied: dict,
    review_status: str,
    test_quality_status: str,
    architecture_status: str,
) -> dict:
    """Evaluate all agent gates and return a unified release decision.

    Returns a dict with keys:
    - release_decision: RELEASE_APPROVED | RELEASE_SKIPPED | RELEASE_BLOCKED | RELEASE_ERROR
    - can_auto_merge: bool
    - reason: str
    - blocking_gates: list[str]
    - warnings: list[str]
    """
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
    if not mapping.get("auto_merge_enabled"):
        skip_reasons.append("auto_merge disabled for repo")
    if final_test_result.get("status") not in ("PASSED",):
        skip_reasons.append(f"tests {final_test_result.get('status', 'NOT_RUN')}")
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


def story_implementation(run_id: int, issue_key: str, issue_type: str, summary: str) -> None:
    logger.info("story_implementation: starting %s (%s) — %s", issue_key, issue_type, summary)

    jira_project_key = issue_key.split("-")[0]

    update_run_step(run_id, "mapping_lookup")
    mapping = get_mapping(jira_project_key, issue_type)
    if not mapping:
        logger.warning("No repo mapping found for project=%s issue_type=%s — aborting", jira_project_key, issue_type)
        return

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

    update_run_step(run_id, "summarizing")
    claude_summary = summarize_repo(repo_path, mapping["repo_slug"], analysis)
    send_message("claude_summary", "COMPLETE", f"{issue_key}:\n{claude_summary}")
    logger.info("story_implementation: Claude summary sent to Telegram")

    update_run_step(run_id, "suggesting")
    suggestion_result = suggest_change(repo_path, analysis, issue_key=issue_key, issue_summary=summary, memory_context=execution_memory)
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
    attempt_1_id = record_attempt(run_id, 1, "implement", "claude-sonnet-4-6")
    update_run_step(run_id, "testing")
    test_result = run_tests(repo_path)
    update_run_field(
        run_id,
        test_status=test_result["status"],
        test_command=test_result["command"],
        test_output=test_result["output"],
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
        retest_result = run_tests(repo_path)
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
    release = evaluate_release_decision(
        mapping=mapping,
        final_test_result=final_test_result,
        applied=applied,
        review_status=review_status,
        test_quality_status=test_quality_status,
        architecture_status=architecture_status,
    )
    update_run_field(
        run_id,
        release_decision=release["release_decision"],
        release_decision_reason=release["reason"],
        release_decided_at=datetime.now(timezone.utc),
    )
    logger.info(
        "story_implementation: release_gate — decision=%s reason=%s",
        release["release_decision"], release["reason"],
    )

    update_run_step(run_id, "merge_check")
    if release["can_auto_merge"]:
        try:
            ensure_github_writes_allowed("merge_pr", mapping["repo_slug"], run_id)
            merge_pull_request(mapping["repo_slug"], pr["number"], pr_title)
            update_run_field(run_id, merge_status="MERGED", merged_at=datetime.now(timezone.utc))
            send_message("pr_merged", "COMPLETE", f"{issue_key}: PR #{pr['number']} auto-merged (squash)")
            logger.info("story_implementation: PR #%s auto-merged", pr["number"])
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

    # --- Claude decomposition ---
    update_run_step(run_id, "decomposing")
    try:
        plan = plan_epic_breakdown(issue_key, summary, memory_context=memory_context)
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
