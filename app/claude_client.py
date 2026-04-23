import os
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
    """Call Claude Haiku to produce a 3-5 sentence technical summary of the repo.

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
        model="claude-haiku-4-5-20251001",
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
        "Claude summary done — cache_read=%s input=%s output=%s",
        response.usage.cache_read_input_tokens,
        response.usage.input_tokens,
        response.usage.output_tokens,
    )
    return summary
