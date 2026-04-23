import ast
import os
import logging
from datetime import datetime, timezone

logger = logging.getLogger("file_modifier")

MAX_CHANGED_FILES = 3


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


def apply_changes(repo_path: str, changes: list[dict]) -> dict:
    """Apply a list of Claude's suggested changes atomically.

    Multiple changes to the same file are applied sequentially in memory before
    any file is written, so they compose correctly and don't overwrite each other.
    Validates all changes first; if any fail, no files are written.
    Enforces MAX_CHANGED_FILES unique-file limit.

    Returns:
        {"applied": True, "files": [str, ...], "count": int}
        {"applied": False, "reason": str, "failed_file": str}
    """
    if not changes:
        return {"applied": False, "reason": "empty changes list", "failed_file": ""}

    # Group changes by file, preserving first-seen order
    file_order: list[str] = []
    by_file: dict[str, list[dict]] = {}
    for change in changes:
        rel_path = change.get("file", "")
        if rel_path not in by_file:
            file_order.append(rel_path)
            by_file[rel_path] = []
        by_file[rel_path].append(change)

    if len(file_order) > MAX_CHANGED_FILES:
        logger.warning("apply_changes: truncated %d unique files to %d", len(file_order), MAX_CHANGED_FILES)
        file_order = file_order[:MAX_CHANGED_FILES]

    # Validate each file: apply all its changes sequentially in memory
    validated: list[tuple[str, str, str]] = []  # (abs_path, final_content, rel_path)

    for rel_path in file_order:
        if not rel_path:
            return {"applied": False, "reason": "empty file path", "failed_file": ""}

        if not _check_path_safe(repo_path, rel_path):
            logger.warning("apply_changes: path traversal rejected — %s", rel_path)
            return {"applied": False, "reason": "path traversal rejected", "failed_file": rel_path}

        abs_path = os.path.join(repo_path, rel_path)
        if not os.path.isfile(abs_path):
            logger.warning("apply_changes: file not found — %s", abs_path)
            return {"applied": False, "reason": "file not found", "failed_file": rel_path}

        with open(abs_path, encoding="utf-8") as f:
            current_content = f.read()

        descriptions: list[str] = []
        for change in by_file[rel_path]:
            original = change.get("original", "")
            replacement = change.get("replacement", "")
            description = change.get("description", "")

            if not original:
                return {"applied": False, "reason": "empty original", "failed_file": rel_path}

            if original not in current_content:
                logger.warning("apply_changes: original text not found in %s", rel_path)
                return {"applied": False, "reason": "original text not found", "failed_file": rel_path}

            if original == replacement:
                logger.warning("apply_changes: no-op change — %s", rel_path)
                return {"applied": False, "reason": "no-op: original equals replacement", "failed_file": rel_path}

            current_content = current_content.replace(original, replacement, 1)
            descriptions.append(description)

        if rel_path.endswith(".py"):
            try:
                ast.parse(current_content)
            except SyntaxError as exc:
                logger.warning("apply_changes: syntax error in %s — %s", rel_path, exc)
                return {"applied": False, "reason": f"syntax error: {exc}", "failed_file": rel_path}

        validated.append((abs_path, current_content, rel_path))
        logger.info("apply_changes: validated %s (%d change(s))", rel_path, len(descriptions))

    for abs_path, final_content, rel_path in validated:
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(final_content)
        logger.info("apply_changes: wrote %s", rel_path)

    files = [v[2] for v in validated]
    return {"applied": True, "files": files, "count": len(files)}


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
