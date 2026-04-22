import json
import logging
from fastapi import APIRouter, Request, HTTPException

from app.database import get_conn
from app.telegram import send_message

logger = logging.getLogger("orchestrator")
router = APIRouter()


@router.post("/webhooks/jira")
async def jira_webhook(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_type = payload.get("webhookEvent", "unknown")
    issue = payload.get("issue", {})
    issue_key = issue.get("key", "unknown")
    fields = issue.get("fields", {})
    issue_type = fields.get("issuetype", {}).get("name", "unknown")
    status = fields.get("status", {}).get("name", "unknown")
    summary = fields.get("summary", "")

    # Only process events that contain an actual status change
    changelog_items = payload.get("changelog", {}).get("items", [])
    status_changed = any(item.get("field") == "status" for item in changelog_items)
    if not status_changed:
        logger.info("Jira webhook ignored (no status change): %s", issue_key)
        return {"received": True, "processed": False}

    logger.info("Jira webhook received: %s | %s | %s → %s", issue_key, issue_type, event_type, status)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO workflow_events (source, event_type, payload_json, status)
                VALUES (%s, %s, %s, %s)
                """,
                ("jira", event_type, json.dumps(payload), "received"),
            )

    send_message(
        event=f"{issue_type} {event_type}",
        status=status,
        details=f"{issue_key}: {summary}",
    )

    return {"received": True}
