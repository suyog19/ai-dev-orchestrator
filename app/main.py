import hashlib
import json
import logging
import sys
from fastapi import FastAPI
from dotenv import load_dotenv

from fastapi import HTTPException
from pydantic import BaseModel

from app.database import (
    init_db, get_conn,
    list_planning_runs, get_planning_run_detail,
    approve_planning_run, reject_planning_run, record_planning_feedback,
)
from app.telegram import send_message
from app.webhooks import router as webhooks_router
from app.repo_mapping import get_all_mappings, get_mapping_by_id, add_mapping, update_mapping, disable_mapping

load_dotenv()

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger("orchestrator")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="AI Dev Orchestrator", version="0.2.0")
app.include_router(webhooks_router)


@app.on_event("startup")
async def on_startup():
    init_db()
    logger.info("AI Dev Orchestrator started")
    send_message("startup", "ok", "AI Dev Orchestrator is running")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/healthz")
def health_check():
    logger.info("Health check called")
    return {"status": "ok"}


@app.get("/debug/send-telegram")
def debug_telegram():
    send_message("debug", "test", "Manual test from /debug/send-telegram")
    return {"sent": True}


@app.get("/debug/telegram/set-webhook")
def register_telegram_webhook(base_url: str | None = None):
    """Register the Telegram bot webhook. Call once after each new deployment.

    Pass ?base_url=https://your-domain.com or set PUBLIC_BASE_URL env var.
    """
    import os
    from app.telegram import set_webhook
    if not base_url:
        base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if not base_url:
        raise HTTPException(
            status_code=400,
            detail="Provide ?base_url=https://your-domain.com or set PUBLIC_BASE_URL env var",
        )
    webhook_url = f"{base_url.rstrip('/')}/webhooks/telegram"
    result = set_webhook(webhook_url)
    return {"webhook_url": webhook_url, "telegram_response": result}


# ---------------------------------------------------------------------------
# Repo mapping endpoints
# ---------------------------------------------------------------------------

class RepoMappingIn(BaseModel):
    jira_project_key: str
    repo_slug: str
    base_branch: str = "main"
    issue_type: str | None = None
    notes: str | None = None
    auto_merge_enabled: bool = False


class RepoMappingUpdate(BaseModel):
    jira_project_key: str | None = None
    repo_slug: str | None = None
    base_branch: str | None = None
    issue_type: str | None = None
    is_active: bool | None = None
    notes: str | None = None
    auto_merge_enabled: bool | None = None


@app.get("/debug/repo-mappings")
def list_repo_mappings():
    return get_all_mappings()


@app.get("/debug/repo-mappings/{mapping_id}")
def inspect_repo_mapping(mapping_id: int):
    mapping = get_mapping_by_id(mapping_id)
    if not mapping:
        raise HTTPException(status_code=404, detail=f"No mapping found for id={mapping_id}")
    return mapping


@app.post("/debug/repo-mappings", status_code=201)
def create_repo_mapping(body: RepoMappingIn):
    return add_mapping(
        jira_project_key=body.jira_project_key,
        repo_slug=body.repo_slug,
        base_branch=body.base_branch,
        issue_type=body.issue_type,
        notes=body.notes,
        auto_merge_enabled=body.auto_merge_enabled,
    )


@app.put("/debug/repo-mappings/{mapping_id}")
def modify_repo_mapping(mapping_id: int, body: RepoMappingUpdate):
    mapping = update_mapping(mapping_id, **body.model_dump(exclude_none=True))
    if not mapping:
        raise HTTPException(status_code=404, detail=f"No mapping found for id={mapping_id}")
    return mapping


@app.delete("/debug/repo-mappings/{mapping_id}", status_code=204)
def deactivate_repo_mapping(mapping_id: int):
    if not disable_mapping(mapping_id):
        raise HTTPException(status_code=404, detail=f"No mapping found for id={mapping_id}")


# ---------------------------------------------------------------------------
# Planning run inspection
# ---------------------------------------------------------------------------

@app.get("/debug/planning-runs")
def list_planning_runs_endpoint(limit: int = 10):
    """Return recent planning runs (epic_breakdown / feature_breakdown), newest first."""
    return list_planning_runs(limit)


@app.get("/debug/planning-runs/{run_id}")
def get_planning_run_endpoint(run_id: int):
    """Return full detail for a planning run: items, approval status, assumptions, open questions."""
    run = get_planning_run_detail(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"No planning run found for id={run_id}")
    return run


@app.post("/debug/planning-runs/{run_id}/approve", status_code=200)
def approve_planning_run_endpoint(run_id: int):
    """Approve a pending planning run and trigger Jira child creation.

    Equivalent to sending APPROVE <run_id> via Telegram.
    Only works when the run is in WAITING_FOR_APPROVAL + approval_status=PENDING.
    """
    from app.database import get_pending_planning_run
    from app.workflows import create_jira_stories_for_run
    run = get_pending_planning_run(run_id)
    if not run:
        raise HTTPException(
            status_code=404,
            detail=f"No pending planning run for id={run_id} (must be WAITING_FOR_APPROVAL + PENDING)",
        )
    approve_planning_run(run_id)
    issue_key = run.get("issue_key") or run.get("parent_issue_key", "?")
    create_jira_stories_for_run(run_id, issue_key)
    return {"approved": True, "run_id": run_id, "issue_key": issue_key}


