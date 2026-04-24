import ast
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
    "what the story describes. Each entry in the changes array must target a DIFFERENT file — never repeat "
    "the same file path twice. If a file needs multiple edits (e.g. a new import plus a new function), "
    "combine them into one contiguous original/replacement block for that file. "
    "Only include a file if it genuinely needs to change to satisfy the story. "
    "If the story is not directly actionable, suggest the most relevant improvement in any of the files. "
    "Each change must be: specific (reference exact existing text), safe (no breaking changes unless "
    "fixing a clear bug), and complete (all lines needed in that file in one contiguous block). "
    "You may add imports for names that genuinely exist in the codebase. "
    "Do not invent new class names or types that are not defined anywhere in the provided files. "
    "Do NOT modify test files (any file under tests/ or named test_*.py). "
    "Tests define expected behaviour — if your implementation breaks a test, the fix loop will handle it."
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


def _extract_python_imports(abs_path: str, repo_path: str) -> list[str]:
    """Return relative paths (from repo root) of local modules directly imported by abs_path.

    Handles absolute imports (from app.models import ...) and relative imports
    (from .models import ...). Skips anything that doesn't resolve to a file in the repo.
    """
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as f:
            source = f.read()
        tree = ast.parse(source)
    except Exception:
        return []

    repo_abs = os.path.realpath(repo_path)
    rel_of_file = os.path.relpath(abs_path, repo_abs).replace("\\", "/")
    package_parts = rel_of_file.split("/")[:-1]  # e.g. ["app"] for "app/main.py"

    found: list[str] = []

    def _try(parts: list[str]) -> None:
        for candidate in ["/".join(parts) + ".py", "/".join(parts) + "/__init__.py"]:
            if os.path.isfile(os.path.join(repo_abs, candidate)):
                found.append(candidate)
                break

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _try(alias.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                # Absolute: from app.models import Item
                _try(node.module.split("."))
            elif node.level > 0:
                # Relative: from .models import Item (level=1), from ..x import y (level=2)
                base = package_parts[:max(0, len(package_parts) - (node.level - 1))]
                suffix = node.module.split(".") if node.module else []
                _try(base + suffix)

    return list(dict.fromkeys(found))  # deduplicate, preserve order


def _extract_keywords(text: str) -> list[str]:
    """Lowercase tokens from story text, filtered for meaningful domain words."""
    tokens = re.findall(r"[a-z]+", text.lower())
    return [t for t in tokens if len(t) > 2 and t not in _STOP_WORDS]


def _select_files_for_story(
    repo_path: str,
    primary_language: str,
    issue_summary: str,
    max_scored: int = 2,
    max_import_deps: int = 2,
) -> list[tuple[str, str, str]]:
    """Score all repo source files by relevance to issue_summary.

    Selection strategy (up to 6 files total):
    1. README — always prepended if present
    2. Top max_scored keyword-scored non-test files (anchors)
    3. Up to max_import_deps direct import dependencies of those anchors (Python only)
    4. Best-scored test file — always appended if one exists

    Returns list of (relative_path, content, reason).
    """
    keywords = _extract_keywords(issue_summary)
    entry_point_set = set(ENTRY_POINTS.get(primary_language, []))

    readme_entry: tuple[str, str, str] | None = None
    scored: list[tuple[int, str, str, str]] = []       # non-test source files
    test_scored: list[tuple[int, str, str, str]] = []  # test files

    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for fname in files:
            abs_path = os.path.join(root, fname)
            rel_path = os.path.relpath(abs_path, repo_path).replace("\\", "/")

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
            is_test = "test" in path_lower or fname.startswith("test_")
            reasons: list[str] = []
            score = 0

            path_matches = [kw for kw in keywords if kw in path_lower]
            if path_matches:
                score += len(path_matches) * 3
                reasons.append(f"path:{','.join(path_matches)}")

            content_lower = content.lower()
            content_hits = sum(min(content_lower.count(kw), 2) for kw in keywords)
            content_hits = min(content_hits, 5)
            if content_hits > 0:
                score += content_hits
                reasons.append(f"content:{content_hits}hits")

            if rel_path in entry_point_set:
                score += 2
                reasons.append("entry-point")

            reason_str = "; ".join(reasons) if reasons else "baseline"

            if is_test:
                test_scored.append((score, rel_path, content, reason_str))
            else:
                scored.append((score, rel_path, content, reason_str))

    scored.sort(key=lambda x: x[0], reverse=True)
    test_scored.sort(key=lambda x: x[0], reverse=True)

    selected_paths: set[str] = set()
    result: list[tuple[str, str, str]] = []

    # 1. README
    if readme_entry:
        result.append(readme_entry)
        selected_paths.add(readme_entry[0])

    # 2. Top max_scored non-test files (anchors for import traversal)
    anchor_paths: list[str] = []
    for _score, rel_path, content, reason in scored[:max_scored]:
        result.append((rel_path, content, reason))
        selected_paths.add(rel_path)
        anchor_paths.append(rel_path)

    # 3. Import dependencies of anchors (Python only)
    if primary_language == "Python":
        dep_count = 0
        for anchor_rel in anchor_paths:
            if dep_count >= max_import_deps:
                break
            for imp_rel in _extract_python_imports(os.path.join(repo_path, anchor_rel), repo_path):
                if dep_count >= max_import_deps:
                    break
                if imp_rel in selected_paths:
                    continue
                content = _read_truncated(os.path.join(repo_path, imp_rel))
                if not content:
                    continue
                result.append((imp_rel, content, f"import-dep:{anchor_rel}"))
                selected_paths.add(imp_rel)
                dep_count += 1

    # 4. Best test file — always include regardless of story keywords
    if test_scored and test_scored[0][1] not in selected_paths:
        _s, rel_path, content, reason = test_scored[0]
        result.append((rel_path, content, reason + "; test-file"))

    if keywords:
        logger.info(
            "File selection for '%s': keywords=%s — selected %s",
            issue_summary[:60],
            keywords,
            [(r, rsn) for r, _, rsn in result],
        )

    return result


MAX_STORIES_PER_EPIC = 8

PLANNING_PROMPT = (
    "You are a software planning assistant specialising in breaking Epics into implementation-ready Stories. "
    "Each Story you propose must be:\n"
    "- Independently deliverable — a developer can implement it without waiting on other Stories\n"
    "- Testable — it has clear, verifiable acceptance criteria\n"
    "- Implementation-sized — a developer can complete it in 1-3 days\n\n"
    "Rules:\n"
    "- Produce 2-8 Stories — prefer fewer, well-scoped Stories over a long noisy list\n"
    "- Never use vague titles like 'Improve system' or 'General refactor'\n"
    "- Title must be imperative verb + object, max 10 words (e.g. 'Add OAuth2 login endpoint')\n"
    "- Do not invent architecture not implied by the Epic description\n"
    "- Surface assumptions and open questions explicitly\n"
    "- Dependency notes must reference other proposed Story titles specifically\n"
    "- Call the plan_breakdown tool only — do not respond with prose."
)

_BREAKDOWN_TOOL = {
    "name": "plan_breakdown",
    "description": "Propose a structured breakdown of an Epic into implementation-ready Stories.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "1-2 sentences summarising the decomposition approach.",
            },
            "assumptions": {
                "type": "array",
                "description": "Explicit assumptions made (e.g. 'Auth layer already exists'). Empty list if none.",
                "items": {"type": "string"},
            },
            "open_questions": {
                "type": "array",
                "description": "Unresolved questions that could affect scope. Empty list if none.",
                "items": {"type": "string"},
            },
            "items": {
                "type": "array",
                "description": f"Proposed Stories (max {MAX_STORIES_PER_EPIC}).",
                "minItems": 1,
                "maxItems": MAX_STORIES_PER_EPIC,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Imperative-verb Story title, max 10 words.",
                        },
                        "description": {
                            "type": "string",
                            "description": "2-3 sentences describing what to implement.",
                        },
                        "acceptance_criteria": {
                            "type": "array",
                            "description": "3-5 testable acceptance criteria.",
                            "items": {"type": "string"},
                            "minItems": 1,
                        },
                        "rationale": {
                            "type": "string",
                            "description": "Why this Story is a separate deliverable unit.",
                        },
                        "dependency_notes": {
                            "type": "string",
                            "description": "Other Story titles this depends on, or empty string.",
                        },
                        "risk_notes": {
                            "type": "string",
                            "description": "Known risks specific to this Story, or empty string.",
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                            "description": "Confidence in this Story's scope being correctly defined.",
                        },
                    },
                    "required": [
                        "title", "description", "acceptance_criteria",
                        "rationale", "confidence",
                    ],
                },
            },
        },
        "required": ["summary", "items", "assumptions", "open_questions"],
    },
}

