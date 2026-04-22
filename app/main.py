import logging
import sys
from fastapi import FastAPI

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

app = FastAPI(title="AI Dev Orchestrator", version="0.1.0")


@app.on_event("startup")
async def on_startup():
    logger.info("AI Dev Orchestrator started")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/healthz")
def health_check():
    logger.info("Health check called")
    return {"status": "ok"}
