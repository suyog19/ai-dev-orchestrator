import logging
from app.database import get_conn
from app.queue import enqueue

logger = logging.getLogger("orchestrator")

# Maps (issue_type, status) → workflow_type
WORKFLOW_MAP = {
    ("Story", "READY FOR DEV"): "story_implementation",
}


def _active_run_exists(issue_key: str, workflow_type: str) -> int | None:
    """Return the run_id of an active (QUEUED or RUNNING) run for this issue, or None."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM workflow_runs
                WHERE issue_key = %s
                  AND workflow_type = %s
                  AND status IN ('QUEUED', 'RUNNING')
                ORDER BY id DESC
                LIMIT 1
                """,
                (issue_key, workflow_type),
            )
            row = cur.fetchone()
    return row[0] if row else None


def dispatch(issue_type: str, new_status: str, event_id: int, issue_key: str = "", summary: str = "") -> str | None:
    key = (issue_type, new_status.upper())
    workflow_type = WORKFLOW_MAP.get(key)

    if not workflow_type:
        logger.info("No workflow mapped for %s → %s — skipping", issue_type, new_status)
        return None

    existing = _active_run_exists(issue_key, workflow_type)
    if existing:
        logger.warning(
            "Duplicate ignored: %s already has active run_id=%s (%s) — not enqueuing",
            issue_key, existing, workflow_type,
        )
        return None

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO workflow_runs (workflow_type, status, related_event_id, issue_key)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (workflow_type, "QUEUED", event_id, issue_key),
            )
            run_id = cur.fetchone()[0]

    enqueue(run_id, workflow_type, issue_key, issue_type, summary)
    logger.info("Workflow queued: %s (run_id=%s, event_id=%s)", workflow_type, run_id, event_id)
    return workflow_type
