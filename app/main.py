import hashlib
import json
import logging
import sys
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from dotenv import load_dotenv

from fastapi import HTTPException, Request
from pydantic import BaseModel

from app.security import admin_key_middleware

from app.database import (
    init_db, get_conn,
    list_planning_runs, get_planning_run_detail,
    approve_planning_run, reject_planning_run, record_planning_feedback,
    generate_epic_outcome_rollup,
    list_agent_reviews,
    list_test_quality_reviews,
    list_architecture_reviews,
    list_security_events,
    get_all_control_flags, set_control_flag, record_security_event, is_paused,
)
from app.telegram import send_message
from app.webhooks import router as webhooks_router
from app.ui import router as ui_router
from app.repo_mapping import get_all_mappings, get_mapping_by_id, add_mapping, update_mapping, disable_mapping
from app.database import add_manual_memory, generate_repo_memory_snapshot
from app.database import list_github_status_updates, find_runs_eligible_for_status_backfill, get_overview_stats
from app.database import list_capability_profiles, get_active_capability_profile
from app.database import (
    upsert_deployment_profile, get_deployment_profile, list_deployment_profiles,
    update_deployment_profile_field, list_deployment_validations, get_deployment_validation,
)

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
app.add_middleware(BaseHTTPMiddleware, dispatch=admin_key_middleware)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(webhooks_router)
app.include_router(ui_router)


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


# ---------------------------------------------------------------------------
# Agent review inspection
# ---------------------------------------------------------------------------

@app.get("/debug/agent-reviews")
def get_agent_reviews(
    run_id: int | None = None,
    repo_slug: str | None = None,
    review_status: str | None = None,
    limit: int = 20,
):
    """List agent_reviews rows. Filter by run_id, repo_slug, or review_status."""
    return list_agent_reviews(
        run_id=run_id,
        repo_slug=repo_slug,
        review_status=review_status,
        limit=limit,
    )


@app.get("/debug/workflow-runs/{run_id}/reviews")
def get_workflow_run_reviews(run_id: int):
    """Return all Reviewer Agent verdicts for a specific workflow run."""
    reviews = list_agent_reviews(run_id=run_id)
    if not reviews:
        raise HTTPException(status_code=404, detail=f"No reviews found for run_id={run_id}")
    return {"run_id": run_id, "reviews": reviews, "count": len(reviews)}


@app.get("/debug/test-quality-reviews")
def get_test_quality_reviews(
    run_id: int | None = None,
    repo_slug: str | None = None,
    quality_status: str | None = None,
    limit: int = 20,
):
    """List agent_test_quality_reviews rows. Filter by run_id, repo_slug, or quality_status."""
    return list_test_quality_reviews(
        run_id=run_id,
        repo_slug=repo_slug,
        quality_status=quality_status,
        limit=limit,
    )


@app.get("/debug/workflow-runs/{run_id}/test-quality")
def get_workflow_run_test_quality(run_id: int):
    """Return all Test Quality Agent verdicts for a specific workflow run."""
    reviews = list_test_quality_reviews(run_id=run_id)
    if not reviews:
        raise HTTPException(status_code=404, detail=f"No test quality reviews found for run_id={run_id}")
    return {"run_id": run_id, "test_quality_reviews": reviews, "count": len(reviews)}


@app.get("/debug/architecture-reviews")
def get_architecture_reviews(
    run_id: int | None = None,
    repo_slug: str | None = None,
    architecture_status: str | None = None,
    limit: int = 20,
):
    """List agent_architecture_reviews rows. Filter by run_id, repo_slug, or architecture_status."""
    return list_architecture_reviews(
        run_id=run_id,
        repo_slug=repo_slug,
        architecture_status=architecture_status,
        limit=limit,
    )


@app.get("/debug/workflow-runs/{run_id}/architecture")
def get_workflow_run_architecture(run_id: int):
    """Return all Architecture Agent verdicts for a specific workflow run."""
    reviews = list_architecture_reviews(run_id=run_id)
    if not reviews:
        raise HTTPException(status_code=404, detail=f"No architecture reviews found for run_id={run_id}")
    return {"run_id": run_id, "architecture_reviews": reviews, "count": len(reviews)}


