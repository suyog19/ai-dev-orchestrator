import logging
import sys
from fastapi import FastAPI
from dotenv import load_dotenv

from app.database import init_db
from app.telegram import send_message
from app.webhooks import router as webhooks_router

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
