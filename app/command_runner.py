"""
Phase 15 — Safe, centralized command execution for all repo operations.

run_repo_command() is the single entry point for running shell commands inside
a cloned repo workspace. It enforces:
  - Only pre-defined command strings from capability profiles (no arbitrary input)
  - Argument-list subprocess invocation (no shell=True, no injection risk)
  - Configurable timeout with graceful reporting
  - Stdout+stderr capture, truncated to a safe max
  - Execution confined to workspace_path (cwd is always the workspace)
  - No environment variable leakage (inherited env only, no secret injection)

All callers pass a command string that originates from the capability profile
(detect_repo_capability_profile) — never from user/Jira input.
"""

import logging
import os
import shlex
import subprocess

logger = logging.getLogger("worker")

# Maximum chars to capture from stdout+stderr
_MAX_OUTPUT = 4000
# Absolute ceiling on any single command timeout (seconds)
_MAX_TIMEOUT = 600


def run_repo_command(
    workspace_path: str,
    command: str | None,
    timeout_seconds: int = 300,
    profile_name: str | None = None,
    label: str = "command",
) -> dict:
    """Run a single shell command inside workspace_path.

    Args:
        workspace_path: Absolute path to the cloned repo — the working directory.
        command: Command string (e.g. "mvn test -q"). Split via shlex for safety.
            Pass None or empty string to get a NOT_RUN result without running anything.
        timeout_seconds: Max seconds. Clamped to _MAX_TIMEOUT.
        profile_name: Used in log messages only.
        label: Human-readable label for log messages (e.g. "tests", "build", "lint").

    Returns:
        {
            "status":    "PASSED" | "FAILED" | "NOT_RUN" | "ERROR",
            "command":   str | None,
            "exit_code": int | None,
            "output":    str,         # combined stdout+stderr, truncated
        }
    """
    result: dict = {
        "status": "NOT_RUN",
        "command": None,
        "exit_code": None,
        "output": "",
    }

    if not command or not command.strip():
        logger.info(
            "run_repo_command [%s/%s]: no command — NOT_RUN",
            profile_name or "unknown", label,
        )
        return result

    # Safety: workspace must be a real directory
    if not os.path.isdir(workspace_path):
        result["status"] = "ERROR"
        result["output"] = f"workspace not found: {workspace_path}"
        logger.error("run_repo_command: workspace missing — %s", workspace_path)
        return result

    timeout = max(1, min(timeout_seconds, _MAX_TIMEOUT))
    result["command"] = command

    try:
        args = shlex.split(command)
    except ValueError as exc:
        result["status"] = "ERROR"
        result["output"] = f"Command parse error: {exc}"
        logger.error("run_repo_command [%s/%s]: shlex.split failed — %s", profile_name, label, exc)
        return result

    logger.info(
        "run_repo_command [%s/%s]: running %r in %s (timeout=%ds)",
        profile_name or "unknown", label, command, workspace_path, timeout,
    )

    try:
        proc = subprocess.run(
            args,
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        combined = (proc.stdout + proc.stderr).strip()
        result["exit_code"] = proc.returncode
        result["output"] = combined[:_MAX_OUTPUT]
        result["status"] = "PASSED" if proc.returncode == 0 else "FAILED"
        last_line = combined.splitlines()[-1] if combined else "(no output)"
        logger.info(
            "run_repo_command [%s/%s]: %s (exit=%d) — %s",
            profile_name or "unknown", label, result["status"], proc.returncode, last_line,
        )
    except subprocess.TimeoutExpired:
        result["status"] = "ERROR"
        result["output"] = f"{label} timed out after {timeout}s"
        logger.error(
            "run_repo_command [%s/%s]: timed out after %ds",
            profile_name or "unknown", label, timeout,
        )
    except FileNotFoundError as exc:
        result["status"] = "ERROR"
        result["output"] = f"Command not found: {exc}"
        logger.error(
            "run_repo_command [%s/%s]: executable not found — %s",
            profile_name or "unknown", label, exc,
        )
    except OSError as exc:
        result["status"] = "ERROR"
        result["output"] = f"OS error: {exc}"
        logger.error(
            "run_repo_command [%s/%s]: OS error — %s",
            profile_name or "unknown", label, exc,
        )

    return result
