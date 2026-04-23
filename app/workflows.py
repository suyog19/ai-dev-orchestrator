import logging
from app.repo_mapping import get_mapping
from app.git_ops import clone_repo, commit_and_push
from app.github_api import create_pull_request
from app.repo_analysis import analyze_repo, format_telegram_summary
from app.claude_client import summarize_repo, suggest_change
from app.file_modifier import apply_suggestion, modify_file
from app.telegram import send_message

logger = logging.getLogger("worker")


def story_implementation(run_id: int, issue_key: str, summary: str) -> None:
    logger.info("story_implementation: starting %s — %s", issue_key, summary)

    mapping = get_mapping(issue_key)
    if not mapping:
        logger.warning("No repo mapping found for %s — skipping clone", issue_key)
        return

    repo_path = clone_repo(
        run_id=run_id,
        issue_key=issue_key,
        repo_name=mapping["repo_name"],
        target_branch=mapping["target_branch"],
    )
    logger.info("story_implementation: repo cloned to %s", repo_path)

    analysis = analyze_repo(repo_path)
    telegram_summary = format_telegram_summary(issue_key, mapping["repo_name"], analysis)
    send_message("repo_analysis", "COMPLETE", telegram_summary)
    logger.info("story_implementation: analysis sent to Telegram")

    claude_summary = summarize_repo(repo_path, mapping["repo_name"], analysis)
    send_message("claude_summary", "COMPLETE", f"{issue_key}:\n{claude_summary}")
    logger.info("story_implementation: Claude summary sent to Telegram")

    suggestion = suggest_change(repo_path, analysis)
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
        commit_message=commit_message,
    )
    send_message("git_push", "COMPLETE", f"{issue_key}: branch {branch} pushed to GitHub")
    logger.info("story_implementation: pushed branch %s", branch)

    pr_body = (
        f"Automated PR created by AI Dev Orchestrator.\n\n"
        f"**Issue:** {issue_key}\n"
        f"**Jira summary:** {summary}\n\n"
        f"## Repo analysis\n{claude_summary}\n\n"
        f"## Change applied\n"
        f"**File:** `{suggestion.get('file', 'N/A')}`\n"
        f"**Description:** {suggestion_description}\n\n"
        f"```diff\n"
        f"- {suggestion.get('original', '').strip()}\n"
        f"+ {suggestion.get('replacement', '').strip()}\n"
        f"```"
    )

    pr = create_pull_request(
        repo_name=mapping["repo_name"],
        head_branch=branch,
        base_branch=mapping["target_branch"],
        title=f"ai: {issue_key} — {suggestion_description}",
        body=pr_body,
    )
    send_message("pr_created", "COMPLETE", f"{issue_key}: PR #{pr['number']} — {pr['url']}")
    logger.info("story_implementation: PR #%s at %s", pr["number"], pr["url"])
