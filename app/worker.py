import os
import sys
import logging
import threading
import time
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger("worker")

from app.database import init_db, get_conn
from app.queue import dequeue, queue_length
from app.telegram import send_message
from app.workflows import story_implementation

WORKFLOW_HANDLERS = {
    "story_implementation": story_implementation,
}

MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "2"))
_semaphore = threading.Semaphore(MAX_WORKERS)


def _update_run_status(run_id: int, status: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workflow_runs SET status=%s, updated_at=NOW() WHERE id=%s",
                (status, run_id),
            )


def _execute(job: dict):
    run_id = job["run_id"]
    workflow_type = job["workflow_type"]
    issue_key = job["issue_key"]
    issue_type = job.get("issue_type", "Story")
    summary = job["summary"]

    handler = WORKFLOW_HANDLERS.get(workflow_type)
    if not handler:
        logger.warning("No handler for workflow type: %s (run_id=%s)", workflow_type, run_id)
        _update_run_status(run_id, "COMPLETED")
        return

    with _semaphore:
        logger.info("Workflow started: %s (run_id=%s)", workflow_type, run_id)
        _update_run_status(run_id, "RUNNING")
        send_message("workflow", "RUNNING", f"{issue_key}: {summary}")

        handler(run_id, issue_key, issue_type, summary)

        _update_run_status(run_id, "COMPLETED")
        logger.info("Workflow completed: %s (run_id=%s)", workflow_type, run_id)
        send_message("workflow", "COMPLETED", f"{issue_key}: {summary}")


def main():
    logger.info("Worker started (MAX_WORKERS=%s)", MAX_WORKERS)
    init_db()

    while True:
        try:
            job = dequeue(timeout=5)
            if job:
                pending = queue_length()
                if pending > 0:
                    logger.info("%s job(s) still waiting in queue", pending)
                    send_message("queue", "WAITING", f"{pending} job(s) pending in queue")
                threading.Thread(target=_execute, args=(job,), daemon=True).start()
        except Exception as exc:
            logger.error("Worker error: %s", exc)
            time.sleep(2)


if __name__ == "__main__":
    main()