REVIEWER_PROMPT = (
    "You are an independent code Reviewer Agent. Your job is to assess whether a PR is safe to merge.\n\n"
    "You will receive:\n"
    "- The Jira Story (key, summary, description, acceptance criteria)\n"
    "- Repository and branch information\n"
    "- The PR (number, title, body)\n"
    "- Implementation details (changed files, unified diff, test results, retry count)\n"
    "- Memory context (prior lessons from this repository)\n\n"
    "Review across four dimensions:\n"
    "1. Story alignment — Does the change address the Jira Story? Are acceptance criteria covered? Is scope appropriate?\n"
    "2. Code quality — Is the implementation clean? Any obvious bugs or unsafe assumptions?\n"
    "3. Test awareness — Were tests run? Did they pass? Are they relevant to the change?\n"
    "4. Diff risk — Number and type of files changed, high-risk areas, potential side effects.\n\n"
    "Verdict rules (strictly enforced):\n"
    "- BLOCKED: tests failed, or the diff clearly contradicts the story intent\n"
    "- NEEDS_CHANGES: implementation is plausible but incomplete, risky, or questionable\n"
    "- APPROVED_BY_AI: story alignment, code risk, and test state are all acceptable\n\n"
    "Call the submit_review tool only — do not respond with prose."
)

