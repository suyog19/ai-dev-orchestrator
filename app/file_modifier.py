import ast
import os
import logging
from datetime import datetime, timezone

logger = logging.getLogger("file_modifier")


def _check_path_safe(repo_path: str, rel_path: str) -> bool:
    """Return True only if rel_path resolves to a location inside repo_path."""
    repo_abs = os.path.realpath(repo_path)
    target_abs = os.path.realpath(os.path.join(repo_path, rel_path))
    return target_abs.startswith(repo_abs + os.sep) or target_abs == repo_abs


def apply_suggestion(repo_path: str, suggestion: dict) -> dict:
    """Apply Claude's suggested change to the target file with pre-apply validation.

    Validation steps (all run before any write):
      1. Required fields present and non-empty
      2. Path stays within repo_path (traversal guard)
      3. File exists on disk
      4. original text is present in the file
      5. original != replacement (no-op guard)
      6. For .py files: ast.parse() the modified content to catch syntax errors

    Returns a dict with file, applied (bool), and description or reason.
    """
    rel_path = suggestion.get("file", "")
    original = suggestion.get("original", "")
    replacement = suggestion.get("replacement", "")
    description = suggestion.get("description", "")

    # 1. Required fields
    if not rel_path or not original:
        logger.warning("apply_suggestion: empty file or original — skipping")
        return {"file": rel_path, "applied": False, "reason": "empty suggestion"}

    # 2. Path traversal guard
    if not _check_path_safe(repo_path, rel_path):
        logger.warning("apply_suggestion: path traversal rejected — %s", rel_path)
        return {"file": rel_path, "applied": False, "reason": "path traversal rejected"}

    # 3. File exists
    abs_path = os.path.join(repo_path, rel_path)
    if not os.path.isfile(abs_path):
        logger.warning("apply_suggestion: file not found — %s", abs_path)
        return {"file": rel_path, "applied": False, "reason": "file not found"}

    with open(abs_path, encoding="utf-8") as f:
        content = f.read()

    # 4. Original text present
    if original not in content:
        logger.warning("apply_suggestion: original text not found in %s", rel_path)
        return {"file": rel_path, "applied": False, "reason": "original text not found"}

    # 5. No-op guard
    if original == replacement:
        logger.warning("apply_suggestion: original and replacement are identical — skipping")
        return {"file": rel_path, "applied": False, "reason": "no-op: original equals replacement"}

    new_content = content.replace(original, replacement, 1)

    # 6. Syntax check for Python files
    if rel_path.endswith(".py"):
        try:
            ast.parse(new_content)
        except SyntaxError as exc:
            logger.warning("apply_suggestion: syntax error after change in %s — %s", rel_path, exc)
            return {"file": rel_path, "applied": False, "reason": f"syntax error: {exc}"}

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