@app.get("/debug/workflow-runs/{run_id}/release-decision")
def get_workflow_run_release_decision(run_id: int):
    """Return the release decision and architecture status for a specific workflow run."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, release_decision, release_decision_reason, release_decided_at,
                       architecture_status, architecture_summary,
                       merge_status, review_status, test_quality_status
                FROM workflow_runs WHERE id = %s
                """,
                (run_id,),
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Workflow run {run_id} not found")
    return {
        "run_id":                  row[0],
        "release_decision":        row[1],
        "release_decision_reason": row[2],
        "release_decided_at":      row[3].isoformat() if row[3] else None,
        "architecture_status":     row[4],
        "architecture_summary":    row[5],
        "merge_status":            row[6],
        "review_status":           row[7],
        "test_quality_status":     row[8],
    }


# ---------------------------------------------------------------------------
# Phase 13 — GitHub status update inspection APIs
# ---------------------------------------------------------------------------

@app.get("/debug/github-status-updates")
def list_github_status_updates_endpoint(run_id: int):
    """List all GitHub commit status updates for a specific workflow run."""
    rows = list_github_status_updates(run_id)
    return {"run_id": run_id, "count": len(rows), "statuses": rows}


@app.get("/debug/workflow-runs/{run_id}/github-statuses")
def get_run_github_statuses(run_id: int):
    """Return GitHub commit statuses published for a specific workflow run."""
    rows = list_github_status_updates(run_id)
    return {"run_id": run_id, "count": len(rows), "statuses": rows}


@app.post("/debug/workflow-runs/{run_id}/republish-github-statuses")
def republish_github_statuses(run_id: int, repo_slug: str):
    """Re-publish GitHub commit statuses for a completed run.

    Idempotent: duplicate statuses on GitHub are acceptable (GitHub keeps the latest per context).
    A new row is always recorded in github_status_updates for the republish attempt.
    Requires: X-Orchestrator-Admin-Key header.
    """
    from app.database import get_run_verdicts
    from app.github_status_publisher import publish_github_statuses_for_run
    from app.security import ensure_github_writes_allowed

    run = get_run_verdicts(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Workflow run {run_id} not found")

    try:
        ensure_github_writes_allowed("status", repo_slug, run_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    result = publish_github_statuses_for_run(run_id, repo_slug)
    return {
        "run_id":    run_id,
        "repo_slug": repo_slug,
        "result":    result,
    }


# ---------------------------------------------------------------------------
# Epic outcome rollup — generate and inspect
# ---------------------------------------------------------------------------

@app.post("/debug/epic-outcomes/{epic_key}", status_code=200)
def generate_epic_outcome(epic_key: str):
    """Generate (or refresh) the Epic-level execution outcome rollup.

    Aggregates all Stories ever created from this Epic and their execution
    results. Upserts a memory_snapshot with scope_type='epic'.
    """
    result = generate_epic_outcome_rollup(epic_key)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"No Stories found for Epic {epic_key} (no planning_outputs with created_issue_key)",
        )
    return result


@app.get("/debug/epic-outcomes/{epic_key}", status_code=200)
def get_epic_outcome(epic_key: str):
    """Return the stored Epic-level outcome rollup snapshot."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, summary, evidence_json, created_at, updated_at
                FROM memory_snapshots
                WHERE scope_type = 'epic' AND scope_key = %s AND memory_kind = 'execution_guidance'
                """,
                (epic_key,),
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"No outcome rollup found for Epic {epic_key}. POST to generate one.",
        )
    return {
        "epic_key":      epic_key,
        "snapshot_id":   row[0],
        "summary":       row[1],
        "evidence":      json.loads(row[2]) if row[2] else None,
        "created_at":    row[3].isoformat(),
        "updated_at":    row[4].isoformat(),
    }


# ---------------------------------------------------------------------------
# Manual memory — human-authored guidance notes
# ---------------------------------------------------------------------------

class ManualMemoryIn(BaseModel):
    scope_type: str   # e.g. "repo" or "epic"
    scope_key: str    # e.g. "suyog19/sandbox-fastapi-app" or "KAN-1"
    content: str


