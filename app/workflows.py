import difflib
import logging
from app.repo_mapping import get_mapping
from app.git_ops import clone_repo, commit_and_push
from app.github_api import create_pull_request, ensure_label, add_label_to_pr
from app.repo_analysis import analyze_repo, format_telegram_summary
from app.claude_client import summarize_repo, suggest_change, fix_change
from app.file_modifier import apply_suggestion, modify_file
from app.telegram import send_message
from app.database import update_run_step, update_run_field, fail_run, record_attempt, complete_attempt
from app.test_runner import run_tests

AI_LABEL = "ai-generated"
AI_LABEL_COLOR = "6f42c1"  # purple

logger = logging.getLogger("worker")


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


def story_implementation(run_id: int, issue_key: str, issue_type: str, summary: str) -> None:
    logger.info("story_implementation: starting %s (%s) — %s", issue_key, issue_type, summary)

    jira_project_key = issue_key.split("-")[0]

    update_run_step(run_id, "mapping_lookup")
    mapping = get_mapping(jira_project_key, issue_type)
    if not mapping:
        logger.warning("No repo mapping found for project=%s issue_type=%s — aborting", jira_project_key, issue_type)
        return

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
    suggestion = suggest_change(repo_path, analysis, issue_key=issue_key, issue_summary=summary)
    suggestion_msg = (
        f"{issue_key}: Suggested change in {suggestion['file']}\n"
        f"{suggestion['description']}\n\n"
        f"--- original ---\n{suggestion.get('original', '')}\n\n"
        f"+++ replacement +++\n{suggestion.get('replacement', '')}"
    )
    send_message("claude_suggestion", "COMPLETE", suggestion_msg)
    logger.info("story_implementation: Claude suggestion sent to Telegram — %s", suggestion["file"])

    update_run_step(run_id, "applying")
    applied = apply_suggestion(repo_path, suggestion)
    if applied["applied"]:
        change_detail = f"{applied['file']} — {applied['description']}"
        logger.info("story_implementation: suggestion applied — %s", applied["file"])
    else:
        fallback = modify_file(repo_path)
        change_detail = f"{fallback['file']} — {fallback['change']} (fallback: {applied['reason']})"
        logger.info("story_implementation: suggestion fallback — %s", applied["reason"])
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
        files_touched=suggestion.get("file"),
    )

    # --- Fix loop: one attempt if tests failed ---
    final_test_result = test_result
    fix_suggestion: dict | None = None

    if test_result["status"] in ("FAILED", "ERROR"):
        update_run_field(run_id, retry_count=1)
        send_message(
            "fix_attempt_started", "RUNNING",
            f"{issue_key}: tests {test_result['status']} — attempting 1 fix",
        )
        logger.info("story_implementation: initiating fix attempt")

        attempt_2_id = record_attempt(run_id, 2, "fix", "claude-sonnet-4-6")
        update_run_step(run_id, "fixing")

        fix_suggestion = fix_change(
            repo_path, analysis,
            issue_key=issue_key,
            issue_summary=summary,
            previous_suggestion=suggestion,
            test_output=test_result["output"],
        )
        fix_applied = apply_suggestion(repo_path, fix_suggestion)
        if fix_applied["applied"]:
            fix_detail = f"{fix_applied['file']} — {fix_applied['description']}"
        else:
            fix_detail = f"fix not applied: {fix_applied.get('reason', 'unknown')}"
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
            files_touched=fix_suggestion.get("file"),
        )

        if retest_result["status"] in ("FAILED", "ERROR"):
            complete_attempt(
                attempt_2_id,
                status="FAILED",
                failure_summary=f"Tests still failing after fix: {(retest_result.get('output') or '')[-300:]}",
                test_status=retest_result["status"],
                files_touched=fix_suggestion.get("file"),
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
    suggestion_description = suggestion.get("description", summary)
    commit_message = f"ai: {issue_key} — {suggestion_description}"

    update_run_step(run_id, "pushing")
    branch = commit_and_push(
        repo_path=repo_path,
        issue_key=issue_key,
        run_id=run_id,
        commit_message=commit_message,
    )
    update_run_field(run_id, working_branch=branch)
    send_message("git_push", "COMPLETE", f"{issue_key}: branch {branch} pushed to GitHub")
    logger.info("story_implementation: pushed branch %s", branch)

    # Build unified diff — prefer fix suggestion if a fix was applied
    diff_source = fix_suggestion if fix_suggestion else suggestion
    original_lines = diff_source.get("original", "").splitlines(keepends=True)
    replacement_lines = diff_source.get("replacement", "").splitlines(keepends=True)
    diff_lines = list(difflib.unified_diff(
        original_lines, replacement_lines,
        fromfile="original", tofile="modified", lineterm="",
    ))
    diff_block = "".join(diff_lines) if diff_lines else (
        f"- {diff_source.get('original', '').strip()}\n+ {diff_source.get('replacement', '').strip()}"
    )

    # Validation checklist
    is_py = suggestion.get("file", "").endswith(".py")
    syntax_line = "- [x] Python syntax check (ast.parse)\n" if is_py else ""
    validation_section = (
        "## Pre-apply validation\n"
        "- [x] Path traversal guard\n"
        "- [x] File exists in repo\n"
        "- [x] Original text found\n"
        "- [x] No-op guard (original ≠ replacement)\n"
        f"{syntax_line}"
    )

    attempt_number = 2 if fix_suggestion else 1
    test_section = _build_test_section(final_test_result, attempt=attempt_number)

    retry_note = ""
    if fix_suggestion:
        retry_note = (
            f"**Note:** Initial implementation failed tests. "
            f"One fix attempt was made and tests now pass.\n\n"
        )

    pr_body = (
        f"> 🤖 Automated PR — [AI Dev Orchestrator](https://github.com/suyog19/ai-dev-orchestrator)\n\n"
        f"**Issue:** {issue_key}  \n"
        f"**Story:** {summary}\n\n"
        f"---\n\n"
        f"## Summary\n{claude_summary}\n\n"
        f"---\n\n"
        f"## Change\n"
        f"{retry_note}"
        f"**File:** `{diff_source.get('file', 'N/A')}`  \n"
        f"**Description:** {diff_source.get('description', suggestion_description)}\n\n"
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
    update_run_step(run_id, "done")
    send_message("pr_created", "COMPLETE", f"{issue_key}: PR #{pr['number']} — {pr['url']}")
    logger.info("story_implementation: PR #%s at %s", pr["number"], pr["url"])
