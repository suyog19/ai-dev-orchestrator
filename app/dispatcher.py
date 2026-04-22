import logging
from app.database import get_conn
from app.queue import enqueue

logger = logging.getLogger("orchestrator")

# Maps (issue_type, status) → workflow_type
WORKFLOW_MAP = {
    ("Story", "READY FOR DEV"): "story_implementation",
}


def dispatch(issue_type: str, new_status: str, event_id: int, issue_key: str = "", summary: str = "") -> str | None:
    key = (issue_type, new_status.upper())
    workflow_type = WORKFLOW_MAP.get(key)

    if not workflow_type:
        logger.info("No workflow mapped for %s → %s — skipping", issue_type, new_status)
        return None

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO workflow_runs (workflow_type, status, related_event_id)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (workflow_type, "QUEUED", event_id),
            )
            run_id = cur.fetchone()[0]

    enqueue(run_id, workflow_type, issue_key, summary)
    logger.info("Workflow queued: %s (run_id=%s, event_id=%s)", workflow_type, run_id, event_id)
    return workflow_type
