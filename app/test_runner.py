import os
import subprocess
import logging

logger = logging.getLogger("worker")

# Any of these in the repo root signals a pytest-compatible project
_PYTEST_INDICATORS = ["pytest.ini", "pyproject.toml", "setup.cfg", "conftest.py", "tests"]


def discover_test_command(repo_path: str) -> str | None:
    """Return the test command if this repo has a supported test framework, else None."""
    for indicator in _PYTEST_INDICATORS:
        if os.path.exists(os.path.join(repo_path, indicator)):
            logger.info("Test discovery: found indicator '%s' — using pytest", indicator)
            return "pytest -q --tb=short"
    logger.info("Test discovery: no pytest indicators found — skipping tests")
    return None


def run_tests(repo_path: str, timeout: int = 120) -> dict:
    """Install repo dependencies then run the test suite.

    Returns a dict with keys:
      command   — the command string used, or None if tests were skipped
      exit_code — integer exit code, or None
      output    — combined stdout+stderr, truncated to 4000 chars
      status    — NOT_RUN | PASSED | FAILED | ERROR
    """
    result: dict = {"command": None, "exit_code": None, "output": "", "status": "NOT_RUN"}

    command = discover_test_command(repo_path)
    if command is None:
        return result

    result["command"] = command

    req_file = os.path.join(repo_path, "requirements.txt")
    if os.path.isfile(req_file):
        logger.info("Installing dependencies: pip install -r requirements.txt")
        install = subprocess.run(
            ["pip", "install", "-q", "-r", "requirements.txt"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if install.returncode != 0:
            output = (install.stdout + install.stderr)[:2000]
            logger.warning("Dependency install failed:\n%s", output)
            result["status"] = "ERROR"
            result["output"] = f"pip install failed:\n{output}"
            return result

    logger.info("Running tests: %s", command)
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
        logger.info("Tests %s (exit=%d) — %s", result["status"], proc.returncode, last_line)
    except subprocess.TimeoutExpired:
        result["status"] = "ERROR"
        result["output"] = f"Tests timed out after {timeout}s"
        logger.error("Test run timed out after %ds", timeout)

    return result