@app.post("/debug/planning-runs/{run_id}/reject", status_code=200)
def reject_planning_run_endpoint(run_id: int):
    """Reject a pending planning run.

    Equivalent to sending REJECT <run_id> via Telegram.
    Only works when the run is in WAITING_FOR_APPROVAL + approval_status=PENDING.
    """
    from app.database import get_pending_planning_run
    from app.telegram import send_message
    run = get_pending_planning_run(run_id)
    if not run:
        raise HTTPException(
            status_code=404,
            detail=f"No pending planning run for id={run_id} (must be WAITING_FOR_APPROVAL + PENDING)",
        )
    reject_planning_run(run_id)
    record_planning_feedback(run_id)
    issue_key = run.get("issue_key") or run.get("parent_issue_key", "?")
    send_message(
        "epic_breakdown_rejected", "REJECTED",
        f"{issue_key}: proposal rejected via HTTP debug endpoint (run_id={run_id})",
    )
    return {"rejected": True, "run_id": run_id, "issue_key": issue_key}


# ---------------------------------------------------------------------------
# Mapping health / parity endpoint
# ---------------------------------------------------------------------------

@app.get("/debug/mapping-health")
def mapping_health():
    """Return active mappings and a fingerprint for quick cross-environment parity checks.

    Compare fingerprint values from dev and prod to detect drift instantly.
    """
    all_mappings = get_all_mappings()
    active = [
        {
            "jira_project_key": m["jira_project_key"],
            "issue_type": m["issue_type"],
            "repo_slug": m["repo_slug"],
            "base_branch": m["base_branch"],
        }
        for m in all_mappings
        if m["is_active"]
    ]
    # Sort for deterministic fingerprint regardless of insertion order
    active_sorted = sorted(active, key=lambda m: (m["jira_project_key"], m["issue_type"] or ""))
    fingerprint = hashlib.sha256(
        json.dumps(active_sorted, sort_keys=True).encode()
    ).hexdigest()[:10]

    return {
        "active_count": len(active_sorted),
        "fingerprint": fingerprint,
        "mappings": active_sorted,
    }


# ---------------------------------------------------------------------------
# Jira event inspector — last N raw payloads for payload format debugging
# ---------------------------------------------------------------------------

@app.get("/debug/jira-events")
def recent_jira_events(limit: int = 3):
    """Return the last N raw Jira webhook payloads as received, newest first.

    Useful for validating that real Jira payloads parse correctly.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, event_type, status, created_at, payload_json
                FROM workflow_events
                WHERE source = 'jira'
                ORDER BY id DESC
                LIMIT %s
                """,
                (min(limit, 10),),
            )
            rows = cur.fetchall()

    return [
        {
            "id": row[0],
            "event_type": row[1],
            "status": row[2],
            "created_at": row[3].isoformat(),
            "payload": json.loads(row[4]),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Workflow run inspection — no SSH required
# ---------------------------------------------------------------------------

_RUN_COLS_LIST = [
    "id", "issue_key", "workflow_type", "status", "current_step",
    "working_branch", "pr_url", "error_detail",
    "retry_count", "test_status", "merge_status",
    "started_at", "completed_at", "created_at",
]

_RUN_COLS_DETAIL = _RUN_COLS_LIST + [
    "test_command", "test_output", "files_changed_count", "merged_at",
]


def _run_row_to_dict(row, cols: list[str]) -> dict:
    return {
        col: (val.isoformat() if hasattr(val, "isoformat") else val)
        for col, val in zip(cols, row)
    }


@app.get("/debug/workflow-runs")
def list_workflow_runs(limit: int = 10):
    """Return most recent workflow runs, newest first. error_detail truncated to 300 chars."""
    select_cols = [
        "left(error_detail, 300) AS error_detail" if c == "error_detail" else c
        for c in _RUN_COLS_LIST
    ]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(select_cols)} FROM workflow_runs ORDER BY id DESC LIMIT %s",
                (min(limit, 50),),
            )
            rows = cur.fetchall()
    return [_run_row_to_dict(row, _RUN_COLS_LIST) for row in rows]


@app.get("/debug/workflow-runs/{run_id}")
def get_workflow_run(run_id: int):
    """Return full detail for a single workflow run including attempts and full test output."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(_RUN_COLS_DETAIL)} FROM workflow_runs WHERE id = %s",
                (run_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"No run found for id={run_id}")
            cur.execute(
                """
                SELECT attempt_number, attempt_type, model_used, status,
                       started_at, completed_at, test_status, files_touched, failure_summary
                FROM workflow_attempts
                WHERE run_id = %s
                ORDER BY attempt_number
                """,
                (run_id,),
            )
            attempt_rows = cur.fetchall()

    result = _run_row_to_dict(row, _RUN_COLS_DETAIL)
    result["attempts"] = [
        {
            "attempt_number": r[0],
            "attempt_type": r[1],
            "model_used": r[2],
            "status": r[3],
            "started_at": r[4].isoformat() if r[4] else None,
            "completed_at": r[5].isoformat() if r[5] else None,
            "test_status": r[6],
            "files_touched": r[7],
            "failure_summary": r[8],
        }
        for r in attempt_rows
    ]
    return result
