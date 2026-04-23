import logging
from app.repo_mapping import get_mapping
from app.git_ops import clone_repo, commit_and_push
from app.github_api import create_pull_request
from app.repo_analysis import analyze_repo, format_telegram_summary
from app.file_modifier import modify_file
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

    modification = modify_file(repo_path)
    send_message("file_modify", "COMPLETE", f"{issue_key}: {modification['file']} — {modification['change']}")
    logger.info("story_implementation: file modified — %s", modification)

    branch = commit_and_push(
        repo_path=repo_path,
        issue_key=issue_key,
        commit_message=f"ai: {issue_key} — {summary}",
    )
    send_message("git_push", "COMPLETE", f"{issue_key}: branch {branch} pushed to GitHub")
    logger.info("story_implementation: pushed branch %s", branch)

    pr = create_pull_request(
        repo_name=mapping["repo_name"],
        head_branch=branch,
        base_branch=mapping["target_branch"],
        title=f"ai: {issue_key} — {summary}",
        body=f"Automated PR created by AI Dev Orchestrator.\n\n**Issue:** {issue_key}\n**Summary:** {summary}",
    )
    send_message("pr_created", "COMPLETE", f"{issue_key}: PR #{pr['number']} — {pr['url']}")
    logger.info("story_implementation: PR #%s at %s", pr["number"], pr["url"])
