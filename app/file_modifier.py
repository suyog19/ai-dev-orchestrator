import os
import logging
from datetime import datetime, timezone

logger = logging.getLogger("file_modifier")


def modify_file(repo_path: str, relative_path: str = "README.md") -> dict:
    """Append a timestamped marker to a file in the repo.

    Returns a dict with the file path and description of the change.
    Raises FileNotFoundError if the target file does not exist.
    """
    target = os.path.join(repo_path, relative_path)

    if not os.path.isfile(target):
        raise FileNotFoundError(f"{relative_path} not found in repo at {repo_path}")

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    marker = f"\n<!-- ai-orchestrator: modified at {timestamp} -->\n"

    with open(target, "a", encoding="utf-8") as f:
        f.write(marker)

    logger.info("Modified %s — appended marker at %s", relative_path, timestamp)

    return {
        "file": relative_path,
        "change": f"Appended timestamp marker at {timestamp}",
    }
