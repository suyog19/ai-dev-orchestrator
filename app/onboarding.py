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

    Iteration 2: clone repo + detect capability profile.
    Iterations 3+ will add command validation, structure scanning, Claude summaries, etc.
    Status transitions are managed by the worker (_execute_onboarding).
    """
    work_dir = f"/tmp/onboarding/{onboarding_run_id}"

    try:
        logger.info("Project onboarding started: repo_slug=%s branch=%s (run_id=%s)", repo_slug, base_branch, onboarding_run_id)

        # --- Step 1: clone ---
        update_onboarding_run(onboarding_run_id, current_step="cloning")
        repo_path = _clone_repo_readonly(onboarding_run_id, repo_slug, base_branch)

        # --- Step 2: detect capability profile ---
        update_onboarding_run(onboarding_run_id, current_step="profile_detection")
        profile = detect_repo_capability_profile(repo_path, repo_slug)
        profile_name = profile["profile_name"]

        # Persist to repo_capability_profiles table (same as story_implementation)
        upsert_capability_profile(repo_slug, profile)
        logger.info("Profile detected and stored for %s: %s", repo_slug, profile_name)

        # Capture commands for the onboarding run record
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

        logger.info("Project onboarding phase 2 complete: run_id=%s profile=%s", onboarding_run_id, profile_name)

    finally:
        if os.path.isdir(work_dir):
            try:
                shutil.rmtree(work_dir)
                logger.info("Onboarding workspace cleaned up: %s", work_dir)
            except Exception as exc:
                logger.warning("Onboarding workspace cleanup failed: %s", exc)
