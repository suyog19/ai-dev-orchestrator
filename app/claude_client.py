import ast
import os
import re
import json
import logging
import anthropic

from app.feedback import CapabilityProfile
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
    "Clarification: If there is genuine ambiguity that makes it impossible to give a safe verdict "
    "and a human answer would resolve it, you may set needs_clarification=true with a specific question and options. "
    "Only use this when the ambiguity is blocking — do not use it to avoid making a judgement call.\n\n"
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

_REVIEW_CLARIFICATION_FIELDS = {
    "needs_clarification": {
        "type": "boolean",
        "description": (
            "Set to true only if there is genuine blocking ambiguity that a human must resolve. "
            "Omit or set false otherwise."
        ),
    },
    "clarification_question": {
        "type": "string",
        "description": "Specific question for the human if needs_clarification=true.",
    },
    "clarification_context_summary": {
        "type": "string",
        "description": "Brief context explaining why clarification is needed.",
    },
    "clarification_options": {
        "type": "array",
        "description": "2-4 short options for the human to choose from.",
        "items": {"type": "string"},
    },
}

# Inject clarification fields into _REVIEW_TOOL schema
_REVIEW_TOOL["input_schema"]["properties"].update(_REVIEW_CLARIFICATION_FIELDS)

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


_AMBIGUITY_CHECK_PROMPT = (
    "You are reviewing a Jira Epic before it is broken down into implementation Stories.\n\n"
    "Your job: identify MISSING SPECIFICS that would force an AI engineer to make unverifiable "
    "assumptions during implementation — i.e. details that only the product owner can supply.\n\n"
    "Flag when the epic says 'change X' without saying what the new value is, "
    "'add Y feature' without describing the behaviour/inputs/outputs, "
    "'fix Z' without identifying the specific problem.\n\n"
    "Do NOT flag:\n"
    "- Normal architecture or technology decisions engineers make themselves\n"
    "- Epics with enough context to write concrete stories\n"
    "- Minor details that can be inferred from common sense\n\n"
    "Be conservative — only return needs_clarification=true when something is truly BLOCKING."
)

_AMBIGUITY_CHECK_TOOL = {
    "name": "submit_ambiguity_check",
    "description": "Submit the result of reviewing an epic for missing implementation specifics.",
    "input_schema": {
        "type": "object",
        "properties": {
            "needs_clarification": {
                "type": "boolean",
                "description": "True only if the epic is missing specifics that would block writing concrete stories.",
            },
            "questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "1-3 targeted questions for the product owner. Empty if needs_clarification=false.",
            },
            "reasoning": {
                "type": "string",
                "description": "One sentence explaining why clarification is or is not needed.",
            },
        },
        "required": ["needs_clarification", "questions", "reasoning"],
    },
}


def detect_epic_missing_specifics(
    issue_key: str,
    summary: str,
    description: str | None,
    acceptance_criteria: list[str],
) -> list[str]:
    """Return targeted clarification questions if the epic is missing implementation-critical
    specifics. Returns [] if the epic is sufficiently concrete or the check fails.

    This is a fast, cheap Claude call — typically < 300 input tokens.
    Non-fatal: callers should wrap in try/except and proceed on failure.
    """
    user_parts = [f"Epic: {issue_key}\nTitle: {summary}"]
    if description:
        user_parts.append(f"Description:\n{description}")
    if acceptance_criteria:
        acs = "\n".join(f"- {ac}" for ac in acceptance_criteria[:10])
        user_parts.append(f"Acceptance criteria:\n{acs}")
    user_content = "\n\n".join(user_parts)

    response = _CLIENT.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=_AMBIGUITY_CHECK_PROMPT,
        tools=[_AMBIGUITY_CHECK_TOOL],
        tool_choice={"type": "tool", "name": "submit_ambiguity_check"},
        messages=[{"role": "user", "content": user_content}],
    )

    logger.info(
        "Epic ambiguity check for %s — input=%s output=%s",
        issue_key, response.usage.input_tokens, response.usage.output_tokens,
    )

    tool_block = next((b for b in response.content if b.type == "tool_use"), None)
    if not tool_block:
        return []

    result = tool_block.input
    if not result.get("needs_clarification"):
        logger.info("Epic ambiguity check %s: no clarification needed — %s", issue_key, result.get("reasoning"))
        return []

    questions = result.get("questions", [])
    logger.info("Epic ambiguity check %s: %d questions — %s", issue_key, len(questions), questions)
    return questions


