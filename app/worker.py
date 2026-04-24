import os
import shutil
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

from app.database import init_db, get_conn, fail_run, update_run_step, recover_stale_runs, record_execution_feedback, expire_stale_clarifications
from app.database import update_onboarding_run
from app.queue import dequeue, queue_length
from app.telegram import send_message
from app.workflows import story_implementation, epic_breakdown
from app.onboarding import run_project_onboarding
from app.clarification import ClarificationRequested

WORKFLOW_HANDLERS = {
    "story_implementation": story_implementation,
    "epic_breakdown":       epic_breakdown,
}

MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "2"))
_semaphore = threading.Semaphore(MAX_WORKERS)


def _update_run_status(run_id: int, status: str):
    extra = ""
    if status == "RUNNING":
        extra = ", started_at=NOW()"
    elif status == "COMPLETED":
        extra = ", completed_at=NOW()"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE workflow_runs SET status=%s{extra}, updated_at=NOW() WHERE id=%s",
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
        work_dir = f"/tmp/workflows/{run_id}"

        try:
            handler(run_id, issue_key, issue_type, summary)
        except ClarificationRequested as cr:
            # Workflow paused for user input — status already set to WAITING_FOR_USER_INPUT
            logger.info(
                "Workflow WAITING_FOR_USER_INPUT: %s (run_id=%s) — clarification_id=%s",
                workflow_type, run_id, cr.clarification_id,
            )
            send_message(
                "clarification_requested", "WAITING_FOR_USER_INPUT",
                f"{issue_key}: waiting for answer to clarification {cr.clarification_id}",
            )
            return
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.error("Workflow FAILED: %s (run_id=%s) — %s", workflow_type, run_id, error_msg)
            fail_run(run_id, error_msg)
            if workflow_type == "story_implementation":
                record_execution_feedback(run_id)
            send_message("workflow", "FAILED", f"{issue_key}: {error_msg}")
            return
        finally:
            if os.path.isdir(work_dir):
                try:
                    shutil.rmtree(work_dir)
                    logger.info("Workspace cleaned up: %s", work_dir)
                except Exception as cleanup_exc:
                    logger.warning("Workspace cleanup failed for %s: %s", work_dir, cleanup_exc)

        # Guard: fail_run() may have been called inside the handler (e.g. hard test failure).
        # If so, the status is already FAILED — don't overwrite it with COMPLETED.
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM workflow_runs WHERE id=%s", (run_id,))
                row = cur.fetchone()
        if row and row[0] in ("FAILED", "WAITING_FOR_APPROVAL", "WAITING_FOR_USER_INPUT"):
            logger.info(
                "Workflow handler set terminal status %s (run_id=%s) — not overwriting with COMPLETED",
                row[0], run_id,
            )
            if row[0] == "FAILED" and workflow_type == "story_implementation":
                record_execution_feedback(run_id)
            return

        _update_run_status(run_id, "COMPLETED")
        if workflow_type == "story_implementation":
            record_execution_feedback(run_id)
        logger.info("Workflow completed: %s (run_id=%s)", workflow_type, run_id)
        send_message("workflow", "COMPLETED", f"{issue_key}: {summary}")


def _execute_onboarding(job: dict):
    onboarding_run_id = job["run_id"]
    repo_slug = job["repo_slug"]
    base_branch = job.get("base_branch", "main")

    with _semaphore:
        logger.info("Onboarding started: repo_slug=%s (run_id=%s)", repo_slug, onboarding_run_id)
        update_onboarding_run(onboarding_run_id, status="RUNNING")
        send_message("project_onboarding", "RUNNING", f"Onboarding {repo_slug} branch={base_branch}")

        try:
            run_project_onboarding(onboarding_run_id, repo_slug, base_branch)
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.error("Onboarding FAILED: repo_slug=%s (run_id=%s) — %s", repo_slug, onboarding_run_id, error_msg)
            update_onboarding_run(onboarding_run_id, status="FAILED", error_detail=error_msg, current_step=None)
            send_message("project_onboarding", "FAILED", f"{repo_slug}: {error_msg}")
            return

        update_onboarding_run(onboarding_run_id, status="COMPLETED", current_step=None)
        logger.info("Onboarding completed: repo_slug=%s (run_id=%s)", repo_slug, onboarding_run_id)
        send_message("project_onboarding", "COMPLETED", f"Onboarding complete for {repo_slug}")


def main():
    logger.info("Worker started (MAX_WORKERS=%s)", MAX_WORKERS)
    init_db()

    recovered = recover_stale_runs()
    if recovered:
        logger.warning("Startup recovery: marked %d stale RUNNING run(s) as FAILED", recovered)
        send_message("startup", "RECOVERY", f"{recovered} stale run(s) recovered — were left RUNNING before restart")
    else:
        logger.info("Startup recovery: no stale runs found")

    expired_runs = expire_stale_clarifications()
    if expired_runs:
        logger.warning("Startup recovery: expired %d stale clarification(s) for runs %s", len(expired_runs), expired_runs)
        send_message("startup", "RECOVERY", f"{len(expired_runs)} stale clarification(s) expired and their runs failed")

    _EXPIRY_CHECK_INTERVAL = 720  # ~1 hour at 5s per loop iteration
    _loop_count = 0

    while True:
        try:
            job = dequeue(timeout=5)
            if job:
                pending = queue_length()
                if pending > 0:
                    logger.info("%s job(s) still waiting in queue", pending)
                    send_message("queue", "WAITING", f"{pending} job(s) pending in queue")
                if job.get("workflow_type") == "project_onboarding":
                    threading.Thread(target=_execute_onboarding, args=(job,), daemon=True).start()
                else:
                    threading.Thread(target=_execute, args=(job,), daemon=True).start()

            # Periodic stale clarification expiry (every ~1 hour)
            _loop_count += 1
            if _loop_count % _EXPIRY_CHECK_INTERVAL == 0:
                try:
                    expired = expire_stale_clarifications()
                    if expired:
                        logger.warning("Periodic expiry: expired %d stale clarification(s) for runs %s", len(expired), expired)
                        send_message("clarification_expiry", "RECOVERY", f"{len(expired)} stale clarification(s) expired")
                except Exception as expiry_exc:
                    logger.error("Periodic expiry check failed: %s", expiry_exc)

        except Exception as exc:
            logger.error("Worker error: %s", exc)
            time.sleep(2)


if __name__ == "__main__":
    main()
