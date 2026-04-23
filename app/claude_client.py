import os
import json
import logging
import anthropic

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

SYSTEM_PROMPT = (
    "You are a code intelligence assistant. Analyze a software repository and write "
    "a concise 3-5 sentence technical summary covering: what the project does, the "
    "technology stack (languages, frameworks, key dependencies), and the main entry "
    "points or overall architecture. Be specific. Avoid filler phrases."
)

SUGGEST_PROMPT = (
    "You are a code improvement assistant. You will be given a Jira story and a source file. "
    "Suggest ONE small, concrete code change that moves the implementation toward what the story describes. "
    "If the story is not directly actionable (e.g. it describes a test or process), suggest the most "
    "relevant improvement you can find in the file instead. "
    "The change must be: specific (reference exact existing text), safe (no breaking changes unless "
    "fixing a clear bug), and minimal (change as few lines as possible). "
    "Respond with ONLY valid JSON — no markdown fences, no explanation — in this exact format:\n"
    '{"file": "<relative path>", "description": "<one sentence>", '
    '"original": "<exact existing text to replace>", "replacement": "<new text>"}'
)


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


def summarize_repo(repo_path: str, repo_name: str, analysis: dict) -> str:
    """Call Claude Sonnet to produce a 3-5 sentence technical summary of the repo.

    Uses prompt caching on the stable system prompt to reduce cost on repeated calls.
    Returns the summary string.
    """
    client = anthropic.Anthropic()

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
    """Ask Claude Sonnet to suggest one targeted code improvement aligned with the Jira story.

    Picks the first non-README entry-point file, sends it to Claude along with the
    issue context, and returns a dict with keys: file, description, original, replacement.
    """
    client = anthropic.Anthropic()

    primary_language = analysis.get("primary_language", "Python")
    key_files = _collect_key_files(repo_path, primary_language)

    # Prefer a source file over README for a code suggestion
    target_file, target_content = None, None
    for rel_path, content in key_files:
        if not rel_path.upper().startswith("README"):
            target_file, target_content = rel_path, content
            break
    if target_file is None and key_files:
        target_file, target_content = key_files[0]

    if target_file is None:
        return {"file": "unknown", "description": "No files found", "original": "", "replacement": ""}

    story_context = ""
    if issue_key or issue_summary:
        story_context = f"Jira issue: {issue_key}\nStory: {issue_summary}\n\n"

    user_content = (
        f"{story_context}"
        f"File: {target_file}\n\n```\n{target_content}\n```\n\n"
        f"Suggest one small improvement to this file that is relevant to the story above."
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=[{
            "type": "text",
            "text": SUGGEST_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_content}],
    )

    raw = next((b.text for b in response.content if b.type == "text"), "{}")
    logger.info(
        "Claude suggestion done (sonnet) — input=%s output=%s",
        response.usage.input_tokens,
        response.usage.output_tokens,
    )

    # Strip accidental markdown fences
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Claude returned non-JSON suggestion: %s", raw[:200])
        return {"file": target_file, "description": raw[:200], "original": "", "replacement": ""}
