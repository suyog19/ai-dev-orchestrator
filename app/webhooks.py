import json
import logging
from fastapi import APIRouter, Request, HTTPException

from app.database import get_conn
from app.dispatcher import dispatch
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
    summary = fields.get("summary", "")

    # Only process events that contain an actual status change
    changelog_items = payload.get("changelog", {}).get("items", [])
    status_item = next((i for i in changelog_items if i.get("field") == "status"), None)
    if not status_item:
        logger.info("Jira webhook ignored (no status change): %s", issue_key)
        return {"received": True, "processed": False}

    new_status = status_item.get("toString", "")
    logger.info("Jira webhook received: %s | %s → %s", issue_key, issue_type, new_status)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO workflow_events (source, event_type, payload_json, status)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                ("jira", event_type, json.dumps(payload), "received"),
            )
            event_id = cur.fetchone()[0]

    send_message(
        event=f"{issue_type} status change",
        status=new_status,
        details=f"{issue_key}: {summary}",
    )

    dispatch(issue_type, new_status, event_id)

    return {"received": True, "processed": True}
