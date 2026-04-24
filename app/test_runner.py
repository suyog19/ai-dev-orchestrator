import logging
import os
import subprocess

from app.command_runner import run_repo_command

logger = logging.getLogger("worker")

# Any of these in the repo root signals a pytest-compatible project
_PYTEST_INDICATORS = ["pytest.ini", "pyproject.toml", "setup.cfg", "conftest.py", "tests"]


def discover_test_command(repo_path: str) -> str | None:
    """Return the test command if this repo has a supported test framework, else None.

    Kept for backwards compatibility — prefer passing a capability profile command.
    """
    for indicator in _PYTEST_INDICATORS:
        if os.path.exists(os.path.join(repo_path, indicator)):
            logger.info("Test discovery: found indicator '%s' — using pytest", indicator)
            return "pytest -q --tb=short"
    logger.info("Test discovery: no pytest indicators found — skipping tests")
    return None


def _install_python_deps(repo_path: str) -> dict | None:
    """Run pip install if requirements.txt exists. Returns error dict on failure, None on success."""
    req_file = os.path.join(repo_path, "requirements.txt")
    if not os.path.isfile(req_file):
        return None
    logger.info("Installing dependencies: pip install -r requirements.txt")
    install = subprocess.run(
        ["pip", "install", "-q", "-r", "requirements.txt"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if install.returncode != 0:
        output = (install.stdout + install.stderr)[:2000]
        logger.warning("Dependency install failed:\n%s", output)
        return {"status": "ERROR", "output": f"pip install failed:\n{output}"}
    return None


def _install_node_deps(repo_path: str, package_manager: str = "npm") -> dict | None:
    """Run npm/yarn/pnpm install. Returns error dict on failure, None on success."""
    if not os.path.isfile(os.path.join(repo_path, "package.json")):
        return None
    install_cmd = {
        "npm": ["npm", "install", "--no-audit", "--no-fund"],
        "yarn": ["yarn", "install", "--non-interactive"],
        "pnpm": ["pnpm", "install", "--no-frozen-lockfile"],
    }.get(package_manager, ["npm", "install", "--no-audit", "--no-fund"])
    logger.info("Installing Node deps: %s", " ".join(install_cmd))
    install = subprocess.run(
        install_cmd,
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if install.returncode != 0:
        output = (install.stdout + install.stderr)[:2000]
        logger.warning("Node dep install failed:\n%s", output)
        return {"status": "ERROR", "output": f"npm install failed:\n{output}"}
    return None


def run_tests(
    repo_path: str,
    timeout: int = 300,
    profile_command: str | None = None,
    profile_name: str | None = None,
) -> dict:
    """Install repo dependencies then run the test suite.

    Args:
        repo_path: Path to cloned repo workspace.
        timeout: Max seconds to allow the test command to run.
        profile_command: Explicit test command from a capability profile. When
            provided, this overrides discover_test_command(). Pass None to fall
            back to the old pytest discovery logic.
        profile_name: Profile name for logging context (optional).

    Returns a dict with keys:
      command              — the command string used, or None if tests were skipped
      exit_code            — integer exit code, or None
      output               — combined stdout+stderr, truncated to 4000 chars
      status               — NOT_RUN | PASSED | FAILED | ERROR
      dependency_install   — PASSED | FAILED | NOT_RUN (for tracking)
    """
    result: dict = {
        "command": None, "exit_code": None, "output": "",
        "status": "NOT_RUN", "dependency_install": "NOT_RUN",
    }

    # Determine the command to run
    if profile_command is not None:
        # Explicit profile command — if it's empty/None from the profile, mark NOT_RUN
        command = profile_command or None
        if command is None:
            logger.info(
                "run_tests: profile %s has no test command — marking NOT_RUN",
                profile_name or "unknown",
            )
            return result
    else:
        command = discover_test_command(repo_path)
        if command is None:
            return result

    result["command"] = command
    profile_label = profile_name or "auto-discovered"

    # Install dependencies based on profile
    if profile_name in (None, "python_fastapi"):
        err = _install_python_deps(repo_path)
        if err:
            result.update(err)
            result["dependency_install"] = "FAILED"
            return result
        result["dependency_install"] = "PASSED"
    elif profile_name == "node_react":
        # Detect package manager from lock file for install command
        if os.path.exists(os.path.join(repo_path, "pnpm-lock.yaml")):
            pm = "pnpm"
        elif os.path.exists(os.path.join(repo_path, "yarn.lock")):
            pm = "yarn"
        else:
            pm = "npm"
        err = _install_node_deps(repo_path, pm)
        if err:
            result.update(err)
            result["dependency_install"] = "FAILED"
            return result
        result["dependency_install"] = "PASSED"

    cmd_result = run_repo_command(
        workspace_path=repo_path,
        command=command,
        timeout_seconds=timeout,
        profile_name=profile_label,
        label="tests",
    )
    result["status"] = cmd_result["status"]
    result["exit_code"] = cmd_result["exit_code"]
    result["output"] = cmd_result["output"]
    return result


def run_build(
    repo_path: str,
    build_command: str | None,
    profile_name: str | None = None,
    timeout: int = 300,
) -> dict:
    """Run the build command for a profile. Returns status dict.

    Returns:
        {"status": "PASSED|FAILED|NOT_RUN|ERROR", "command": str|None, "output": str}
    """
    if not build_command:
        return {"status": "NOT_RUN", "command": None, "output": ""}
    result = run_repo_command(
        workspace_path=repo_path,
        command=build_command,
        timeout_seconds=timeout,
        profile_name=profile_name or "unknown",
        label="build",
    )
    return {"status": result["status"], "command": result["command"], "output": result["output"]}


def run_lint(
    repo_path: str,
    lint_command: str | None,
    profile_name: str | None = None,
    timeout: int = 120,
) -> dict:
    """Run the lint command for a profile. Returns status dict.

    Returns:
        {"status": "PASSED|FAILED|NOT_RUN|ERROR", "command": str|None, "output": str}
    """
    if not lint_command:
        return {"status": "NOT_RUN", "command": None, "output": ""}
    result = run_repo_command(
        workspace_path=repo_path,
        command=lint_command,
        timeout_seconds=timeout,
        profile_name=profile_name or "unknown",
        label="lint",
    )
    return {"status": result["status"], "command": result["command"], "output": result["output"]}
