import os
import re
import json
import logging
import anthropic

from app.repo_analysis import EXTENSION_TO_LANGUAGE, IGNORED_DIRS

logger = logging.getLogger("claude_client")

# Candidate entry-point files per language — checked in order
ENTRY_POINTS = {
    "Python":     ["app/main.py", "main.py", "src/main.py", "app/__init__.py"],
    "JavaScript": ["index.js", "src/index.js", "app.js", "server.js"],
    "TypeScript": ["index.ts", "src/index.ts", "app.ts", "server.ts"],
    "Go":         ["main.go", "cmd/main.go"],
    "Java":       ["src/main/java/Main.java", "Main.java"],
    "Rust":       ["src/main.rs"],
    "Ruby":       ["app.rb", "main.rb", "config.ru"],
}

# Common words that appear in every story summary but don't identify relevant files
_STOP_WORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "so", "that", "this", "it", "its",
    "as", "we", "i", "my", "our", "their", "all", "if", "when", "where",
    "not", "no", "into", "also", "each", "any", "only",
    # common story verbs that appear in every summary
    "add", "use", "using", "implement", "create", "update", "change",
    "make", "get", "set", "new", "ensure", "allow", "support",
}

SYSTEM_PROMPT = (
    "You are a code intelligence assistant. Analyze a software repository and write "
    "a concise 3-5 sentence technical summary covering: what the project does, the "
    "technology stack (languages, frameworks, key dependencies), and the main entry "
    "points or overall architecture. Be specific. Avoid filler phrases."
)

FIX_PROMPT = (
    "You are a code repair assistant. A previous implementation attempt failed tests. "
    "You will be given: the original story, the current state of all changed files, and the failing test output. "
    "Call the apply_code_changes tool with targeted fixes (one or more files) to make the failing tests pass. "
    "Rules:\n"
    "- Change as few lines as possible\n"
    "- Do not break currently passing tests\n"
    "- Only modify files that were part of the original implementation\n"
    "- Do not import or reference types that do not already exist in the codebase\n"
    "- If the error is an import of a non-existent type, remove that import and rewrite the code to use existing types"
)

SUGGEST_PROMPT = (
    "You are a code improvement assistant. You will be given a Jira story and one or more source files. "
    "Call the apply_code_changes tool with one to three concrete code changes that together fully implement "
    "what the story describes. Each change must be in a different file. Only include a change in a file "
    "if that file genuinely needs to change to satisfy the story. "
    "If the story is not directly actionable, suggest the most relevant improvement in any of the files. "
    "Each change must be: specific (reference exact existing text), safe (no breaking changes unless "
    "fixing a clear bug), and complete (all lines needed in that file in one contiguous block). "
    "You may add imports for names that genuinely exist in the codebase. "
    "Do not invent new class names or types that are not defined anywhere in the provided files."
)

# Tool schema used by both suggest_change and fix_change to guarantee structured output
_CHANGES_TOOL = {
    "name": "apply_code_changes",
    "description": "Apply one or more targeted code changes across one or more source files.",
    "input_schema": {
        "type": "object",
        "properties": {
            "changes": {
                "type": "array",
                "description": "List of file changes to apply (max 3 files, one change per file)",
                "minItems": 1,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "file":        {"type": "string", "description": "Relative path to the file to change"},
                        "description": {"type": "string", "description": "One sentence describing what this change does"},
                        "original":    {"type": "string", "description": "The exact existing text to replace (must match file content exactly)"},
                        "replacement": {"type": "string", "description": "The new text that replaces the original"},
                    },
                    "required": ["file", "description", "original", "replacement"],
                },
            },
            "summary": {
                "type": "string",
                "description": "One sentence summarizing all changes together",
            },
        },
        "required": ["changes", "summary"],
    },
}


def _read_truncated(path: str, max_lines: int = 150) -> str | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        content = "".join(lines[:max_lines])
        if len(lines) > max_lines:
            content += f"\n... ({len(lines) - max_lines} more lines truncated)\n"
        return content
    except Exception:
        return None


def _collect_key_files(repo_path: str, primary_language: str) -> list[tuple[str, str]]:
    """Return [(relative_path, content)] for README + up to 3 language-specific entry points."""
    files = []

    for readme in ["README.md", "README.rst", "README.txt", "README"]:
        content = _read_truncated(os.path.join(repo_path, readme))
        if content:
            files.append((readme, content))
            break

    for relative in ENTRY_POINTS.get(primary_language, []):
        if len(files) >= 4:
            break
        content = _read_truncated(os.path.join(repo_path, relative))
        if content:
            files.append((relative, content))

    return files