_REVIEW_TOOL = {
    "name": "submit_review",
    "description": "Submit a structured code review verdict for a PR.",
    "input_schema": {
        "type": "object",
        "properties": {
            "review_status": {
                "type": "string",
                "enum": ["APPROVED_BY_AI", "NEEDS_CHANGES", "BLOCKED"],
                "description": (
                    "Overall verdict. BLOCKED if tests failed or diff contradicts story. "
                    "NEEDS_CHANGES if plausible but incomplete or risky. "
                    "APPROVED_BY_AI only when story alignment, code risk, and test state are all acceptable."
                ),
            },
            "risk_level": {
                "type": "string",
                "enum": ["LOW", "MEDIUM", "HIGH"],
                "description": "Overall risk level of the change.",
            },
            "summary": {
                "type": "string",
                "description": "1-3 sentences summarising the review verdict.",
            },
            "findings": {
                "type": "array",
                "description": "Individual findings from the review, one per concern.",
                "items": {
                    "type": "object",
                    "properties": {
                        "severity": {
                            "type": "string",
                            "enum": ["INFO", "WARNING", "CRITICAL"],
                            "description": "INFO for observations, WARNING for concerns, CRITICAL for blockers.",
                        },
                        "category": {
                            "type": "string",
                            "enum": ["story_alignment", "code_quality", "test_awareness", "diff_risk"],
                        },
                        "message": {"type": "string"},
                    },
                    "required": ["severity", "category", "message"],
                },
            },
            "blocking_reasons": {
                "type": "array",
                "description": "Explicit reasons this PR cannot merge. Empty list if APPROVED_BY_AI.",
                "items": {"type": "string"},
            },
            "recommendations": {
                "type": "array",
                "description": "Non-blocking suggestions for improvement.",
                "items": {"type": "string"},
            },
        },
        "required": ["review_status", "risk_level", "summary", "findings", "blocking_reasons", "recommendations"],
    },
}

TEST_QUALITY_PROMPT = (
    "You are an independent Test Quality Agent. Your job is to assess whether the tests "
    "in this PR are sufficient to trust that the implementation is correct.\n\n"
    "You will receive:\n"
    "- The Jira Story (key, summary, description, acceptance criteria)\n"
    "- The PR (number, title, body)\n"
    "- The unified diff (source and test file changes)\n"
    "- Test results (status, command, output excerpt)\n"
    "- Implementation context (changed files, retry count)\n"
    "- Memory context (prior lessons from this repository)\n\n"
    "Evaluate five dimensions:\n"
    "1. Acceptance criteria coverage — Do tests map to the Story's acceptance criteria?\n"
    "2. Changed behaviour coverage — If code behaviour changed, are relevant tests added or updated?\n"
    "3. Edge cases — Are negative paths, empty inputs, invalid values, not-found cases covered?\n"
    "4. Test integrity — Were tests weakened, skipped, or assertions removed to make the PR pass?\n"
    "5. Confidence — Passing tests are necessary but not sufficient. If tests are shallow, block.\n\n"
    "Verdict rules (strictly enforced):\n"
    "- TESTS_BLOCKING: tests failed, tests NOT_RUN for a test-capable repo, tests removed/skipped to pass\n"
    "- TESTS_WEAK: tests pass but do not adequately cover the Story acceptance criteria\n"
    "- TEST_QUALITY_APPROVED: tests meaningfully cover the Story and changed behaviour\n\n"
    "Call the submit_test_quality_review tool only — do not respond with prose."
)

