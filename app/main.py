import logging
import sys
from fastapi import FastAPI
from dotenv import load_dotenv

from fastapi import HTTPException
from pydantic import BaseModel

from app.database import init_db
from app.telegram import send_message
from app.webhooks import router as webhooks_router
from app.repo_mapping import get_mapping, get_all_mappings, add_mapping

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
    issue_key: str
    repo_name: str
    target_branch: str = "main"


@app.get("/debug/repo-mappings")
def list_repo_mappings():
    return get_all_mappings()


@app.get("/debug/repo-mappings/{issue_key}")
def inspect_repo_mapping(issue_key: str):
    mapping = get_mapping(issue_key)
    if not mapping:
        raise HTTPException(status_code=404, detail=f"No mapping found for '{issue_key}'")
    return mapping


@app.post("/debug/repo-mappings")
def create_repo_mapping(body: RepoMappingIn):
    return add_mapping(body.issue_key, body.repo_name, body.target_branch)