def _extract_keywords(text: str) -> list[str]:
    """Lowercase tokens from story text, filtered for meaningful domain words."""
    tokens = re.findall(r"[a-z]+", text.lower())
    return [t for t in tokens if len(t) > 2 and t not in _STOP_WORDS]


def _select_files_for_story(
    repo_path: str,
    primary_language: str,
    issue_summary: str,
    max_files: int = 4,
) -> list[tuple[str, str, str]]:
    """Score all repo source files by relevance to issue_summary.

    Returns list of (relative_path, content, reason) for the top max_files files,
    with README prepended if present (it doesn't consume a scored slot).
    """
    keywords = _extract_keywords(issue_summary)
    entry_point_set = set(ENTRY_POINTS.get(primary_language, []))
    wants_tests = "test" in keywords

    readme_entry: tuple[str, str, str] | None = None
    scored: list[tuple[int, str, str, str]] = []  # (score, rel_path, content, reason)

    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for fname in files:
            abs_path = os.path.join(root, fname)
            rel_path = os.path.relpath(abs_path, repo_path).replace("\\", "/")

            # Capture README separately
            if fname.upper().startswith("README"):
                content = _read_truncated(abs_path)
                if content and readme_entry is None:
                    readme_entry = (rel_path, content, "readme")
                continue

            ext = os.path.splitext(fname)[1].lower()
            if ext not in EXTENSION_TO_LANGUAGE:
                continue

            content = _read_truncated(abs_path)
            if not content:
                continue

            path_lower = rel_path.lower()
            reasons: list[str] = []
            score = 0

            # Keywords appearing in the file path (strong signal)
            path_matches = [kw for kw in keywords if kw in path_lower]
            if path_matches:
                score += len(path_matches) * 3
                reasons.append(f"path:{','.join(path_matches)}")

            # Keywords appearing in file content (capped to avoid weighting huge files)
            content_lower = content.lower()
            content_hits = sum(min(content_lower.count(kw), 2) for kw in keywords)
            content_hits = min(content_hits, 5)
            if content_hits > 0:
                score += content_hits
                reasons.append(f"content:{content_hits}hits")

            # Small bonus for known entry points so they don't disappear entirely
            if rel_path in entry_point_set:
                score += 2
                reasons.append("entry-point")

            # Test file bonus when story explicitly targets testing
            is_test = "test" in path_lower or fname.startswith("test_")
            if is_test and wants_tests:
                score += 2
                reasons.append("test-match")

            reason_str = "; ".join(reasons) if reasons else "baseline"
            scored.append((score, rel_path, content, reason_str))

    scored.sort(key=lambda x: x[0], reverse=True)

    result: list[tuple[str, str, str]] = []
    if readme_entry:
        result.append(readme_entry)

    for score, rel_path, content, reason in scored[:max_files]:
        result.append((rel_path, content, reason))

    if keywords:
        logger.info(
            "File selection for '%s': keywords=%s — selected %s",
            issue_summary[:60],
            keywords,
            [(r, rsn) for r, _, rsn in result],
        )

    return result


_CLIENT = anthropic.Anthropic(timeout=120.0)


def summarize_repo(repo_path: str, repo_name: str, analysis: dict) -> str:
    """Call Claude Sonnet to produce a 3-5 sentence technical summary of the repo.

    Uses prompt caching on the stable system prompt to reduce cost on repeated calls.
    Returns the summary string.
    """
    client = _CLIENT

    key_files = _collect_key_files(repo_path, analysis.get("primary_language", "Unknown"))

    top_level_str = ", ".join(analysis.get("top_level", [])[:15])
    ext_str = ", ".join(f"{ext}({n})" for ext, n in analysis.get("ext_counts", {}).items())

    file_sections = ""
    for rel_path, content in key_files:
        file_sections += f"\n--- {rel_path} ---\n{content}\n"

    user_content = (
        f"Repo: {repo_name}\n"
        f"Primary language: {analysis.get('primary_language', 'Unknown')}\n"
        f"Total files: {analysis.get('total_files', 0)}\n"
        f"Top extensions: {ext_str}\n"
        f"Top-level structure: {top_level_str}\n"
        f"\nKey file contents:{file_sections}"
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_content}],
    )

    summary = next((b.text for b in response.content if b.type == "text"), "")
    logger.info(
        "Claude summary done (sonnet) — cache_read=%s input=%s output=%s",
        response.usage.cache_read_input_tokens,
        response.usage.input_tokens,
        response.usage.output_tokens,
    )
    return summary


