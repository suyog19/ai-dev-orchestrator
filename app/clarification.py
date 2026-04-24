"""
Phase 12 — Clarification loop core mechanics.

pause_for_clarification() — create request, set WAITING_FOR_USER_INPUT, send Telegram, raise exception
resume_workflow_after_clarification() — re-enqueue the run so the worker picks it up
ClarificationRequested — exception caught by worker, not treated as failure
"""
import logging
import json

from app.feedback import CLARIFICATION_ENABLED, CLARIFICATION_TIMEOUT_HOURS

logger = logging.getLogger("orchestrator")


class ClarificationRequested(Exception):
    """Raised when a workflow pauses to wait for user input.

    The worker catches this and transitions the run to WAITING_FOR_USER_INPUT
    without marking it FAILED.
    """
    def __init__(self, clarification_id: int, question: str):
        self.clarification_id = clarification_id
        self.question = question
        super().__init__(f"Waiting for clarification {clarification_id}: {question[:80]}")


def is_clarification_enabled() -> bool:
    """Check if clarification is enabled (feedback constant + DB control flag)."""
    if not CLARIFICATION_ENABLED:
        return False
    try:
        from app.database import get_control_flag
        return get_control_flag("clarification_enabled", "true").lower() == "true"
    except Exception:
        return CLARIFICATION_ENABLED


def pause_for_clarification(
    run_id: int,
    question: str,
    context_key: str,
    context_summary: str | None = None,
    options: list[str] | None = None,
    workflow_type: str | None = None,
    issue_key: str | None = None,
    repo_slug: str | None = None,
) -> None:
    """Create a clarification request, transition run to WAITING_FOR_USER_INPUT,
    send Telegram message, and raise ClarificationRequested.

    The calling workflow should NOT catch ClarificationRequested — the worker
    catches it and handles the state transition to WAITING_FOR_USER_INPUT.
    """
    from app.database import (
        create_clarification_request, get_conn,
        update_clarification_telegram_id,
    )
    from app.telegram import send_clarification_request

    clarification_id = create_clarification_request(
        run_id=run_id,
        question=question,
        context_key=context_key,
        context_summary=context_summary,
        options=options,
        workflow_type=workflow_type,
        issue_key=issue_key,
        repo_slug=repo_slug,
        timeout_hours=CLARIFICATION_TIMEOUT_HOURS,
    )

    # Transition the run to WAITING_FOR_USER_INPUT immediately (before raising)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE workflow_runs
                SET status = 'WAITING_FOR_USER_INPUT', updated_at = NOW()
                WHERE id = %s
                """,
                (run_id,),
            )

    # Send Telegram notification
    clarification_row = {
        "id": clarification_id,
        "run_id": run_id,
        "issue_key": issue_key,
        "question": question,
        "options": options,
    }
    try:
        msg_id = send_clarification_request(clarification_row)
        if msg_id:
            update_clarification_telegram_id(clarification_id, msg_id)
    except Exception as exc:
        logger.error("Failed to send clarification Telegram message: %s", exc)

    logger.info(
        "Workflow paused for clarification: run_id=%s clarification_id=%s context_key=%s",
        run_id, clarification_id, context_key,
    )
    raise ClarificationRequested(clarification_id, question)


def resume_workflow_after_clarification(run_id: int) -> None:
    """Re-enqueue a run after its clarification has been answered.

    Fetches workflow_type, issue_key, and summary from DB, then enqueues
    the same run_id again so the worker picks it up and re-runs the workflow.
    The workflow will detect the answered clarification and inject the answer.
    """
    from app.database import get_conn
    from app.queue import enqueue

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT workflow_type, issue_key, summary
                FROM workflow_runs WHERE id = %s
                """,
                (run_id,),
            )
            row = cur.fetchone()

    if not row:
        logger.error("resume_workflow_after_clarification: run_id=%s not found", run_id)
        return

    workflow_type, issue_key, summary = row
    issue_key = issue_key or "UNKNOWN"
    summary = summary or issue_key

    logger.info(
        "Resuming workflow after clarification: run_id=%s workflow_type=%s issue_key=%s",
        run_id, workflow_type, issue_key,
    )
    enqueue(
        run_id=run_id,
        workflow_type=workflow_type,
        issue_key=issue_key,
        issue_type="Story",
        summary=summary,
    )
