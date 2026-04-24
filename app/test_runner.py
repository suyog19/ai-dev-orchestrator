import logging
import os
import subprocess

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
      command   — the command string used, or None if tests were skipped
      exit_code — integer exit code, or None
      output    — combined stdout+stderr, truncated to 4000 chars
      status    — NOT_RUN | PASSED | FAILED | ERROR
    """
    result: dict = {"command": None, "exit_code": None, "output": "", "status": "NOT_RUN"}

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
            return result

    logger.info("Running tests [profile=%s]: %s", profile_label, command)
    try:
        proc = subprocess.run(
            command.split(),
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        combined = (proc.stdout + proc.stderr).strip()
        result["exit_code"] = proc.returncode
        result["output"] = combined[:4000]
        result["status"] = "PASSED" if proc.returncode == 0 else "FAILED"
        last_line = combined.splitlines()[-1] if combined else ""
        logger.info(
            "Tests %s [profile=%s] (exit=%d) — %s",
            result["status"], profile_label, proc.returncode, last_line,
        )
    except subprocess.TimeoutExpired:
        result["status"] = "ERROR"
        result["output"] = f"Tests timed out after {timeout}s"
        logger.error("Test run timed out after %ds [profile=%s]", timeout, profile_label)

    return result
