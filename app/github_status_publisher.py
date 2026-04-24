"""
Phase 13 — Publish GitHub commit statuses for all orchestrator gates.

Call publish_github_statuses_for_run() after the Release Gate decision is stored.
Status publishing is a best-effort write: failures are logged and returned in the
summary dict but never propagate exceptions to callers.
"""
import json
import logging
import os

logger = logging.getLogger("orchestrator.github_status_publisher")


def publish_github_statuses_for_run(
    run_id: int,
    repo_slug: str,
    pr_number: int | None = None,
) -> dict:
    """Publish GitHub commit statuses for all five gates of a completed run.

    Reads verdict fields from workflow_runs, maps them to GitHub states, publishes
    each status, and records them in github_status_updates.

    Returns:
        published: number of statuses successfully published
        failed: number of statuses that failed
        skipped: True if head_sha was missing (cannot publish)
        errors: list of error strings for failed statuses
    """
    from app.database import (
        get_run_verdicts, record_github_status_update, update_run_field,
    )
    from app.github_api import create_commit_status
    from app.github_status_mapper import (
        map_test_status_to_github,
        map_reviewer_status_to_github,
        map_test_quality_status_to_github,
        map_architecture_status_to_github,
        map_release_decision_to_github,
    )

    run = get_run_verdicts(run_id)
    if not run:
        logger.error("publish_github_statuses_for_run: run %s not found", run_id)
        return {"published": 0, "failed": 0, "skipped": True, "errors": [f"Run {run_id} not found"]}

    sha = run.get("head_sha")
    if not sha:
        logger.warning(
            "publish_github_statuses_for_run: run %s has no head_sha — cannot publish statuses",
            run_id,
        )
        return {"published": 0, "failed": 0, "skipped": True, "errors": ["head_sha missing"]}

    base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    target_url = f"{base_url}/debug/workflow-runs/{run_id}/release-decision" if base_url else None

    statuses = [
        map_test_status_to_github(run["test_status"]),
        map_reviewer_status_to_github(run["review_status"]),
        map_test_quality_status_to_github(run["test_quality_status"]),
        map_architecture_status_to_github(run["architecture_status"]),
        map_release_decision_to_github(run["release_decision"]),
    ]

    published = 0
    failed = 0
    errors: list[str] = []

    for status in statuses:
        context = status["context"]
        state = status["state"]
        description = status["description"]
        try:
            gh_response = create_commit_status(
                repo_slug=repo_slug,
                sha=sha,
                state=state,
                context=context,
                description=description,
                target_url=target_url,
            )
            record_github_status_update(
                run_id=run_id,
                repo_slug=repo_slug,
                commit_sha=sha,
                context=context,
                state=state,
                description=description,
                pr_number=pr_number,
                target_url=target_url,
                github_response_json=json.dumps(gh_response),
            )
            published += 1
            logger.info(
                "GitHub status published: run=%s context=%s state=%s sha=%.8s",
                run_id, context, state, sha,
            )
        except Exception as exc:
            failed += 1
            err = f"{context}: {exc}"
            errors.append(err)
            logger.error("GitHub status publish failed: run=%s %s", run_id, err)
            # Record the failure in DB so it's inspectable
            try:
                record_github_status_update(
                    run_id=run_id,
                    repo_slug=repo_slug,
                    commit_sha=sha,
                    context=context,
                    state="error",
                    description=f"Publish error: {str(exc)[:100]}",
                    pr_number=pr_number,
                    target_url=target_url,
                )
            except Exception:
                pass

    # Mark run as published (even partial — the individual rows record what happened)
    if published > 0:
        from datetime import datetime, timezone
        update_run_field(
            run_id,
            github_statuses_published=True,
            github_statuses_published_at=datetime.now(timezone.utc),
        )

    return {
        "published": published,
        "failed":    failed,
        "skipped":   False,
        "errors":    errors,
    }
