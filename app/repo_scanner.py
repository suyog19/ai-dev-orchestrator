"""
Phase 17 — Repo structure scanner for project onboarding.

scan_repo_structure(workspace_path, profile_name) inspects a cloned repo and returns
a structured dict capturing folder layout, key files by category, and file counts.

No file content is read — only paths are analysed.
Output is bounded (lists capped) to keep JSON storage size small.
"""

import json
import logging
import os

logger = logging.getLogger("orchestrator")

_IGNORED_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache",
    ".tox", "dist", "build", "target", ".gradle", ".idea", ".vscode",
    "coverage", ".coverage", ".pytest_cache", ".next", ".nuxt", "out",
}

_MAX_LIST = 20  # cap any single list to this many entries

# Config / build / infra files that are always interesting
_CONFIG_FILENAMES = {
    "requirements.txt", "pyproject.toml", "setup.cfg", "setup.py",
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
    "gradlew", "Makefile", "Dockerfile", "docker-compose.yml",
    "docker-compose.yaml", ".env.example", ".env.sample",
    "vite.config.js", "vite.config.ts", "next.config.js", "next.config.ts",
    "next.config.mjs", "webpack.config.js", "tsconfig.json", "jest.config.js",
    "jest.config.ts", "pytest.ini", "conftest.py", ".flake8", ".eslintrc",
    ".eslintrc.json", ".eslintrc.js", "pyproject.toml",
}

# Deploy / infra files
_DEPLOY_FILENAMES = {
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    ".github", "k8s", "kubernetes", "terraform", "helm", "nginx.conf",
    "Procfile", "app.yaml", "serverless.yml",
}

# Patterns that signal routing/API files by path fragment
_ROUTING_PATTERNS = (
    "/routes", "/router", "/api", "/views", "/controllers", "/endpoints",
    "main.py", "app.py", "index.js", "index.ts", "server.js", "server.ts",
    "App.jsx", "App.tsx", "App.vue",
)

# Patterns that signal models/entities
_MODEL_PATTERNS = (
    "/models", "/entities", "/schemas", "/types", "/domain",
    "model.py", "models.py", "schema.py", "schemas.py",
)

# Patterns that signal services/components/business logic
_SERVICE_PATTERNS = (
    "/services", "/service", "/components", "/hooks", "/utils", "/helpers",
    "/lib", "/core", "/business",
)

_DOC_FILENAMES = {"README.md", "README.rst", "CHANGELOG.md", "CONTRIBUTING.md", "docs"}


def _rel(workspace: str, path: str) -> str:
    return os.path.relpath(path, workspace)


def scan_repo_structure(workspace_path: str, profile_name: str | None = None) -> dict:
    """Walk workspace_path and return a bounded structural summary dict.

    Returns:
        {
            "top_level_dirs":  list[str],   # direct children that are directories
            "top_level_files": list[str],   # direct children that are files
            "config_files":    list[str],   # detected config / build files
            "deploy_files":    list[str],   # Dockerfiles, CI, infra files
            "routing_files":   list[str],   # routing / API / entry points
            "model_files":     list[str],   # models / entities / schemas
            "service_files":   list[str],   # services / components / utils
            "test_files":      list[str],   # test files (sample)
            "doc_files":       list[str],   # README / docs
            "total_files":     int,
            "source_file_count": int,
            "test_file_count": int,
        }
    """
    top_level_dirs: list[str] = []
    top_level_files: list[str] = []
    config_files: list[str] = []
    deploy_files: list[str] = []
    routing_files: list[str] = []
    model_files: list[str] = []
    service_files: list[str] = []
    test_files: list[str] = []
    doc_files: list[str] = []

    total_files = 0
    test_file_count = 0
    source_file_count = 0

    # Top-level children
    try:
        for entry in sorted(os.listdir(workspace_path)):
            full = os.path.join(workspace_path, entry)
            if os.path.isdir(full):
                if entry not in _IGNORED_DIRS:
                    top_level_dirs.append(entry)
                    if entry in _DEPLOY_FILENAMES:
                        deploy_files.append(entry)
                    if entry in _DOC_FILENAMES:
                        doc_files.append(entry)
            else:
                top_level_files.append(entry)
                if entry in _CONFIG_FILENAMES:
                    config_files.append(entry)
                if entry in _DEPLOY_FILENAMES:
                    deploy_files.append(entry)
                if entry in _DOC_FILENAMES:
                    doc_files.append(entry)
    except OSError:
        pass

    # Walk the full tree
    for root, dirs, files in os.walk(workspace_path):
        dirs[:] = sorted(d for d in dirs if d not in _IGNORED_DIRS)

        rel_root = _rel(workspace_path, root)

        for fname in files:
            total_files += 1
            rel_path = os.path.join(rel_root, fname) if rel_root != "." else fname

            # Config files in subdirectories
            if fname in _CONFIG_FILENAMES and rel_path not in config_files:
                config_files.append(rel_path)

            # Deploy/infra files
            if fname in _DEPLOY_FILENAMES and rel_path not in deploy_files:
                deploy_files.append(rel_path)

            # Docs
            if fname in _DOC_FILENAMES and rel_path not in doc_files:
                doc_files.append(rel_path)

            # Routing / API
            low = rel_path.replace("\\", "/").lower()
            if any(p in low for p in _ROUTING_PATTERNS):
                if len(routing_files) < _MAX_LIST:
                    routing_files.append(rel_path)

            # Models / entities
            if any(p in low for p in _MODEL_PATTERNS):
                if len(model_files) < _MAX_LIST:
                    model_files.append(rel_path)

            # Services / components
            if any(p in low for p in _SERVICE_PATTERNS):
                if len(service_files) < _MAX_LIST:
                    service_files.append(rel_path)

            # Test files
            is_test = (
                "test" in rel_path.lower() or "spec" in rel_path.lower()
                or fname.startswith("test_") or fname.endswith("_test.py")
                or fname.endswith(".test.js") or fname.endswith(".spec.ts")
            )
            if is_test:
                test_file_count += 1
                if len(test_files) < _MAX_LIST:
                    test_files.append(rel_path)
            else:
                source_file_count += 1

    # Deduplicate and cap all lists
    def _cap(lst: list) -> list:
        seen: set = set()
        result = []
        for x in lst:
            norm = x.replace("\\", "/")
            if norm not in seen:
                seen.add(norm)
                result.append(norm)
        return result[:_MAX_LIST]

    result = {
        "top_level_dirs":    _cap(top_level_dirs),
        "top_level_files":   _cap(top_level_files),
        "config_files":      _cap(config_files),
        "deploy_files":      _cap(deploy_files),
        "routing_files":     _cap(routing_files),
        "model_files":       _cap(model_files),
        "service_files":     _cap(service_files),
        "test_files":        _cap(test_files),
        "doc_files":         _cap(doc_files),
        "total_files":       total_files,
        "source_file_count": source_file_count,
        "test_file_count":   test_file_count,
        "profile_name":      profile_name or "unknown",
    }

    logger.info(
        "scan_repo_structure: total=%d src=%d tests=%d dirs=%s",
        total_files, source_file_count, test_file_count, top_level_dirs[:6],
    )
    return result
