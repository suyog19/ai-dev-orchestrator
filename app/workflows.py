import time
import logging

logger = logging.getLogger("worker")


def story_implementation(issue_key: str, summary: str) -> None:
    logger.info("story_implementation: starting work on %s — %s", issue_key, summary)
    time.sleep(5)  # stub: simulate implementation work
    logger.info("story_implementation: finished %s", issue_key)