_TEST_QUALITY_TOOL = {
    "name": "submit_test_quality_review",
    "description": "Submit a structured test quality verdict for a PR.",
    "input_schema": {
        "type": "object",
        "properties": {
            "quality_status": {
                "type": "string",
                "enum": ["TEST_QUALITY_APPROVED", "TESTS_WEAK", "TESTS_BLOCKING"],
                "description": (
                    "Overall verdict. TESTS_BLOCKING if tests failed, not run, or removed to pass. "
                    "TESTS_WEAK if tests pass but coverage is shallow. "
                    "TEST_QUALITY_APPROVED only when tests meaningfully cover the Story."
                ),
            },
            "confidence_level": {
                "type": "string",
                "enum": ["LOW", "MEDIUM", "HIGH"],
                "description": "Confidence in the test adequacy assessment.",
            },
            "summary": {
                "type": "string",
                "description": "1-3 sentences summarising the test quality verdict.",
            },
            "coverage_findings": {
                "type": "array",
                "description": "Coverage assessment per acceptance criterion or behaviour.",
                "items": {
                    "type": "object",
                    "properties": {
                        "criteria": {"type": "string", "description": "The acceptance criterion or behaviour being assessed."},
                        "status": {"type": "string", "enum": ["covered", "partial", "missing"], "description": "Coverage status."},
                        "evidence": {"type": "string", "description": "Test file or test name providing coverage, or reason it is missing."},
                    },
                    "required": ["criteria", "status", "evidence"],
                },
            },
            "missing_tests": {
                "type": "array",
                "description": "Descriptions of test scenarios that are absent but needed.",
                "items": {"type": "string"},
            },
            "suspicious_tests": {
                "type": "array",
                "description": "Tests that appear weakened, skipped, or modified to avoid failure.",
                "items": {"type": "string"},
            },
            "recommendations": {
                "type": "array",
                "description": "Non-blocking suggestions for future test improvements.",
                "items": {"type": "string"},
            },
        },
        "required": [
            "quality_status", "confidence_level", "summary",
            "coverage_findings", "missing_tests", "suspicious_tests", "recommendations",
        ],
    },
}

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


