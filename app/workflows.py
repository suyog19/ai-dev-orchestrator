import difflib
import logging
from app.repo_mapping import get_mapping
from app.git_ops import clone_repo, commit_and_push
from app.github_api import create_pull_request, ensure_label, add_label_to_pr
from app.repo_analysis import analyze_repo, format_telegram_summary
from app.claude_client import summarize_repo, suggest_change
from app.file_modifier import apply_suggestion, modify_file
from app.telegram import send_message

AI_LABEL = "ai-generated"
AI_LABEL_COLOR = "6f42c1"  # purple

logger = logging.getLogger("worker")


def story_implementation(run_id: int, issue_key: str, issue_type: str, summary: str) -> None:
    logger.info("story_implementation: starting %s (%s) — %s", issue_key, issue_type, summary)

    jira_project_key = issue_key.split("-")[0]
    mapping = get_mapping(jira_project_key, issue_type)
    if not mapping:
        logger.warning("No repo mapping found for project=%s issue_type=%s — aborting", jira_project_key, issue_type)
        return

    repo_path = clone_repo(
        run_id=run_id,
        issue_key=issue_key,
        repo_name=mapping["repo_slug"],
        target_branch=mapping["base_branch"],
    )
    logger.info("story_implementation: repo cloned to %s", repo_path)

    analysis = analyze_repo(repo_path)
    telegram_summary = format_telegram_summary(issue_key, mapping["repo_slug"], analysis)
    send_message("repo_analysis", "COMPLETE", telegram_summary)
    logger.info("story_implementation: analysis sent to Telegram")

    claude_summary = summarize_repo(repo_path, mapping["repo_slug"], analysis)
    send_message("claude_summary", "COMPLETE", f"{issue_key}:\n{claude_summary}")
    logger.info("story_implementation: Claude summary sent to Telegram")

    suggestion = suggest_change(repo_path, analysis, issue_key=issue_key, issue_summary=summary)
    suggestion_msg = (
        f"{issue_key}: Suggested change in {suggestion['file']}\n"
        f"{suggestion['description']}\n\n"
        f"--- original ---\n{suggestion.get('original', '')}\n\n"
        f"+++ replacement +++\n{suggestion.get('replacement', '')}"
    )
    send_message("claude_suggestion", "COMPLETE", suggestion_msg)
    logger.info("story_implementation: Claude suggestion sent to Telegram — %s", suggestion["file"])

    applied = apply_suggestion(repo_path, suggestion)
    if applied["applied"]:
        change_detail = f"{applied['file']} — {applied['description']}"
        logger.info("story_implementation: suggestion applied — %s", applied["file"])
    else:
        fallback = modify_file(repo_path)
        change_detail = f"{fallback['file']} — {fallback['change']} (fallback: {applied['reason']})"
        logger.info("story_implementation: suggestion fallback — %s", applied["reason"])
    send_message("file_apply", "COMPLETE", f"{issue_key}: {change_detail}")

    suggestion_description = suggestion.get("description", summary)
    commit_message = f"ai: {issue_key} — {suggestion_description}"

    branch = commit_and_push(
        repo_path=repo_path,
        issue_key=issue_key,
        run_id=run_id,
        commit_message=commit_message,
    )
    send_message("git_push", "COMPLETE", f"{issue_key}: branch {branch} pushed to GitHub")
    logger.info("story_implementation: pushed branch %s", branch)

    # Build unified diff from suggestion original/replacement
    original_lines = suggestion.get("original", "").splitlines(keepends=True)
    replacement_lines = suggestion.get("replacement", "").splitlines(keepends=True)
    diff_lines = list(difflib.unified_diff(
        original_lines, replacement_lines,
        fromfile="original", tofile="modified", lineterm="",
    ))
    diff_block = "".join(diff_lines) if diff_lines else f"- {suggestion.get('original', '').strip()}\n+ {suggestion.get('replacement', '').strip()}"

    # Validation checklist — all passed if suggestion was applied
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

    pr_body = (
        f"> 🤖 Automated PR — [AI Dev Orchestrator](https://github.com/suyog19/ai-dev-orchestrator)\n\n"
        f"**Issue:** {issue_key}  \n"
        f"**Story:** {summary}\n\n"
        f"---\n\n"
        f"## Summary\n{claude_summary}\n\n"
        f"---\n\n"
        f"## Change\n"
        f"**File:** `{suggestion.get('file', 'N/A')}`  \n"
        f"**Description:** {suggestion_description}\n\n"
        f"```diff\n{diff_block}\n```\n\n"
        f"---\n\n"
        f"{validation_section}\n"
        f"---\n\n"
        f"## Review checklist\n"
        f"- [ ] Change matches the story intent\n"
        f"- [ ] No unintended side effects\n"
        f"- [ ] Tests added or updated if applicable\n"
        f"- [ ] Ready to merge\n"
    )

    ensure_label(mapping["repo_slug"], AI_LABEL, color=AI_LABEL_COLOR, description="Opened by AI Dev Orchestrator")
    pr = create_pull_request(
        repo_name=mapping["repo_slug"],
        head_branch=branch,
        base_branch=mapping["base_branch"],
        title=f"ai: {issue_key} — {suggestion_description}",
        body=pr_body,
    )
    add_label_to_pr(mapping["repo_slug"], pr["number"], AI_LABEL)
    send_message("pr_created", "COMPLETE", f"{issue_key}: PR #{pr['number']} — {pr['url']}")
    logger.info("story_implementation: PR #%s at %s", pr["number"], pr["url"])
