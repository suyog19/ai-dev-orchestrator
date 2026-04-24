import json
import logging
import os
from fastapi import APIRouter, Request, HTTPException

from app.database import (
    get_conn,
    get_pending_planning_run, approve_planning_run, reject_planning_run,
    request_regeneration, create_planning_run,
    get_planning_run_for_regeneration, record_planning_feedback,
    is_paused, record_security_event,
)
from app.dispatcher import dispatch
from app.telegram import send_message, parse_approval_command
from app.queue import enqueue
from app.workflows import create_jira_stories_for_run

logger = logging.getLogger("orchestrator")
router = APIRouter()


@router.post("/webhooks/jira")
async def jira_webhook(request: Request, token: str | None = None):
    # Jira webhook secret validation (if JIRA_WEBHOOK_SECRET is configured)
    expected_secret = os.environ.get("JIRA_WEBHOOK_SECRET", "")
    if expected_secret:
        if not token or token != expected_secret:
            logger.warning(
                "Jira webhook: invalid or missing token from %s",
                request.client.host if request.client else "unknown",
            )
            record_security_event(
                event_type="webhook_rejected",
                source="jira",
                actor=request.client.host if request.client else "unknown",
                endpoint="/webhooks/jira",
                method="POST",
                status="REJECTED",
                details={"reason": "invalid_token"},
            )
            raise HTTPException(status_code=401, detail="Invalid or missing webhook token")

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

    # Pause check — log and drop the dispatch if orchestrator is paused
    if is_paused():
        logger.warning("Orchestrator is PAUSED — Jira webhook received but not dispatched: %s", issue_key)
        record_security_event(
            event_type="automation_paused_jira_blocked",
            source="jira",
            actor=issue_key,
            endpoint="/webhooks/jira",
            method="POST",
            status="BLOCKED",
            details={"issue_key": issue_key, "new_status": new_status},
        )
        return {"received": True, "processed": False, "reason": "orchestrator_paused"}

    dispatch(issue_type, new_status, event_id, issue_key=issue_key, summary=summary)

    return {"received": True, "processed": True}


@router.post("/webhooks/telegram")
async def telegram_webhook(request: Request):
    """Receive approval commands from the Telegram bot.

    Accepts: APPROVE <run_id> | REJECT <run_id> | REGENERATE <run_id>
    Messages from chats other than TELEGRAM_CHAT_ID are silently ignored.
    """
    try:
        payload = await request.json()
    except Exception:
        return {"ok": True}

    message = payload.get("message") or payload.get("edited_message") or {}
    text = (message.get("text") or "").strip()
    incoming_chat_id = str(message.get("chat", {}).get("id", ""))

    # Only process messages from the configured chat
    expected_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if expected_chat_id and incoming_chat_id != expected_chat_id:
        logger.warning("Telegram webhook: message from unexpected chat %s — rejected", incoming_chat_id)
        record_security_event(
            event_type="telegram_rejected",
            source="telegram",
            actor=incoming_chat_id,
            endpoint="/webhooks/telegram",
            method="POST",
            status="REJECTED",
            details={"reason": "unexpected_chat_id"},
        )
        return {"ok": True}

    if not text:
        return {"ok": True}

    cmd = parse_approval_command(text)
    if not cmd:
        logger.info("Telegram webhook: non-approval message ignored (%r)", text[:60])
        return {"ok": True}

    action, run_id = cmd

    # Sanity check run_id (must be positive and reasonable)
    if run_id <= 0 or run_id > 10_000_000:
        logger.warning("Telegram webhook: malformed run_id %s in command %s — rejected", run_id, action)
        record_security_event(
            event_type="telegram_rejected",
            source="telegram",
            actor=incoming_chat_id,
            endpoint="/webhooks/telegram",
            method="POST",
            status="REJECTED",
            details={"reason": "malformed_run_id", "run_id": run_id, "action": action},
        )
        return {"ok": True}

    # Pause check — block approval commands when paused
    if is_paused():
        logger.warning("Orchestrator is PAUSED — Telegram command blocked: %s %s", action, run_id)
        record_security_event(
            event_type="automation_paused_telegram_blocked",
            source="telegram",
            actor=incoming_chat_id,
            endpoint="/webhooks/telegram",
            method="POST",
            status="BLOCKED",
            details={"action": action, "run_id": run_id},
        )
        send_message("control", "PAUSED", f"Command {action} {run_id} blocked — orchestrator is paused.")
        return {"ok": True}
    logger.info("Telegram approval command received: %s %s", action, run_id)

    # REGENERATE accepts both pending and completed runs; APPROVE/REJECT only accept pending.
    if action == "REGENERATE":
        run = get_planning_run_for_regeneration(run_id)
        not_found_msg = (
            f"No actionable planning run found for ID {run_id}.\n"
            f"REGENERATE requires the run to be in WAITING_FOR_APPROVAL (pending) "
            f"or COMPLETED (already approved and children created)."
        )
    else:
        run = get_pending_planning_run(run_id)
        not_found_msg = (
            f"No pending planning run found for ID {run_id}.\n"
            f"Run may not exist, already actioned, or not in WAITING_FOR_APPROVAL state."
        )

    if not run:
        send_message("approval_error", "ERROR", not_found_msg)
        return {"ok": True}

    issue_key = run.get("issue_key", "?")

    if action == "APPROVE":
        approve_planning_run(run_id)
        logger.info("Planning run %s APPROVED for %s — starting Jira creation", run_id, issue_key)
        create_jira_stories_for_run(run_id, issue_key)

    elif action == "REJECT":
        reject_planning_run(run_id)
        n_events = record_planning_feedback(run_id)
        logger.info("Planning run %s REJECTED for %s — %d feedback events recorded", run_id, issue_key, n_events)
        send_message(
            "epic_breakdown_rejected", "REJECTED",
            f"{issue_key}: proposal rejected (run_id={run_id})\n"
            f"No Jira Stories will be created.",
        )

    elif action == "REGENERATE":
        request_regeneration(run_id)
        n_events = record_planning_feedback(run_id)
        logger.info("Planning run %s REGENERATE_REQUESTED for %s — %d feedback events recorded", run_id, issue_key, n_events)
        new_run_id = create_planning_run(
            issue_key=issue_key,
            workflow_type=run["workflow_type"],
            related_event_id=run.get("related_event_id"),
        )
        summary = run.get("summary") or issue_key
        enqueue(new_run_id, run["workflow_type"], issue_key, "Epic", summary)
        send_message(
            "epic_breakdown_regenerate", "RUNNING",
            f"{issue_key}: regenerating breakdown\n"
            f"Old run_id={run_id} closed. New run_id={new_run_id} queued.",
        )
        logger.info(
            "Planning run %s superseded by REGENERATE — new run_id=%s for %s",
            run_id, new_run_id, issue_key,
        )

    return {"ok": True}