def suggest_change(repo_path: str, analysis: dict, issue_key: str = "", issue_summary: str = "", memory_context: str = "") -> dict:
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

    memory_block = f"Prior lessons from this repository:\n{memory_context}\n\n" if memory_context else ""
    user_content = (
        f"{story_context}"
        f"Available source files (ordered by relevance to story):{file_sections}\n"
        f"{memory_block}"
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
    memory_context: str = "",
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
    memory_block = f"Prior lessons from this repository:\n{memory_context}\n\n" if memory_context else ""
    user_content = (
        f"Story: {issue_key} — {issue_summary}\n\n"
        f"The previous implementation changed {files_str} but tests are failing.\n\n"
        f"Current file contents:{file_sections}\n"
        f"Failing test output (last 60 lines):\n```\n{trimmed_output}\n```\n\n"
        f"{memory_block}"
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


def plan_epic_breakdown(issue_key: str, summary: str, memory_context: str = "") -> dict:
    """Ask Claude Sonnet to decompose an Epic into Stories.

    Returns the plan_breakdown tool input dict:
    {
        "summary": "...",
        "assumptions": [...],
        "open_questions": [...],
        "items": [
            {"title": "...", "description": "...", "acceptance_criteria": [...],
             "rationale": "...", "dependency_notes": "...",
             "risk_notes": "...", "confidence": "high|medium|low"},
            ...
        ]
    }
    Raises RuntimeError if Claude returns no tool call or empty items.
    """
    user_content = f"Epic: {issue_key}\nTitle: {summary}\n"
    if memory_context:
        user_content += f"\nPrior lessons from this repository:\n{memory_context}\n"
    user_content += f"\nPropose up to {MAX_STORIES_PER_EPIC} Stories for this Epic using the plan_breakdown tool."

    response = _CLIENT.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=[{
            "type": "text",
            "text": PLANNING_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        tools=[_BREAKDOWN_TOOL],
        tool_choice={"type": "tool", "name": "plan_breakdown"},
        messages=[{"role": "user", "content": user_content}],
    )

    logger.info(
        "Claude epic breakdown done — input=%s output=%s",
        response.usage.input_tokens,
        response.usage.output_tokens,
    )

    tool_block = next((b for b in response.content if b.type == "tool_use"), None)
    if not tool_block:
        raise RuntimeError("Claude returned no tool_use block for epic breakdown")

    result = tool_block.input
    items = result.get("items", [])
    if not items:
        raise RuntimeError("Claude returned empty items list for epic breakdown")

    if len(items) > MAX_STORIES_PER_EPIC:
        logger.warning(
            "Claude returned %d items — truncating to cap of %d",
            len(items), MAX_STORIES_PER_EPIC,
        )
        result["items"] = items[:MAX_STORIES_PER_EPIC]

    return result


def review_pr(
    story_context: dict,
    pr_context: dict,
    diff: str,
    test_result: dict,
    memory_context: str = "",
) -> dict:
    """Run the Reviewer Agent against a PR and return a structured verdict.

    story_context keys: key, summary, description, acceptance_criteria (list[str])
    pr_context keys: number, url, title, repo_slug, base_branch, working_branch,
                     files_changed (list[str]), commit_message, retry_count, files_changed_count
    diff: unified diff string (truncated to 8 000 chars internally)
    test_result keys: status, command, output_excerpt
    memory_context: formatted bullet string from get_execution_memory()

    Returns the submit_review tool input dict:
    {
        "review_status": "APPROVED_BY_AI" | "NEEDS_CHANGES" | "BLOCKED",
        "risk_level":    "LOW" | "MEDIUM" | "HIGH",
        "summary":       "...",
        "findings":      [{"severity": ..., "category": ..., "message": ...}, ...],
        "blocking_reasons": [...],
        "recommendations":  [...],
    }
    Raises RuntimeError if Claude returns no tool_use block.
    """
    ac_lines = "\n".join(
        f"  - {ac}" for ac in (story_context.get("acceptance_criteria") or [])
    )
    story_block = (
        f"Story: {story_context.get('key', '')} — {story_context.get('summary', '')}\n"
        f"Description: {story_context.get('description') or '(none)'}\n"
        f"Acceptance criteria:\n{ac_lines or '  (none provided)'}"
    )

    files_list = "\n".join(f"  - {f}" for f in (pr_context.get("files_changed") or []))
    output_excerpt = (test_result.get("output_excerpt") or "").strip()
    output_lines = "\n".join(f"    {ln}" for ln in output_excerpt.splitlines()[-30:])
    test_block = (
        f"  Status: {test_result.get('status', 'NOT_RUN')}\n"
        f"  Command: {test_result.get('command', '')}\n"
        f"  Output (last 30 lines):\n{output_lines or '    (none)'}"
    )

    diff_trimmed = (diff or "").strip()
    if len(diff_trimmed) > 8000:
        diff_trimmed = diff_trimmed[:8000] + "\n... (diff truncated)"

    memory_block = (
        f"Prior lessons from this repository:\n{memory_context}\n\n"
        if memory_context else ""
    )

    user_content = (
        f"{story_block}\n\n"
        f"Repository: {pr_context.get('repo_slug', '')}\n"
        f"Base branch: {pr_context.get('base_branch', '')}\n"
        f"Working branch: {pr_context.get('working_branch', '')}\n\n"
        f"PR #{pr_context.get('number', '')} — {pr_context.get('title', '')}\n"
        f"PR URL: {pr_context.get('url', '')}\n\n"
        f"Changed files ({pr_context.get('files_changed_count', 0)}):\n"
        f"{files_list or '  (none)'}\n\n"
        f"Retry count: {pr_context.get('retry_count', 0)}\n"
        f"Commit message: {pr_context.get('commit_message', '')}\n\n"
        f"Test results:\n{test_block}\n\n"
        f"Unified diff:\n```\n{diff_trimmed}\n```\n\n"
        f"{memory_block}"
        f"Review this PR and call submit_review with your structured verdict."
    )

    response = _CLIENT.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=[{
            "type": "text",
            "text": REVIEWER_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        tools=[_REVIEW_TOOL],
        tool_choice={"type": "tool", "name": "submit_review"},
        messages=[{"role": "user", "content": user_content}],
    )

    logger.info(
        "Claude review done (sonnet) — input=%s output=%s",
        response.usage.input_tokens,
        response.usage.output_tokens,
    )

    tool_block = next((b for b in response.content if b.type == "tool_use"), None)
    if not tool_block:
        raise RuntimeError("Reviewer Agent returned no tool_use block")

    return tool_block.input


def review_test_quality(
    story_context: dict,
    pr_context: dict,
    diff_context: dict,
    test_context: dict,
    implementation_context: dict,
    memory_context: str = "",
) -> dict:
    """Run the Test Quality Agent against a PR and return a structured verdict.

    story_context keys: key, summary, description, acceptance_criteria (list[str])
    pr_context keys: number, url, title, body
    diff_context keys: full_diff (str), changed_files (list[str])
    test_context keys: status, command, output_excerpt, test_files_changed (list[str]),
                       skipped_tests_detected (bool)
    implementation_context keys: files_changed_count, retry_count,
                                 changed_source_files (list[str]), changed_test_files (list[str])
    memory_context: formatted bullet string from get_execution_memory()

    Returns the submit_test_quality_review tool input dict:
    {
        "quality_status":    "TEST_QUALITY_APPROVED" | "TESTS_WEAK" | "TESTS_BLOCKING",
        "confidence_level":  "LOW" | "MEDIUM" | "HIGH",
        "summary":           "...",
        "coverage_findings": [{"criteria": ..., "status": ..., "evidence": ...}, ...],
        "missing_tests":     [...],
        "suspicious_tests":  [...],
        "recommendations":   [...],
    }
    Raises RuntimeError if Claude returns no tool_use block.
    """
    ac_lines = "\n".join(
        f"  - {ac}" for ac in (story_context.get("acceptance_criteria") or [])
    )
    story_block = (
        f"Story: {story_context.get('key', '')} — {story_context.get('summary', '')}\n"
        f"Description: {story_context.get('description') or '(none)'}\n"
        f"Acceptance criteria:\n{ac_lines or '  (none provided)'}"
    )

    pr_body = (pr_context.get("body") or "").strip()[:1000]

    changed_files = "\n".join(f"  - {f}" for f in (diff_context.get("changed_files") or []))
    src_files = "\n".join(
        f"  - {f}" for f in (implementation_context.get("changed_source_files") or [])
    )
    test_files = "\n".join(
        f"  - {f}" for f in (implementation_context.get("changed_test_files") or [])
    )

    output_excerpt = (test_context.get("output_excerpt") or "").strip()
    output_lines = "\n".join(f"    {ln}" for ln in output_excerpt.splitlines()[-30:])
    skipped_flag = "YES" if test_context.get("skipped_tests_detected") else "NO"

    diff_trimmed = (diff_context.get("full_diff") or "").strip()
    if len(diff_trimmed) > 8000:
        diff_trimmed = diff_trimmed[:8000] + "\n... (diff truncated)"

    memory_block = (
        f"Prior lessons from this repository:\n{memory_context}\n\n"
        if memory_context else ""
    )

    user_content = (
        f"{story_block}\n\n"
        f"PR #{pr_context.get('number', '')} — {pr_context.get('title', '')}\n"
        f"PR URL: {pr_context.get('url', '')}\n"
        f"PR body:\n{pr_body or '  (none)'}\n\n"
        f"Changed files ({implementation_context.get('files_changed_count', 0)} total):\n"
        f"{changed_files or '  (none)'}\n\n"
        f"Source files changed:\n{src_files or '  (none)'}\n"
        f"Test files changed:\n{test_files or '  (none)'}\n\n"
        f"Retry count: {implementation_context.get('retry_count', 0)}\n\n"
        f"Test execution:\n"
        f"  Status: {test_context.get('status', 'NOT_RUN')}\n"
        f"  Command: {test_context.get('command', '')}\n"
        f"  Skipped tests detected: {skipped_flag}\n"
        f"  Output (last 30 lines):\n{output_lines or '    (none)'}\n\n"
        f"Unified diff:\n```\n{diff_trimmed}\n```\n\n"
        f"{memory_block}"
        f"Assess the test quality and call submit_test_quality_review with your structured verdict."
    )

    response = _CLIENT.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=[{
            "type": "text",
            "text": TEST_QUALITY_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        tools=[_TEST_QUALITY_TOOL],
        tool_choice={"type": "tool", "name": "submit_test_quality_review"},
        messages=[{"role": "user", "content": user_content}],
    )

    logger.info(
        "Claude test_quality done (sonnet) — input=%s output=%s",
        response.usage.input_tokens,
        response.usage.output_tokens,
    )

    tool_block = next((b for b in response.content if b.type == "tool_use"), None)
    if not tool_block:
        raise RuntimeError("Test Quality Agent returned no tool_use block")

    return tool_block.input
