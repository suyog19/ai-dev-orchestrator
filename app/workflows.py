import logging
from app.repo_mapping import get_mapping
from app.git_ops import clone_repo

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
    logger.info("story_implementation: repo ready at %s", repo_path)