def suggest_change(repo_path: str, analysis: dict, issue_key: str = "", issue_summary: str = "") -> dict:
    """Ask Claude Sonnet to suggest up to 3 code changes aligned with the Jira story.

    Selects files by keyword overlap with the issue summary rather than a fixed entry-point
    list, giving Claude context most relevant to the story intent.
    Returns a dict with keys: changes (list of {file, description, original, replacement}), summary.
    """
    client = _CLIENT

    primary_language = analysis.get("primary_language", "Python")
    selected = _select_files_for_story(repo_path, primary_language, issue_summary)

    if not selected:
        return {
            "changes": [{"file": "unknown", "description": "No files found", "original": "", "replacement": ""}],
            "summary": "",
        }

    story_context = ""
    if issue_key or issue_summary:
        story_context = f"Jira issue: {issue_key}\nStory: {issue_summary}\n\n"

    file_sections = ""
    for rel_path, content, _reason in selected:
        file_sections += f"\n--- {rel_path} ---\n```\n{content}\n```\n"

    user_content = (
        f"{story_context}"
        f"Available source files (ordered by relevance to story):{file_sections}\n"
        f"Implement the story by changing one to three of the files above."
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=[{
            "type": "text",
            "text": SUGGEST_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        tools=[_CHANGES_TOOL],
        tool_choice={"type": "tool", "name": "apply_code_changes"},
        messages=[{"role": "user", "content": user_content}],
    )

    logger.info(
        "Claude suggestion done (sonnet) — input=%s output=%s",
        response.usage.input_tokens,
        response.usage.output_tokens,
    )

    tool_block = next((b for b in response.content if b.type == "tool_use"), None)
    if tool_block:
        return tool_block.input

    logger.warning("Claude suggestion: no tool_use block in response")
    fallback_file = selected[1][0] if len(selected) > 1 else selected[0][0]
    return {
        "changes": [{"file": fallback_file, "description": "No tool call returned", "original": "", "replacement": ""}],
        "summary": "",
    }


def fix_change(
    repo_path: str,
    analysis: dict,
    issue_key: str,
    issue_summary: str,
    previous_changes: list[dict],
    test_output: str,
) -> dict:
    """Ask Claude to fix a failing implementation.

    Sends the original story, current content of all changed files, and trimmed test failure
    output. Returns the same shape as suggest_change: {changes: [...], summary: "..."}.
    """
    client = _CLIENT

    # Trim test output to the most useful tail (last 60 lines)
    failure_lines = (test_output or "").strip().splitlines()
    trimmed_output = "\n".join(failure_lines[-60:]) if failure_lines else "(no output)"

    file_sections = ""
    changed_files = []
    for change in previous_changes:
        changed_file = change.get("file", "")
        if not changed_file:
            continue
        changed_files.append(changed_file)
        file_abs = os.path.join(repo_path, changed_file)
        current_content = _read_truncated(file_abs) or "(file not found)"
        file_sections += f"\n--- {changed_file} ---\n```\n{current_content}\n```\n"

    files_str = ", ".join(f"`{f}`" for f in changed_files)
    user_content = (
        f"Story: {issue_key} — {issue_summary}\n\n"
        f"The previous implementation changed {files_str} but tests are failing.\n\n"
        f"Current file contents:{file_sections}\n"
        f"Failing test output (last 60 lines):\n```\n{trimmed_output}\n```\n\n"
        f"Call apply_code_changes with minimal fixes to make the failing tests pass "
        f"without breaking any currently passing tests."
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=[{
            "type": "text",
            "text": FIX_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        tools=[_CHANGES_TOOL],
        tool_choice={"type": "tool", "name": "apply_code_changes"},
        messages=[{"role": "user", "content": user_content}],
    )

    logger.info(
        "Claude fix done (sonnet) — input=%s output=%s",
        response.usage.input_tokens,
        response.usage.output_tokens,
    )

    tool_block = next((b for b in response.content if b.type == "tool_use"), None)
    if tool_block:
        return tool_block.input

    logger.warning("Claude fix: no tool_use block in response")
    fallback_file = changed_files[0] if changed_files else ""
    return {
        "changes": [{"file": fallback_file, "description": "No tool call returned", "original": "", "replacement": ""}],
        "summary": "",
    }
