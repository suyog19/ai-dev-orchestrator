"""
Phase 15 — Repo capability profile detector.

detect_repo_capability_profile(workspace_path, repo_slug) inspects a cloned
workspace and returns a structured capability profile dict.

Detection is explicit and conservative: if evidence is ambiguous or missing,
we fall back to generic_unknown and disable auto-merge.
"""

import copy
import json
import logging
import os
from pathlib import Path

import yaml

from app.feedback import CapabilityProfile

logger = logging.getLogger("worker")


def get_test_command_for_profile(profile: dict) -> str | None:
    """Return the test command for this profile, or None if tests are not supported.

    None means the caller should mark test_status=NOT_RUN without attempting to run.
    """
    if not profile:
        return None
    caps = profile.get("capabilities", {})
    if not caps.get("supports_tests", False):
        return None
    return profile.get("test_command")


def get_build_command_for_profile(profile: dict) -> str | None:
    """Return the build command, or None if not supported."""
    if not profile:
        return None
    caps = profile.get("capabilities", {})
    if not caps.get("supports_build", False):
        return None
    return profile.get("build_command")


def get_lint_command_for_profile(profile: dict) -> str | None:
    """Return the lint command, or None if not supported."""
    if not profile:
        return None
    caps = profile.get("capabilities", {})
    if not caps.get("supports_lint", False):
        return None
    return profile.get("lint_command")


# ---------------------------------------------------------------------------
# Profile definitions — commands and capabilities per stack
# ---------------------------------------------------------------------------

_PROFILE_DEFAULTS: dict[str, dict] = {
    CapabilityProfile.PYTHON_FASTAPI: {
        "primary_language": "python",
        "framework": "fastapi",
        "package_manager": "pip",
        "test_command": "pytest -q --tb=short",
        "build_command": None,
        "lint_command": None,
        "source_patterns": ["app/**/*.py", "*.py"],
        "test_patterns": ["tests/**/*.py", "test_*.py", "*_test.py"],
        "capabilities": {
            "supports_tests": True,
            "supports_lint": False,
            "supports_build": False,
            "supports_import_graph": True,
            "supports_auto_merge": True,
        },
    },
    CapabilityProfile.JAVA_MAVEN: {
        "primary_language": "java",
        "framework": "maven",
        "package_manager": "maven",
        "test_command": "mvn test -q",
        "build_command": "mvn package -DskipTests -q",
        "lint_command": None,
        "source_patterns": ["src/main/java/**/*.java"],
        "test_patterns": ["src/test/java/**/*.java"],
        "capabilities": {
            "supports_tests": True,
            "supports_lint": False,
            "supports_build": True,
            "supports_import_graph": False,
            "supports_auto_merge": False,
        },
    },
    CapabilityProfile.JAVA_GRADLE: {
        "primary_language": "java",
        "framework": "gradle",
        "package_manager": "gradle",
        "test_command": "./gradlew test",
        "build_command": "./gradlew build",
        "lint_command": None,
        "source_patterns": ["src/main/java/**/*.java"],
        "test_patterns": ["src/test/java/**/*.java"],
        "capabilities": {
            "supports_tests": True,
            "supports_lint": False,
            "supports_build": True,
            "supports_import_graph": False,
            "supports_auto_merge": False,
        },
    },
    CapabilityProfile.NODE_REACT: {
        "primary_language": "javascript",
        "framework": "react",
        "package_manager": "npm",  # refined by _detect_node_package_manager
        "test_command": None,       # refined by _detect_node_scripts
        "build_command": None,
        "lint_command": None,
        "source_patterns": ["src/**/*.{js,jsx,ts,tsx}", "*.{js,ts}"],
        "test_patterns": ["**/*.test.{js,jsx,ts,tsx}", "**/*.spec.{js,ts}", "**/__tests__/**"],
        "capabilities": {
            "supports_tests": False,  # set True only if test script exists
            "supports_lint": False,
            "supports_build": False,
            "supports_import_graph": False,
            "supports_auto_merge": False,
        },
    },
    CapabilityProfile.GENERIC_UNKNOWN: {
        "primary_language": "unknown",
        "framework": None,
        "package_manager": None,
        "test_command": None,
        "build_command": None,
        "lint_command": None,
        "source_patterns": [],
        "test_patterns": [],
        "capabilities": {
            "supports_tests": False,
            "supports_lint": False,
            "supports_build": False,
            "supports_import_graph": False,
            "supports_auto_merge": False,
        },
    },
}


# ---------------------------------------------------------------------------
# Command hints loader
# ---------------------------------------------------------------------------

_HINTS_PATH = Path(__file__).parent.parent / "config" / "repo_command_hints.yaml"
_hints_cache: dict | None = None


