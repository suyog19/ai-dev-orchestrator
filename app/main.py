import hashlib
import json
import logging
import sys
from fastapi import FastAPI
from dotenv import load_dotenv

from fastapi import HTTPException
from pydantic import BaseModel

from app.database import init_db, get_conn
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


# ---------------------------------------------------------------------------
# Repo mapping endpoints
# ---------------------------------------------------------------------------

class RepoMappingIn(BaseModel):
    jira_project_key: str
    repo_slug: str
    base_branch: str = "main"
    issue_type: str | None = None
    notes: str | None = None


class RepoMappingUpdate(BaseModel):
    jira_project_key: str | None = None
    repo_slug: str | None = None
    base_branch: str | None = None
    issue_type: str | None = None
    is_active: bool | None = None
    notes: str | None = None


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
