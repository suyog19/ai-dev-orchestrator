import os
import logging
from datetime import datetime, timezone

logger = logging.getLogger("file_modifier")


def apply_suggestion(repo_path: str, suggestion: dict) -> dict:
    """Apply Claude's suggested change to the target file.

    Performs a single string replacement of suggestion['original'] with
    suggestion['replacement'] in suggestion['file'].
    Returns a dict with file, applied (bool), and description or reason.
    Falls back gracefully if the file is missing or the original text is not found.
    """
    rel_path = suggestion.get("file", "")
    original = suggestion.get("original", "")
    replacement = suggestion.get("replacement", "")
    description = suggestion.get("description", "")

    if not rel_path or not original:
        logger.warning("apply_suggestion: empty file or original — skipping")
        return {"file": rel_path, "applied": False, "reason": "empty suggestion"}

    abs_path = os.path.join(repo_path, rel_path)
    if not os.path.isfile(abs_path):
        logger.warning("apply_suggestion: file not found — %s", abs_path)
        return {"file": rel_path, "applied": False, "reason": "file not found"}

    with open(abs_path, encoding="utf-8") as f:
        content = f.read()

    if original not in content:
        logger.warning("apply_suggestion: original text not found in %s", rel_path)
        return {"file": rel_path, "applied": False, "reason": "original text not found"}

    new_content = content.replace(original, replacement, 1)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    logger.info("apply_suggestion: applied change to %s — %s", rel_path, description)
    return {"file": rel_path, "applied": True, "description": description}


def modify_file(repo_path: str, relative_path: str = "README.md") -> dict:
    """Fallback: append a timestamped marker to a file in the repo."""
    target = os.path.join(repo_path, relative_path)

    if not os.path.isfile(target):
        raise FileNotFoundError(f"{relative_path} not found in repo at {repo_path}")

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    marker = f"\n<!-- ai-orchestrator: modified at {timestamp} -->\n"

    with open(target, "a", encoding="utf-8") as f:
        f.write(marker)

    logger.info("Modified %s — appended marker at %s", relative_path, timestamp)
    return {"file": relative_path, "change": f"Appended timestamp marker at {timestamp}"}