def plan_epic_breakdown(
    issue_key: str,
    summary: str,
    description: str | None = None,
    acceptance_criteria: list[str] | None = None,
    memory_context: str = "",
) -> dict:
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
    if description:
        user_content += f"\nDescription:\n{description}\n"
    if acceptance_criteria:
        acs = "\n".join(f"- {ac}" for ac in acceptance_criteria)
        user_content += f"\nAcceptance criteria:\n{acs}\n"
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
    profile_name = test_context.get("profile_name") or "python_fastapi"

    # Phase 15: stack-aware test conventions hint
    _STACK_TEST_HINTS = {
        "java_maven":   "JUnit 4/5 (annotations: @Test, @Disabled, @Ignore; assertions: assertEquals, assertThat)",
        "java_gradle":  "JUnit 4/5 (annotations: @Test, @Disabled, @Ignore; assertions: assertEquals, assertThat)",
        "node_react":   "Jest or Vitest (it(), test(), describe(), expect(); skip: it.skip, xtest, describe.skip)",
        "python_fastapi": "pytest (def test_*, @pytest.mark.skip, pytest.raises, assert)",
    }
    stack_hint = _STACK_TEST_HINTS.get(profile_name, "unknown stack — use generic best practices")

    diff_trimmed = (diff_context.get("full_diff") or "").strip()
    if len(diff_trimmed) > 8000:
        diff_trimmed = diff_trimmed[:8000] + "\n... (diff truncated)"

    memory_block = (
        f"Prior lessons from this repository:\n{memory_context}\n\n"
        if memory_context else ""
    )

    user_content = (
        f"{story_block}\n\n"
        f"Repo stack: {profile_name} — {stack_hint}\n\n"
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
        f"Assess the test quality using {profile_name} conventions "
        f"and call submit_test_quality_review with your structured verdict."
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


# ---------------------------------------------------------------------------
# Phase 10 — Architecture / Impact Agent
# ---------------------------------------------------------------------------

ARCHITECTURE_PROMPT = (
    "You are an independent Architecture / Impact Agent. Your job is to assess whether a PR "
    "is safe from a design and system-impact perspective — independently of whether tests pass "
    "or the code matches the Story.\n\n"
    "You will receive:\n"
    "- The Jira Story (key, summary, description, acceptance criteria)\n"
    "- Repository context (language, framework, repo slug)\n"
    "- The PR (number, title, body)\n"
    "- The unified diff and changed file list\n"
    "- Signal context (test status, reviewer verdict, test quality verdict, file count, retry count)\n"
    "- Memory context (prior lessons from this repository)\n\n"
    "Evaluate these dimensions:\n"
    "1. Scope discipline — Is the change limited to the Story scope? Unrelated refactoring or touching too many layers?\n"
    "2. API compatibility — Did request/response shape or endpoint behavior change? Backward-compatible?\n"
    "3. Data/model impact — Schema or model change? Migration needed? Risk to stored data?\n"
    "4. Dependency impact — New dependency added? Existing dependency changed? Version risk?\n"
    "5. Operational impact — Config or env var changes? Deployment/runtime impact?\n"
    "6. Security impact — Auth/permission changes? Input validation changed? Sensitive data exposure?\n"
    "7. Maintainability — Design coherent? Duplication introduced? Future maintenance risk?\n\n"
    "Verdict rules (strictly enforced):\n"
    "- ARCHITECTURE_BLOCKED: auth/security/data compatibility unsafe; change contradicts Story; "
    "large unrelated redesign introduced; response contract broken without justification.\n"
    "- ARCHITECTURE_NEEDS_REVIEW: medium-risk design concern; change is broader than Story but not dangerous; "
    "design could cause future maintenance issues.\n"
    "- ARCHITECTURE_APPROVED: change is scoped to the Story, no meaningful architecture or system risk detected.\n\n"
    "Do not penalize for style. Do not comment on test adequacy (that is the Test Quality Agent's job). "
    "Do not repeat what the Reviewer Agent says about code quality. "
    "Clarification: If there is genuine blocking architectural ambiguity that a human must resolve "
    "and an answer would change your verdict, set needs_clarification=true with a specific question and options. "
    "Only use this when truly necessary — do not avoid making a judgement call.\n\n"
    "Call the submit_architecture_review tool only — do not respond with prose."
)

_ARCHITECTURE_TOOL = {
    "name": "submit_architecture_review",
    "description": "Submit a structured architecture and system-impact verdict for a PR.",
    "input_schema": {
        "type": "object",
        "properties": {
            "architecture_status": {
                "type": "string",
                "enum": ["ARCHITECTURE_APPROVED", "ARCHITECTURE_NEEDS_REVIEW", "ARCHITECTURE_BLOCKED"],
                "description": (
                    "Overall verdict. ARCHITECTURE_BLOCKED for unsafe security/data/API changes or scope violations. "
                    "ARCHITECTURE_NEEDS_REVIEW for medium-risk design concerns. "
                    "ARCHITECTURE_APPROVED for scoped, low-risk changes."
                ),
            },
            "risk_level": {
                "type": "string",
                "enum": ["LOW", "MEDIUM", "HIGH"],
                "description": "Overall architecture risk level of this change.",
            },
            "summary": {
                "type": "string",
                "description": "1-3 sentences summarising the architecture verdict.",
            },
            "impact_areas": {
                "type": "array",
                "description": "Assessment per impact dimension.",
                "items": {
                    "type": "object",
                    "properties": {
                        "area": {
                            "type": "string",
                            "enum": ["api", "data", "security", "dependencies", "config", "scope", "maintainability"],
                            "description": "The impact dimension being assessed.",
                        },
                        "risk": {
                            "type": "string",
                            "enum": ["LOW", "MEDIUM", "HIGH"],
                            "description": "Risk level for this specific area.",
                        },
                        "finding": {
                            "type": "string",
                            "description": "Concise finding for this area.",
                        },
                    },
                    "required": ["area", "risk", "finding"],
                },
            },
            "blocking_reasons": {
                "type": "array",
                "description": "Reasons for ARCHITECTURE_BLOCKED verdict (empty if not blocked).",
                "items": {"type": "string"},
            },
            "recommendations": {
                "type": "array",
                "description": "Non-blocking suggestions for design improvements.",
                "items": {"type": "string"},
            },
        },
        "required": [
            "architecture_status", "risk_level", "summary",
            "impact_areas", "blocking_reasons", "recommendations",
        ],
    },
}

# Inject clarification fields into _ARCHITECTURE_TOOL schema
_ARCHITECTURE_TOOL["input_schema"]["properties"].update(_REVIEW_CLARIFICATION_FIELDS)


def review_architecture(
    story_context: dict,
    repo_context: dict,
    pr_context: dict,
    diff_context: dict,
    signal_context: dict,
    memory_context: str = "",
) -> dict:
    """Run the Architecture Agent against a PR and return a structured verdict.

    story_context keys: key, summary, description, acceptance_criteria (list[str])
    repo_context keys: repo_slug, primary_language, framework
    pr_context keys: number, url, title, body
    diff_context keys: full_diff (str), changed_files (list[str])
    signal_context keys: test_status, review_status, test_quality_status,
                         files_changed_count, retry_count
    memory_context: formatted bullet string from get_execution_memory()

    Returns the submit_architecture_review tool input dict:
    {
        "architecture_status": "ARCHITECTURE_APPROVED" | "ARCHITECTURE_NEEDS_REVIEW" | "ARCHITECTURE_BLOCKED",
        "risk_level":          "LOW" | "MEDIUM" | "HIGH",
        "summary":             "...",
        "impact_areas":        [{"area": ..., "risk": ..., "finding": ...}, ...],
        "blocking_reasons":    [...],
        "recommendations":     [...],
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

    repo_block = (
        f"Repository: {repo_context.get('repo_slug', '')}\n"
        f"Language: {repo_context.get('primary_language', 'unknown')}\n"
        f"Framework: {repo_context.get('framework', 'unknown')}"
    )

    pr_body = (pr_context.get("body") or "").strip()[:800]
    changed_files = "\n".join(f"  - {f}" for f in (diff_context.get("changed_files") or []))

    diff_trimmed = (diff_context.get("full_diff") or "").strip()
    if len(diff_trimmed) > 8000:
        diff_trimmed = diff_trimmed[:8000] + "\n... (diff truncated)"

    memory_block = (
        f"Prior lessons from this repository:\n{memory_context}\n\n"
        if memory_context else ""
    )

    user_content = (
        f"{story_block}\n\n"
        f"{repo_block}\n\n"
        f"PR #{pr_context.get('number', '')} — {pr_context.get('title', '')}\n"
        f"PR URL: {pr_context.get('url', '')}\n"
        f"PR body:\n{pr_body or '  (none)'}\n\n"
        f"Changed files ({signal_context.get('files_changed_count', 0)} total):\n"
        f"{changed_files or '  (none)'}\n\n"
        f"Other agent signals:\n"
        f"  Test status: {signal_context.get('test_status', 'NOT_RUN')}\n"
        f"  Reviewer verdict: {signal_context.get('review_status', 'N/A')}\n"
        f"  Test Quality verdict: {signal_context.get('test_quality_status', 'N/A')}\n"
        f"  Files changed: {signal_context.get('files_changed_count', 0)}\n"
        f"  Retry count: {signal_context.get('retry_count', 0)}\n\n"
        f"Unified diff:\n```\n{diff_trimmed}\n```\n\n"
        f"{memory_block}"
        f"Assess the architecture and system impact, then call submit_architecture_review with your verdict."
    )

    response = _CLIENT.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=[{
            "type": "text",
            "text": ARCHITECTURE_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        tools=[_ARCHITECTURE_TOOL],
        tool_choice={"type": "tool", "name": "submit_architecture_review"},
        messages=[{"role": "user", "content": user_content}],
    )

    logger.info(
        "Claude architecture_review done (sonnet) — input=%s output=%s",
        response.usage.input_tokens,
        response.usage.output_tokens,
    )

    tool_block = next((b for b in response.content if b.type == "tool_use"), None)
    if not tool_block:
        raise RuntimeError("Architecture Agent returned no tool_use block")

    return tool_block.input


# ---------------------------------------------------------------------------
# Phase 17 — Onboarding architecture summary
# ---------------------------------------------------------------------------

_ONBOARDING_ARCHITECTURE_PROMPT = (
    "You are a technical analyst performing a project onboarding scan. "
    "You will be given a repo structure scan, detected capability profile, and key file contents. "
    "Your job is to produce a concise, accurate architecture summary that will be used to inform "
    "future AI-assisted code changes. Be specific. Flag genuine uncertainty as open questions — "
    "do not invent certainty. Avoid generic boilerplate.\n\n"
    "CRITICAL: Always populate file_landmark_map with concrete file paths for the most common "
    "change types (e.g. 'homepage text → frontend/src/pages/Home.jsx', "
    "'API routes → app/routes/api.py', 'main layout → src/layouts/Layout.tsx'). "
    "Use the directory listing and any file contents provided to determine the most likely paths. "
    "For monorepos, prefix each landmark with its subdirectory. "
    "For repos with ambiguous or unknown stacks, provide your best-effort guess based on file names "
    "and directory structure, and flag any genuine uncertainty in open_questions. "
    "An approximate landmark is far more useful than an empty list — empty is only acceptable when "
    "the repo contains no source files at all."
)

_ONBOARDING_ARCHITECTURE_TOOL = {
    "name": "submit_architecture_snapshot",
    "description": "Submit a structured architecture snapshot for a project onboarding scan.",
    "input_schema": {
        "type": "object",
        "properties": {
            "architecture_summary": {
                "type": "string",
                "description": "3-5 sentence summary of what the project does, its tech stack, and overall design",
            },
            "main_modules": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Key directories or modules (e.g. 'app/routes — FastAPI route handlers')",
            },
            "entry_points": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Main entry point files or commands",
            },
            "data_flow": {
                "type": "string",
                "description": "Brief description of how data moves through the system",
            },
            "test_strategy": {
                "type": "string",
                "description": "Observed test approach (framework, coverage style, test types)",
            },
            "deployment_notes": {
                "type": "string",
                "description": "Observed deployment files or configuration (Dockerfile, CI, hosting)",
            },
            "risks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Potential risks or concerns for automated changes (e.g. no tests, complex auth)",
            },
            "open_questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Things that are unclear or need human confirmation before AI changes",
            },
            "file_landmark_map": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "File-to-purpose mapping for common code change targets. "
                    "Each entry: '<change type> → <relative/file/path>'. "
                    "Examples: 'homepage text → frontend/src/pages/Home.jsx', "
                    "'API entry point → app/main.py', 'main layout → src/layouts/Layout.tsx', "
                    "'auth logic → services/core-api/auth/routes.py'. "
                    "Include 3-8 entries covering the most likely change areas. "
                    "For monorepos prefix with the subdirectory. "
                    "For ambiguous stacks, use best-effort paths based on directory structure."
                ),
            },
        },
        "required": [
            "architecture_summary", "main_modules", "entry_points",
            "data_flow", "test_strategy", "deployment_notes", "risks", "open_questions",
            "file_landmark_map",
        ],
    },
}


def generate_onboarding_architecture_summary(
    repo_path: str,
    repo_slug: str,
    structure_scan: dict,
    profile: dict,
) -> dict:
    """Ask Claude to produce a structured architecture snapshot for project onboarding.

    Args:
        repo_path:      Path to cloned repo workspace.
        repo_slug:      Owner/repo identifier for context.
        structure_scan: Output of scan_repo_structure().
        profile:        Output of detect_repo_capability_profile().

    Returns the submit_architecture_snapshot tool input dict.
    Raises RuntimeError if Claude returns no tool_use block.
    """
    primary_language = profile.get("primary_language", "unknown")
    caps = profile.get("capabilities", {})

    # Collect key file contents — README + entry points
    key_files = _collect_key_files(repo_path, primary_language.capitalize())

    # For generic_unknown with detected monorepo components, read entry + page files
    # from each subdir. Onboarding is a one-time scan so we can be thorough here.
    monorepo_components = caps.get("monorepo_components", [])
    if monorepo_components:
        # Extended paths per profile for deep architecture analysis (not used in story flow)
        _onboarding_paths: dict[str, list[str]] = {
            CapabilityProfile.PYTHON_FASTAPI: [
                "app/main.py", "main.py", "src/main.py", "app/__init__.py",
                "app/routes.py", "app/api.py",
            ],
            CapabilityProfile.NODE_REACT: [
                "src/App.jsx", "src/App.tsx", "src/App.js",
                "src/pages/index.jsx", "src/pages/index.tsx", "src/pages/Home.jsx",
                "src/pages/Home.tsx", "pages/index.jsx", "pages/index.tsx",
                "index.js", "src/index.js",
            ],
            CapabilityProfile.JAVA_MAVEN: [
                "src/main/java/Main.java", "src/main/java/Application.java",
            ],
            CapabilityProfile.JAVA_GRADLE: [
                "src/main/java/Main.java", "src/main/java/Application.java",
            ],
        }
        for component in monorepo_components[:5]:
            sub_path = os.path.join(repo_path, component["subdir"])
            paths_to_try = _onboarding_paths.get(component["profile_name"], [])
            files_read = 0
            for rel_entry in paths_to_try:
                if files_read >= 3:
                    break
                full = os.path.join(sub_path, rel_entry)
                content = _read_truncated(full, max_lines=80)
                if content:
                    display_path = f"{component['subdir']}/{rel_entry}"
                    if not any(r == display_path for r, _ in key_files):
                        key_files.append((display_path, content))
                        files_read += 1

    # Also read package/build/config files
    config_candidates = structure_scan.get("config_files", [])
    for rel_path in config_candidates[:4]:
        full = os.path.join(repo_path, rel_path)
        content = _read_truncated(full, max_lines=60)
        if content and not any(r == rel_path for r, _ in key_files):
            key_files.append((rel_path, content))

    file_sections = ""
    for rel_path, content in key_files[:12]:
        file_sections += f"\n--- {rel_path} ---\n{content}\n"

    # Format structure scan for prompt
    def _fmt_list(lst: list) -> str:
        return ", ".join(lst[:10]) if lst else "(none)"

    structure_block = (
        f"Top-level dirs: {_fmt_list(structure_scan.get('top_level_dirs', []))}\n"
        f"Config files: {_fmt_list(structure_scan.get('config_files', []))}\n"
        f"Deploy files: {_fmt_list(structure_scan.get('deploy_files', []))}\n"
        f"Routing/API files: {_fmt_list(structure_scan.get('routing_files', []))}\n"
        f"Model files: {_fmt_list(structure_scan.get('model_files', []))}\n"
        f"Service files: {_fmt_list(structure_scan.get('service_files', []))}\n"
        f"Test files (sample): {_fmt_list(structure_scan.get('test_files', []))}\n"
        f"Total files: {structure_scan.get('total_files', 0)}, "
        f"source: {structure_scan.get('source_file_count', 0)}, "
        f"tests: {structure_scan.get('test_file_count', 0)}"
    )

    profile_block = (
        f"Profile: {profile.get('profile_name', 'unknown')}\n"
        f"Language: {profile.get('primary_language', 'unknown')}, "
        f"Framework: {profile.get('framework', 'unknown')}\n"
        f"Test command: {profile.get('test_command') or '(none)'}\n"
        f"Build command: {profile.get('build_command') or '(none)'}\n"
        f"Lint command: {profile.get('lint_command') or '(none)'}"
    )

    # Surface monorepo component info explicitly so Claude can produce better landmarks
    monorepo_block = ""
    if monorepo_components:
        lines = [
            f"  - {c['subdir']}/ ({c['profile_name']})"
            for c in monorepo_components
        ]
        monorepo_block = "\nDetected monorepo components:\n" + "\n".join(lines) + "\n"

    user_content = (
        f"Repo: {repo_slug}\n\n"
        f"Capability profile:\n{profile_block}\n"
        f"{monorepo_block}\n"
        f"Repo structure:\n{structure_block}\n\n"
        f"Key file contents:{file_sections}\n\n"
        f"Call submit_architecture_snapshot with your structured analysis."
    )

    response = _CLIENT.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=[{
            "type": "text",
            "text": _ONBOARDING_ARCHITECTURE_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        tools=[_ONBOARDING_ARCHITECTURE_TOOL],
        tool_choice={"type": "tool", "name": "submit_architecture_snapshot"},
        messages=[{"role": "user", "content": user_content}],
    )

    logger.info(
        "Onboarding architecture summary done (sonnet) — input=%s output=%s",
        response.usage.input_tokens,
        response.usage.output_tokens,
    )

    tool_block = next((b for b in response.content if b.type == "tool_use"), None)
    if not tool_block:
        raise RuntimeError("Architecture summary returned no tool_use block")

    return tool_block.input


# ---------------------------------------------------------------------------
# Phase 17 — Onboarding coding conventions snapshot
# ---------------------------------------------------------------------------

_ONBOARDING_CONVENTIONS_PROMPT = (
    "You are a technical analyst performing a project onboarding scan. "
    "You will be given key source files from a repository. "
    "Your job is to identify the coding conventions and patterns actually present in the code — "
    "naming, organisation, error handling, testing style, API style. "
    "Be specific and concrete. Reference actual patterns you observe. "
    "Do not invent conventions that aren't in the code."
)

_ONBOARDING_CONVENTIONS_TOOL = {
    "name": "submit_coding_conventions",
    "description": "Submit a structured coding conventions snapshot for a project.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "2-3 sentence plain-English summary of the dominant coding style",
            },
            "naming_conventions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Observed naming patterns (e.g. 'snake_case for Python functions', 'PascalCase for React components')",
            },
            "folder_organization": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Observed folder structure patterns (e.g. 'routes under app/routes/', 'tests mirror app structure')",
            },
            "api_style": {
                "type": "array",
                "items": {"type": "string"},
                "description": "API / route handler patterns observed",
            },
            "error_handling_style": {
                "type": "array",
                "items": {"type": "string"},
                "description": "How errors are handled in the codebase",
            },
            "test_naming_style": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Test function naming and organisation conventions",
            },
            "patterns_to_follow": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific patterns AI should replicate in new code for this repo",
            },
            "patterns_to_avoid": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Patterns that appear absent or inconsistent and should not be introduced",
            },
        },
        "required": [
            "summary", "naming_conventions", "folder_organization", "api_style",
            "error_handling_style", "test_naming_style", "patterns_to_follow", "patterns_to_avoid",
        ],
    },
}


def generate_onboarding_coding_conventions(
    repo_path: str,
    repo_slug: str,
    structure_scan: dict,
    profile: dict,
) -> dict:
    """Ask Claude to produce a structured coding conventions snapshot for project onboarding.

    Reads source files + test files to identify observed conventions.
    Returns the submit_coding_conventions tool input dict.
    Raises RuntimeError if Claude returns no tool_use block.
    """
    primary_language = profile.get("primary_language", "unknown")

    # Collect source + test file contents
    file_sections = ""
    collected = 0

    candidate_files = (
        structure_scan.get("routing_files", [])
        + structure_scan.get("model_files", [])
        + structure_scan.get("service_files", [])
        + structure_scan.get("test_files", [])[:4]
    )
    # Also add key files (README etc)
    for readme in ["README.md", "README.rst"]:
        import os as _os
        content = _read_truncated(_os.path.join(repo_path, readme), max_lines=40)
        if content:
            file_sections += f"\n--- {readme} ---\n{content}\n"
            collected += 1

    seen: set = set()
    for rel_path in candidate_files:
        if collected >= 8:
            break
        if rel_path in seen:
            continue
        seen.add(rel_path)
        full = os.path.join(repo_path, rel_path)
        content = _read_truncated(full, max_lines=80)
        if content:
            file_sections += f"\n--- {rel_path} ---\n{content}\n"
            collected += 1

    user_content = (
        f"Repo: {repo_slug}\n"
        f"Profile: {profile.get('profile_name', 'unknown')} "
        f"({profile.get('primary_language', '?')}/{profile.get('framework', '?')})\n"
        f"Top-level dirs: {', '.join(structure_scan.get('top_level_dirs', []))}\n\n"
        f"Source files:{file_sections}\n\n"
        f"Identify the coding conventions and call submit_coding_conventions."
    )

    response = _CLIENT.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=[{
            "type": "text",
            "text": _ONBOARDING_CONVENTIONS_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        tools=[_ONBOARDING_CONVENTIONS_TOOL],
        tool_choice={"type": "tool", "name": "submit_coding_conventions"},
        messages=[{"role": "user", "content": user_content}],
    )

    logger.info(
        "Onboarding coding conventions done (sonnet) — input=%s output=%s",
        response.usage.input_tokens,
        response.usage.output_tokens,
    )

    tool_block = next((b for b in response.content if b.type == "tool_use"), None)
    if not tool_block:
        raise RuntimeError("Coding conventions returned no tool_use block")

    return tool_block.input
