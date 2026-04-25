import os
import logging
from collections import Counter

logger = logging.getLogger("repo_analysis")

EXTENSION_TO_LANGUAGE = {
    ".py": "Python",
    ".js": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".jsx": "JavaScript",
    ".go": "Go",
    ".java": "Java",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".php": "PHP",
    ".cs": "C#",
    ".cpp": "C++",
    ".c": "C",
    ".swift": "Swift",
    ".kt": "Kotlin",
    # Web content files — included so keyword scorer can match text inside HTML/CSS/templates
    ".html": "HTML",
    ".htm": "HTML",
    ".css": "CSS",
    ".vue": "JavaScript",
    ".svelte": "JavaScript",
}

IGNORED_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache"}


def analyze_repo(repo_path: str) -> dict:
    """Walk repo tree, detect primary language and file structure."""
    ext_counts = Counter()
    total_files = 0

    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for f in files:
            total_files += 1
            ext = os.path.splitext(f)[1].lower()
            if ext:
                ext_counts[ext] += 1

    primary_language = "Unknown"
    for ext, _ in ext_counts.most_common():
        lang = EXTENSION_TO_LANGUAGE.get(ext)
        if lang:
            primary_language = lang
            break

    top_level = sorted(os.listdir(repo_path))

    logger.info(
        "Analysis: %s, %d files, top extensions: %s",
        primary_language,
        total_files,
        dict(ext_counts.most_common(3)),
    )

    return {
        "primary_language": primary_language,
        "total_files": total_files,
        "top_level": top_level,
        "ext_counts": dict(ext_counts.most_common(5)),
    }


def format_telegram_summary(issue_key: str, repo_name: str, analysis: dict) -> str:
    top = analysis["top_level"]
    top_str = "  " + "\n  ".join(top[:12])
    if len(top) > 12:
        top_str += f"\n  (+{len(top) - 12} more)"

    ext_str = ", ".join(f"{ext}({n})" for ext, n in analysis["ext_counts"].items())

    return (
        f"Issue: {issue_key}\n"
        f"Repo: {repo_name}\n"
        f"Language: {analysis['primary_language']}\n"
        f"Files: {analysis['total_files']}\n"
        f"Top extensions: {ext_str}\n"
        f"Structure:\n{top_str}"
    )
