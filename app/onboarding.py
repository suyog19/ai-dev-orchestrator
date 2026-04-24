import logging

from app.database import update_onboarding_run

logger = logging.getLogger("orchestrator")


def run_project_onboarding(onboarding_run_id: int, repo_slug: str, base_branch: str):
    """Execute the project onboarding workflow.

    Each iteration adds steps here. Currently a stub that completes immediately.
    Status transitions are managed by the worker (_execute_onboarding).
    """
    logger.info("Project onboarding started: repo_slug=%s branch=%s (run_id=%s)", repo_slug, base_branch, onboarding_run_id)
    update_onboarding_run(onboarding_run_id, current_step="started")
    # Iterations 2+ will add: clone, profile detection, command validation, scanning, Claude summarisation, etc.
    logger.info("Project onboarding completed (stub): run_id=%s", onboarding_run_id)
