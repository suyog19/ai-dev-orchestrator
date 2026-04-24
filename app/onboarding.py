import json
import logging
import os
import shutil
import subprocess

from app.database import update_onboarding_run, upsert_capability_profile
from app.repo_profiler import (
    detect_repo_capability_profile,
    get_test_command_for_profile,
    get_build_command_for_profile,
    get_lint_command_for_profile,
)
from app.repo_scanner import scan_repo_structure
from app.test_runner import run_tests, run_build, run_lint

logger = logging.getLogger("orchestrator")


def _clone_repo_readonly(run_id: int, repo_slug: str, base_branch: str) -> str:
    """Clone repo at base_branch into /tmp/onboarding/<run_id>/repo (no working branch).

    Returns absolute path to the cloned directory.
    Raises RuntimeError on failure.
    """
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if not github_token:
        raise RuntimeError("GITHUB_TOKEN env var is not set")

    work_dir = f"/tmp/onboarding/{run_id}"
    os.makedirs(work_dir, exist_ok=True)
    repo_path = os.path.join(work_dir, "repo")

    clone_url = f"https://{github_token}@github.com/{repo_slug}.git"
    logger.info("Cloning %s (branch: %s) into %s", repo_slug, base_branch, repo_path)

    result = subprocess.run(
        ["git", "clone", "--depth=1", "--branch", base_branch, clone_url, repo_path],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git clone failed: {result.stderr.strip()}")

    logger.info("Onboarding clone ready at %s", repo_path)
    return repo_path


def run_project_onboarding(onboarding_run_id: int, repo_slug: str, base_branch: str):
    """Execute the project onboarding workflow.

    Steps so far:
      1. Clone repo (read-only)
      2. Detect capability profile
      3. Command validation dry-run (test / build / lint)
      4. Repo structure scan (stored as structure_scan_json)

    Workspace is cleaned up in the finally block regardless of outcome.
    Status transitions are managed by the worker (_execute_onboarding).
    """
    work_dir = f"/tmp/onboarding/{onboarding_run_id}"

    try:
        logger.info(
            "Project onboarding started: repo_slug=%s branch=%s (run_id=%s)",
            repo_slug, base_branch, onboarding_run_id,
        )

        # ------------------------------------------------------------------
        # Step 1: clone
        # ------------------------------------------------------------------
        update_onboarding_run(onboarding_run_id, current_step="cloning")
        repo_path = _clone_repo_readonly(onboarding_run_id, repo_slug, base_branch)

        # ------------------------------------------------------------------
        # Step 2: detect capability profile
        # ------------------------------------------------------------------
        update_onboarding_run(onboarding_run_id, current_step="profile_detection")
        profile = detect_repo_capability_profile(repo_path, repo_slug)
        profile_name = profile["profile_name"]

        upsert_capability_profile(repo_slug, profile)
        logger.info("Profile detected and stored for %s: %s", repo_slug, profile_name)

        test_cmd = get_test_command_for_profile(profile)
        build_cmd = get_build_command_for_profile(profile)
        lint_cmd = get_lint_command_for_profile(profile)

        update_onboarding_run(
            onboarding_run_id,
            current_step="profile_detected",
            capability_profile_name=profile_name,
            test_command=test_cmd,
            build_command=build_cmd,
            lint_command=lint_cmd,
        )

        # ------------------------------------------------------------------
        # Step 3: command validation dry-run
        # ------------------------------------------------------------------
        update_onboarding_run(onboarding_run_id, current_step="command_validation")

        # Test: always attempt if the profile says it supports tests
        test_result = "NOT_RUN"
        if test_cmd:
            logger.info("Onboarding command validation: running tests for %s", repo_slug)
            tr = run_tests(
                repo_path=repo_path,
                timeout=300,
                profile_command=test_cmd,
                profile_name=profile_name,
            )
            test_result = tr["status"]
            logger.info("Onboarding test result: %s", test_result)
        else:
            logger.info("Onboarding command validation: no test command — skipping")

        # Build: attempt if profile supports it
        build_result = "NOT_RUN"
        if build_cmd:
            logger.info("Onboarding command validation: running build for %s", repo_slug)
            br = run_build(repo_path=repo_path, build_command=build_cmd, profile_name=profile_name, timeout=300)
            build_result = br["status"]
            logger.info("Onboarding build result: %s", build_result)
        else:
            logger.info("Onboarding command validation: no build command — skipping")

        # Lint: attempt if configured
        lint_result = "NOT_RUN"
        if lint_cmd:
            logger.info("Onboarding command validation: running lint for %s", repo_slug)
            lr = run_lint(repo_path=repo_path, lint_command=lint_cmd, profile_name=profile_name, timeout=120)
            lint_result = lr["status"]
            logger.info("Onboarding lint result: %s", lint_result)
        else:
            logger.info("Onboarding command validation: no lint command — skipping")

        update_onboarding_run(
            onboarding_run_id,
            current_step="commands_validated",
            test_result=test_result,
            build_result=build_result,
            lint_result=lint_result,
        )

        logger.info(
            "Project onboarding command validation done: run_id=%s test=%s build=%s lint=%s",
            onboarding_run_id, test_result, build_result, lint_result,
        )

        # ------------------------------------------------------------------
        # Step 4: repo structure scan
        # ------------------------------------------------------------------
        update_onboarding_run(onboarding_run_id, current_step="structure_scan")
        structure = scan_repo_structure(repo_path, profile_name=profile_name)
        update_onboarding_run(
            onboarding_run_id,
            current_step="structure_scanned",
            structure_scan_json=json.dumps(structure),
        )
        logger.info(
            "Project onboarding structure scan done: run_id=%s total_files=%d dirs=%s",
            onboarding_run_id, structure["total_files"], structure["top_level_dirs"][:5],
        )

    finally:
        if os.path.isdir(work_dir):
            try:
                shutil.rmtree(work_dir)
                logger.info("Onboarding workspace cleaned up: %s", work_dir)
            except Exception as exc:
                logger.warning("Onboarding workspace cleanup failed: %s", exc)