def _load_hints() -> dict:
    """Load repo command hints from config/repo_command_hints.yaml.

    Cached after first load. Returns empty dict if file is missing or invalid.
    """
    global _hints_cache
    if _hints_cache is not None:
        return _hints_cache
    try:
        with open(_HINTS_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        _hints_cache = data.get("repos", {}) or {}
    except FileNotFoundError:
        _hints_cache = {}
    except Exception as exc:
        logger.warning("Failed to load repo_command_hints.yaml: %s", exc)
        _hints_cache = {}
    return _hints_cache


def _apply_hints(profile: dict, repo_slug: str) -> dict:
    """Apply command hints for repo_slug to profile if an entry exists.

    Returns the (possibly modified) profile. Sets profile_source to
    'configured_hint' if hints were applied, 'auto_detected' otherwise.
    """
    hints = _load_hints()
    hint = hints.get(repo_slug)
    if not hint:
        profile["profile_source"] = "auto_detected"
        return profile

    overrideable = {
        "profile_name", "test_command", "build_command", "lint_command",
        "source_patterns", "test_patterns", "primary_language", "framework",
    }
    for key in overrideable:
        if key in hint:
            profile[key] = hint[key]

    # Update capabilities based on what's now present
    if hint.get("test_command"):
        profile.setdefault("capabilities", {})["supports_tests"] = True
    if hint.get("build_command"):
        profile.setdefault("capabilities", {})["supports_build"] = True
    if hint.get("lint_command"):
        profile.setdefault("capabilities", {})["supports_lint"] = True

    profile["profile_source"] = "configured_hint"
    profile["auto_detected"] = False
    logger.info("Command hints applied for %s: profile=%s", repo_slug, profile.get("profile_name"))
    return profile


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _exists(workspace: str, *paths: str) -> bool:
    """Return True if any of the paths exist under workspace."""
    return any(os.path.exists(os.path.join(workspace, p)) for p in paths)


def _file_contains(workspace: str, filename: str, keyword: str) -> bool:
    """Return True if a file in workspace contains keyword (case-insensitive)."""
    path = os.path.join(workspace, filename)
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return keyword.lower() in f.read().lower()
    except OSError:
        return False


def _detect_python_fastapi(workspace: str) -> bool:
    """Return True if this looks like a Python/FastAPI project."""
    has_python = _exists(workspace, "requirements.txt", "pyproject.toml", "setup.cfg", "setup.py")
    if not has_python:
        return False
    # Must have FastAPI in requirements or pyproject, OR have app/main.py
    has_fastapi = (
        _file_contains(workspace, "requirements.txt", "fastapi")
        or _file_contains(workspace, "pyproject.toml", "fastapi")
        or _exists(workspace, "app/main.py")
    )
    return has_fastapi


def _detect_java_maven(workspace: str) -> bool:
    return _exists(workspace, "pom.xml")


def _detect_java_gradle(workspace: str) -> bool:
    return _exists(workspace, "build.gradle", "build.gradle.kts") or (
        _exists(workspace, "gradlew") and _exists(workspace, "settings.gradle", "settings.gradle.kts")
    )


def _detect_node_react(workspace: str) -> bool:
    if not _exists(workspace, "package.json"):
        return False
    # Prefer React/Vite/Next indicators; plain Node without these stays unknown
    return (
        _exists(workspace, "vite.config.js", "vite.config.ts",
                "next.config.js", "next.config.ts", "next.config.mjs")
        or _file_contains(workspace, "package.json", "react")
        or _file_contains(workspace, "package.json", "vite")
        or _file_contains(workspace, "package.json", "next")
        or _exists(workspace, "src")  # conventional React src/ directory
    )


def _scan_for_subdir_stacks(workspace: str) -> list[dict]:
    """Scan top-level subdirectories for recognisable profile markers.

    Used when root-level detection returns generic_unknown — catches monorepos
    and repos where the main stack lives one directory below the root.
    Returns a list of {subdir, profile_name} dicts sorted by subdir name.
    """
    results = []
    try:
        entries = sorted(os.listdir(workspace))
    except OSError:
        return results

    for entry in entries:
        if entry.startswith("."):
            continue
        subdir = os.path.join(workspace, entry)
        if not os.path.isdir(subdir):
            continue
        if _detect_java_gradle(subdir):
            results.append({"subdir": entry, "profile_name": CapabilityProfile.JAVA_GRADLE})
        elif _detect_java_maven(subdir):
            results.append({"subdir": entry, "profile_name": CapabilityProfile.JAVA_MAVEN})
        elif _detect_node_react(subdir):
            pm = _detect_node_package_manager(subdir)
            scripts = _detect_node_scripts(subdir, pm)
            results.append({
                "subdir": entry,
                "profile_name": CapabilityProfile.NODE_REACT,
                "package_manager": pm,
                **scripts["commands"],
            })
        elif _detect_python_fastapi(subdir):
            results.append({"subdir": entry, "profile_name": CapabilityProfile.PYTHON_FASTAPI})
    return results


def _detect_node_package_manager(workspace: str) -> str:
    """Detect npm/yarn/pnpm from lock files."""
    if _exists(workspace, "pnpm-lock.yaml"):
        return "pnpm"
    if _exists(workspace, "yarn.lock"):
        return "yarn"
    return "npm"


def _detect_node_scripts(workspace: str, pkg_manager: str) -> dict:
    """Read package.json scripts and return test/build/lint commands."""
    path = os.path.join(workspace, "package.json")
    commands: dict = {"test_command": None, "build_command": None, "lint_command": None}
    caps: dict = {"supports_tests": False, "supports_build": False, "supports_lint": False}
    try:
        with open(path, encoding="utf-8") as f:
            pkg = json.load(f)
        scripts = pkg.get("scripts", {})
        runner = pkg_manager
        # Use "--run" flag for vitest/non-jest runners to avoid watch mode
        if "test" in scripts:
            commands["test_command"] = f"{runner} test -- --run" if runner == "npm" else f"{runner} test"
            # For jest-based projects, don't add -- --run (it's harmless but odd)
            # Keep it simple: just use the detected command
            commands["test_command"] = f"{runner} test"
            caps["supports_tests"] = True
        if "build" in scripts:
            commands["build_command"] = f"{runner} run build"
            caps["supports_build"] = True
        if "lint" in scripts:
            commands["lint_command"] = f"{runner} run lint"
            caps["supports_lint"] = True
    except (OSError, json.JSONDecodeError):
        pass
    return {"commands": commands, "caps": caps}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_repo_capability_profile(workspace_path: str, repo_slug: str) -> dict:
    """Detect the capability profile for a cloned repo workspace.

    Detection order (first match wins):
      1. Java Gradle  (check before Maven in case both pom.xml + Gradle files exist)
      2. Java Maven
      3. Node/React
      4. Python/FastAPI
      5. generic_unknown

    Returns a profile dict compatible with upsert_capability_profile().
    """
    profile_name: str
    profile: dict

    if _detect_java_gradle(workspace_path):
        profile_name = CapabilityProfile.JAVA_GRADLE
    elif _detect_java_maven(workspace_path):
        profile_name = CapabilityProfile.JAVA_MAVEN
    elif _detect_node_react(workspace_path):
        profile_name = CapabilityProfile.NODE_REACT
    elif _detect_python_fastapi(workspace_path):
        profile_name = CapabilityProfile.PYTHON_FASTAPI
    else:
        profile_name = CapabilityProfile.GENERIC_UNKNOWN

    profile = copy.deepcopy(_PROFILE_DEFAULTS[profile_name])
    profile["profile_name"] = profile_name
    profile["auto_detected"] = True

    # For unknown root: scan one level deep for per-subdir stacks (monorepo support).
    # The root profile stays generic_unknown (no commands to run at root level),
    # but component info is stored in capabilities for the architecture prompt to use.
    if profile_name == CapabilityProfile.GENERIC_UNKNOWN:
        subdir_stacks = _scan_for_subdir_stacks(workspace_path)
        if subdir_stacks:
            profile["capabilities"]["is_monorepo"] = True
            profile["capabilities"]["monorepo_components"] = subdir_stacks
            logger.info(
                "Monorepo components detected for %s: %s",
                repo_slug, [s["subdir"] for s in subdir_stacks],
            )

    # Refine Node profiles with actual package.json scripts
    if profile_name == CapabilityProfile.NODE_REACT:
        pm = _detect_node_package_manager(workspace_path)
        profile["package_manager"] = pm
        result = _detect_node_scripts(workspace_path, pm)
        profile.update(result["commands"])
        profile["capabilities"].update(result["caps"])
        # Detect framework more precisely
        if _exists(workspace_path, "next.config.js", "next.config.ts", "next.config.mjs"):
            profile["framework"] = "next"
        elif _exists(workspace_path, "vite.config.js", "vite.config.ts"):
            profile["framework"] = "vite"

    # Refine Gradle: prefer gradlew over gradle binary
    if profile_name == CapabilityProfile.JAVA_GRADLE:
        if not _exists(workspace_path, "gradlew"):
            profile["test_command"] = "gradle test"
            profile["build_command"] = "gradle build"

    # Apply repo-specific command hints (overrides auto-detection if entry exists)
    profile = _apply_hints(profile, repo_slug)

    logger.info(
        "Profile detection for %s: %s (source=%s, language=%s, test_cmd=%s)",
        repo_slug, profile.get("profile_name"), profile.get("profile_source"),
        profile.get("primary_language"),
        profile.get("test_command"),
    )
    return profile