@app.post("/debug/memory", status_code=201)
def create_manual_memory(body: ManualMemoryIn):
    """Store a human-authored guidance note for a given scope.

    Uses memory_kind='manual_note' (source='human'). The note is included
    in future planning and execution prompt enrichment alongside derived snapshots.
    Calling this endpoint again with the same scope_type/scope_key replaces the note.

    Example body:
        {"scope_type": "repo", "scope_key": "suyog19/sandbox-fastapi-app",
         "content": "Stories in this repo should stay small and avoid test edits unless explicitly requested"}
    """
    if not body.content.strip():
        raise HTTPException(status_code=400, detail="content must not be empty")
    return add_manual_memory(body.scope_type, body.scope_key, body.content.strip())


@app.get("/debug/memory")
def list_memory_snapshots(scope_type: str | None = None, scope_key: str | None = None):
    """Return memory snapshots, optionally filtered by scope_type and/or scope_key.

    Includes both derived (auto-generated) and human snapshots.
    Query params are ANDed when both are provided.
    """
    conditions = []
    params = []
    if scope_type:
        conditions.append("scope_type = %s")
        params.append(scope_type)
    if scope_key:
        conditions.append("scope_key = %s")
        params.append(scope_key)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, scope_type, scope_key, memory_kind, source,
                       summary, evidence_json, created_at, updated_at
                FROM memory_snapshots
                {where}
                ORDER BY scope_type, scope_key, memory_kind
                """,
                params,
            )
            rows = cur.fetchall()

    return [
        {
            "id":          r[0],
            "scope_type":  r[1],
            "scope_key":   r[2],
            "memory_kind": r[3],
            "source":      r[4],
            "summary":     r[5],
            "evidence":    json.loads(r[6]) if r[6] else None,
            "created_at":  r[7].isoformat(),
            "updated_at":  r[8].isoformat(),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Feedback events — raw signal inspection
# ---------------------------------------------------------------------------

@app.get("/debug/feedback-events")
def list_feedback_events(
    limit: int = 20,
    source_type: str | None = None,
    repo_slug: str | None = None,
    feedback_type: str | None = None,
    source_run_id: int | None = None,
):
    """Return raw feedback_events rows, newest first.

    Optional filters: source_type, repo_slug, feedback_type, source_run_id.
    Max limit: 100.
    """
    limit = min(limit, 100)

    conditions = []
    params: list = []
    if source_type:
        conditions.append("source_type = %s")
        params.append(source_type)
    if repo_slug:
        conditions.append("repo_slug = %s")
        params.append(repo_slug)
    if feedback_type:
        conditions.append("feedback_type = %s")
        params.append(feedback_type)
    if source_run_id is not None:
        conditions.append("source_run_id = %s")
        params.append(source_run_id)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, source_type, source_run_id, epic_key, story_key,
                       repo_slug, feedback_type, feedback_value, created_at
                FROM feedback_events
                {where}
                ORDER BY id DESC
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()

    return [
        {
            "id":            r[0],
            "source_type":   r[1],
            "source_run_id": r[2],
            "epic_key":      r[3],
            "story_key":     r[4],
            "repo_slug":     r[5],
            "feedback_type": r[6],
            "value":         r[7],
            "created_at":    r[8].isoformat(),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Memory recompute — force-refresh a derived snapshot
# ---------------------------------------------------------------------------

@app.post("/debug/memory/recompute", status_code=200)
def recompute_memory(scope_type: str, scope_key: str):
    """Force-refresh a derived memory snapshot for the given scope.

    scope_type=repo  → recomputes both planning_guidance and execution_guidance
                       for the given repo_slug (scope_key).
    scope_type=epic  → recomputes the epic execution_guidance rollup
                       for the given epic_key (scope_key).

    Returns the updated snapshot(s). Does not affect manual_note entries.
    """
    if scope_type == "repo":
        result = generate_repo_memory_snapshot(scope_key)
        return {"scope_type": scope_type, "scope_key": scope_key, "result": result}
    if scope_type == "epic":
        result = generate_epic_outcome_rollup(scope_key)
        if result is None:
            raise HTTPException(
                status_code=404,
                detail=f"No Stories found for Epic {scope_key} — cannot generate rollup.",
            )
        return {"scope_type": scope_type, "scope_key": scope_key, "result": result}
    raise HTTPException(
        status_code=400,
        detail=f"Unsupported scope_type '{scope_type}'. Use 'repo' or 'epic'.",
    )


# ---------------------------------------------------------------------------
# Clarification inspection and management APIs
# ---------------------------------------------------------------------------

@app.get("/debug/clarifications")
def list_clarifications_endpoint(
    status: str | None = None,
    run_id: int | None = None,
    limit: int = 50,
):
    """List clarification_requests. Filter by status (PENDING/ANSWERED/CANCELLED/EXPIRED) or run_id."""
    from app.database import list_clarifications
    return list_clarifications(status=status, run_id=run_id, limit=limit)


@app.get("/debug/clarifications/{clarification_id}")
def get_clarification_endpoint(clarification_id: int):
    """Return full details for a single clarification_request."""
    from app.database import get_clarification_by_id
    clar = get_clarification_by_id(clarification_id)
    if not clar:
        raise HTTPException(status_code=404, detail=f"Clarification {clarification_id} not found")
    return clar


@app.post("/debug/clarifications/{clarification_id}/answer", status_code=200)
async def admin_answer_clarification(clarification_id: int, request: Request):
    """Admin endpoint: answer a clarification and resume the workflow. Requires admin key."""
    body = await request.json()
    answer_text = body.get("answer_text", "").strip()
    if not answer_text:
        raise HTTPException(status_code=400, detail="answer_text is required")

    from app.database import get_clarification_by_id, mark_clarification_answered
    from app.clarification import resume_workflow_after_clarification

    clar = get_clarification_by_id(clarification_id)
    if not clar:
        raise HTTPException(status_code=404, detail=f"Clarification {clarification_id} not found")
    if clar["status"] != "PENDING":
        raise HTTPException(status_code=409, detail=f"Clarification {clarification_id} is {clar['status']}, not PENDING")

    ok = mark_clarification_answered(clarification_id, answer_text)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to mark clarification answered")

    resume_workflow_after_clarification(clar["run_id"])
    send_message(
        "admin_clarification_answered", "ANSWERED",
        f"Admin answered clarification {clarification_id} for run {clar['run_id']}: {answer_text[:100]}",
    )
    return {
        "clarification_id": clarification_id,
        "run_id": clar["run_id"],
        "status": "ANSWERED",
        "answer_text": answer_text,
        "summary": f"Clarification answered; run {clar['run_id']} re-enqueued.",
    }


@app.post("/debug/clarifications/{clarification_id}/cancel", status_code=200)
def admin_cancel_clarification(clarification_id: int):
    """Admin endpoint: cancel a clarification and fail the workflow. Requires admin key."""
    from app.database import get_clarification_by_id, mark_clarification_cancelled, fail_run

    clar = get_clarification_by_id(clarification_id)
    if not clar:
        raise HTTPException(status_code=404, detail=f"Clarification {clarification_id} not found")
    if clar["status"] != "PENDING":
        raise HTTPException(status_code=409, detail=f"Clarification {clarification_id} is {clar['status']}, not PENDING")

    ok = mark_clarification_cancelled(clarification_id)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to cancel clarification")

    fail_run(clar["run_id"], f"Clarification {clarification_id} cancelled by admin")
    send_message(
        "admin_clarification_cancelled", "CANCELLED",
        f"Admin cancelled clarification {clarification_id} — run {clar['run_id']} marked FAILED.",
    )
    return {
        "clarification_id": clarification_id,
        "run_id": clar["run_id"],
        "status": "CANCELLED",
        "summary": f"Clarification cancelled; run {clar['run_id']} marked FAILED.",
    }


@app.post("/debug/clarifications/{clarification_id}/resend", status_code=200)
def admin_resend_clarification(clarification_id: int):
    """Admin endpoint: resend the Telegram clarification question. Requires admin key."""
    from app.database import get_clarification_by_id, update_clarification_telegram_id
    from app.telegram import send_clarification_request

    clar = get_clarification_by_id(clarification_id)
    if not clar:
        raise HTTPException(status_code=404, detail=f"Clarification {clarification_id} not found")

    msg_id = send_clarification_request(clar)
    if msg_id:
        update_clarification_telegram_id(clarification_id, msg_id)
    return {
        "clarification_id": clarification_id,
        "telegram_message_id": msg_id,
        "summary": "Clarification question resent to Telegram.",
    }


# ---------------------------------------------------------------------------
# Admin — Security events inspection
# ---------------------------------------------------------------------------

@app.get("/admin/security-events")
def get_security_events(
    event_type: str | None = None,
    source: str | None = None,
    status: str | None = None,
    limit: int = 50,
):
    """List security audit events. Protected by admin key. Filter by event_type, source, status."""
    return list_security_events(event_type=event_type, source=source, status=status, limit=limit)


# ---------------------------------------------------------------------------
# Admin — Emergency pause / resume / control status
# ---------------------------------------------------------------------------

@app.get("/admin/control-status")
def get_control_status():
    """Return current runtime control flags."""
    flags = get_all_control_flags()
    return {"paused": is_paused(), "flags": flags}


@app.post("/admin/pause", status_code=200)
def pause_orchestrator(request: Request):
    """Pause automation — blocks new Jira workflows and Telegram commands."""
    set_control_flag("orchestrator_paused", "true")
    actor = request.client.host if request.client else "unknown"
    record_security_event(
        event_type="automation_paused",
        source="http",
        actor=actor,
        endpoint="/admin/pause",
        method="POST",
        status="PAUSED",
    )
    logger.warning("Orchestrator PAUSED by %s", actor)
    return {"paused": True, "message": "Orchestrator is now paused. New workflows and Telegram commands will be blocked."}


@app.get("/admin/github/branch-protection")
def audit_branch_protection(repo_slug: str, branch: str = "main"):
    """Fetch GitHub branch protection rules for the given repo and branch."""
    from app.github_api import get_branch_protection
    try:
        return get_branch_protection(repo_slug, branch)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GitHub API error: {exc}")


class ValidateRequiredChecksRequest(BaseModel):
    repo_slug: str
    branch: str = "main"


@app.post("/admin/github/branch-protection/validate-required-checks")
def validate_required_checks(body: ValidateRequiredChecksRequest):
    """Dry-run assessment of whether required orchestrator status checks are configured.

    Read-only — does not mutate GitHub branch protection settings.
    Returns: valid (bool), missing_required_contexts, recommendations.
    Requires: X-Orchestrator-Admin-Key header.
    """
    from app.github_api import get_branch_protection
    from app.feedback import GITHUB_REQUIRED_CHECK

    try:
        audit = get_branch_protection(body.repo_slug, body.branch)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GitHub API error: {exc}")

    orch_status = audit.get("orchestrator_check_status", {})
    missing = orch_status.get("missing_required", [])
    valid = len(missing) == 0

    recommendations = []
    if not audit.get("protected"):
        recommendations.append(
            f"Branch '{body.branch}' has no protection rules. Enable branch protection "
            "and require the orchestrator/release-gate status check."
        )
    elif not valid:
        recommendations.append(
            f"Add '{GITHUB_REQUIRED_CHECK}' as a required status check in GitHub → "
            f"Settings → Branches → {body.branch} → Required status checks."
        )
    if audit.get("allow_force_pushes"):
        recommendations.append("Disable force pushes to prevent bypassing required checks.")

    return {
        "repo_slug":                body.repo_slug,
        "branch":                   body.branch,
        "valid":                    valid,
        "protected":                audit.get("protected", False),
        "missing_required_contexts": missing,
        "optional_configured":      orch_status.get("optional_configured", []),
        "current_required_checks":  audit.get("required_status_checks", []),
        "recommendations":          recommendations,
    }


class BackfillRequest(BaseModel):
    repo_slug: str | None = None
    limit: int = 20
    only_missing: bool = True


@app.post("/admin/github/statuses/backfill")
def backfill_github_statuses(body: BackfillRequest):
    """Backfill GitHub commit statuses for recent eligible runs.

    Eligible runs: have a PR URL, release decision, and head_sha.
    Ineligible runs (no head_sha) are reported as skipped with reason.
    Safe to rerun — GitHub keeps the latest status per context, DB records each attempt.
    Requires: X-Orchestrator-Admin-Key header.
    """
    from app.github_status_publisher import publish_github_statuses_for_run
    from app.security import ensure_github_writes_allowed

    eligible = find_runs_eligible_for_status_backfill(
        repo_slug=body.repo_slug,
        limit=body.limit,
        only_missing=body.only_missing,
    )

    published_runs = []
    skipped_runs = []
    failed_runs = []

    for run in eligible:
        run_id = run["run_id"]
        repo_slug = run["repo_slug"]
        try:
            ensure_github_writes_allowed("status", repo_slug, run_id)
        except RuntimeError as exc:
            skipped_runs.append({"run_id": run_id, "reason": str(exc)})
            continue

        result = publish_github_statuses_for_run(run_id, repo_slug)
        if result["skipped"]:
            skipped_runs.append({"run_id": run_id, "reason": result["errors"][0] if result["errors"] else "skipped"})
        elif result["failed"] > 0:
            failed_runs.append({"run_id": run_id, "errors": result["errors"]})
        else:
            published_runs.append({"run_id": run_id, "published": result["published"]})

    return {
        "eligible_found":   len(eligible),
        "published_count":  len(published_runs),
        "skipped_count":    len(skipped_runs),
        "failed_count":     len(failed_runs),
        "published_runs":   published_runs,
        "skipped_runs":     skipped_runs,
        "failed_runs":      failed_runs,
    }


@app.post("/admin/resume", status_code=200)
def resume_orchestrator(request: Request):
    """Resume automation — re-enables Jira workflows and Telegram commands."""
    set_control_flag("orchestrator_paused", "false")
    actor = request.client.host if request.client else "unknown"
    record_security_event(
        event_type="automation_resumed",
        source="http",
        actor=actor,
        endpoint="/admin/resume",
        method="POST",
        status="RESUMED",
    )
    logger.info("Orchestrator RESUMED by %s", actor)
    return {"paused": False, "message": "Orchestrator is now running. New workflows will be accepted."}


# ---------------------------------------------------------------------------
# Phase 15 — Capability profiles
# ---------------------------------------------------------------------------

@app.get("/debug/repo-capability-profiles")
def list_repo_capability_profiles(repo_slug: str | None = None):
    """List capability profiles. Optionally filter by repo_slug."""
    profiles = list_capability_profiles(repo_slug=repo_slug)
    return {"profiles": profiles, "count": len(profiles)}


@app.get("/debug/repo-capability-profiles/{repo_slug:path}")
def get_repo_capability_profile(repo_slug: str):
    """Return the active capability profile for a repo_slug."""
    profile = get_active_capability_profile(repo_slug)
    if not profile:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"No active profile for {repo_slug}")
    return profile


# ---------------------------------------------------------------------------
# Phase 16 — Deployment Profiles
# ---------------------------------------------------------------------------

class DeploymentProfileBody(BaseModel):
    repo_slug: str
    environment: str = "dev"
    deployment_type: str
    base_url: str | None = None
    healthcheck_path: str | None = None
    enabled: bool = True
    smoke_tests: list | None = None


class DeploymentProfileUpdateBody(BaseModel):
    deployment_type: str | None = None
    base_url: str | None = None
    healthcheck_path: str | None = None
    enabled: bool | None = None
    smoke_tests: list | None = None


@app.get("/debug/deployment-profiles")
def list_dep_profiles(repo_slug: str | None = None):
    """List deployment profiles, optionally filtered by repo_slug."""
    profiles = list_deployment_profiles(repo_slug=repo_slug)
    return {"profiles": profiles, "count": len(profiles)}


@app.get("/debug/deployment-profiles/{repo_slug:path}")
def get_dep_profile(repo_slug: str, environment: str = "dev"):
    """Return the active deployment profile for a repo_slug + environment."""
    profile = get_deployment_profile(repo_slug, environment)
    if not profile:
        raise HTTPException(status_code=404, detail=f"No profile for {repo_slug}/{environment}")
    return profile


@app.post("/debug/deployment-profiles", status_code=201)
def create_dep_profile(body: DeploymentProfileBody):
    """Create or update a deployment profile (upsert on repo_slug+environment)."""
    if body.smoke_tests is not None:
        try:
            import json as _json
            _json.dumps(body.smoke_tests)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"Invalid smoke_tests JSON: {exc}")
    data = body.model_dump()
    profile_id = upsert_deployment_profile(data)
    return {"id": profile_id, "message": "Deployment profile created/updated"}


@app.put("/debug/deployment-profiles/{profile_id}", status_code=200)
def update_dep_profile(profile_id: int, body: DeploymentProfileUpdateBody):
    """Update specific fields on a deployment profile by id."""
    updates: dict = {}
    if body.deployment_type is not None:
        updates["deployment_type"] = body.deployment_type
    if body.base_url is not None:
        updates["base_url"] = body.base_url
    if body.healthcheck_path is not None:
        updates["healthcheck_path"] = body.healthcheck_path
    if body.enabled is not None:
        updates["enabled"] = body.enabled
    if body.smoke_tests is not None:
        import json as _json
        updates["smoke_tests_json"] = _json.dumps(body.smoke_tests)
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")
    update_deployment_profile_field(profile_id, **updates)
    return {"message": "Deployment profile updated"}


# ---------------------------------------------------------------------------
# Phase 16 — Deployment Validations (read endpoints; write via service)
# ---------------------------------------------------------------------------

@app.get("/debug/deployment-validations")
def list_dep_validations(
    run_id: int | None = None,
    repo_slug: str | None = None,
    status: str | None = None,
    limit: int = 50,
):
    """List deployment validations with optional filters."""
    rows = list_deployment_validations(run_id=run_id, repo_slug=repo_slug, status=status, limit=limit)
    return {"validations": rows, "count": len(rows)}


@app.get("/debug/workflow-runs/{run_id}/deployment-validation")
def get_run_dep_validation(run_id: int):
    """Return the latest deployment validation for a workflow run."""
    row = get_deployment_validation(run_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"No deployment validation for run {run_id}")
    return row


@app.post("/debug/workflow-runs/{run_id}/run-deployment-validation", status_code=200)
def rerun_deployment_validation(run_id: int, repo_slug: str | None = None, environment: str = "dev"):
    """Admin-triggered re-run of deployment validation for a workflow run.

    Stores a new deployment_validations row and updates workflow_runs.deployment_validation_status.
    Requires admin key auth (inherited from BaseHTTPMiddleware).
    """
    from app.database import get_conn as _gc
    from app.deployment_validator import run_deployment_validation as _run_dv
    import os

    # Resolve repo_slug from workflow_runs if not provided
    if not repo_slug:
        with _gc() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT repo_slug FROM repo_mappings rm "
                    "JOIN workflow_runs wr ON wr.id=%s "
                    "WHERE rm.jira_project_key = split_part(wr.issue_key, '-', 1) "
                    "LIMIT 1",
                    (run_id,),
                )
                row = cur.fetchone()
                if row:
                    repo_slug = row[0]

    if not repo_slug:
        raise HTTPException(
            status_code=422,
            detail="repo_slug is required (could not derive from run mapping)",
        )

    timeout_s = int(os.environ.get("DEPLOYMENT_VALIDATION_TIMEOUT_SECONDS", "120"))
    retry_n   = int(os.environ.get("DEPLOYMENT_VALIDATION_RETRY_COUNT", "3"))
    retry_d   = int(os.environ.get("DEPLOYMENT_VALIDATION_RETRY_DELAY_SECONDS", "10"))

    result = _run_dv(
        run_id=run_id,
        repo_slug=repo_slug,
        environment=environment,
        timeout_seconds=timeout_s,
        retry_count=retry_n,
        retry_delay_seconds=retry_d,
    )

    return {
        "run_id": run_id,
        "repo_slug": repo_slug,
        "environment": environment,
        "status": result["status"],
        "summary": result["summary"],
        "validation_id": result["validation_id"],
        "smoke_results": result["smoke_results"],
    }
