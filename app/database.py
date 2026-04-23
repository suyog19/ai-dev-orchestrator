import json
import os
import time
import logging
from contextlib import contextmanager
from pathlib import Path

import psycopg2
from psycopg2 import pool

logger = logging.getLogger("orchestrator")

_pool = None


def _create_pool():
    global _pool
    db_url = os.environ["DATABASE_URL"]
    _pool = psycopg2.pool.ThreadedConnectionPool(minconn=1, maxconn=5, dsn=db_url)


@contextmanager
def get_conn():
    conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


def init_db(retries: int = 5, delay: int = 3):
    for attempt in range(1, retries + 1):
        try:
            _create_pool()
            break
        except Exception as exc:
            if attempt == retries:
                raise
            logger.warning("DB not ready (attempt %d/%d): %s — retrying in %ds", attempt, retries, exc, delay)
            time.sleep(delay)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS workflow_events (
                    id          SERIAL PRIMARY KEY,
                    source      VARCHAR(100)  NOT NULL,
                    event_type  VARCHAR(100)  NOT NULL,
                    payload_json TEXT         NOT NULL,
                    status      VARCHAR(50)   NOT NULL DEFAULT 'received',
                    created_at  TIMESTAMP     NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    id               SERIAL PRIMARY KEY,
                    workflow_type    VARCHAR(100) NOT NULL,
                    status           VARCHAR(50)  NOT NULL DEFAULT 'RECEIVED',
                    related_event_id INTEGER      REFERENCES workflow_events(id),
                    error_detail     TEXT         NULL,
                    created_at       TIMESTAMP    NOT NULL DEFAULT NOW(),
                    updated_at       TIMESTAMP    NOT NULL DEFAULT NOW()
                )
            """)
            # Migrations — add columns that may not exist on older deployments
            for col_sql in [
                "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS error_detail   TEXT          NULL",
                "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS started_at     TIMESTAMP     NULL",
                "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS completed_at   TIMESTAMP     NULL",
                "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS current_step   VARCHAR(100)  NULL",
                "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS working_branch VARCHAR(200)  NULL",
                "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS pr_url         VARCHAR(500)  NULL",
            ]:
                cur.execute(col_sql)

            # Migrate old schema (issue_key-based) to new project-key-based schema
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'repo_mappings' AND column_name = 'issue_key'
            """)
            if cur.fetchone():
                cur.execute("DROP TABLE repo_mappings")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS repo_mappings (
                    id                SERIAL PRIMARY KEY,
                    jira_project_key  VARCHAR(50)   NOT NULL,
                    issue_type        VARCHAR(50)   NULL,
                    repo_slug         VARCHAR(200)  NOT NULL,
                    base_branch       VARCHAR(100)  NOT NULL DEFAULT 'main',
                    is_active         BOOLEAN       NOT NULL DEFAULT TRUE,
                    notes             TEXT          NULL,
                    created_at        TIMESTAMP     NOT NULL DEFAULT NOW(),
                    updated_at        TIMESTAMP     NOT NULL DEFAULT NOW()
                )
            """)

    # Seed mappings from config/seed_mappings.json
    seed_file = Path(__file__).parent.parent / "config" / "seed_mappings.json"
    if seed_file.exists():
        from app.repo_mapping import upsert_seed_mappings
        mappings = json.loads(seed_file.read_text())
        upsert_seed_mappings(mappings)

    logger.info("Database initialized — tables ready")


def fail_run(run_id: int, error_detail: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE workflow_runs
                   SET status='FAILED', error_detail=%s, completed_at=NOW(), updated_at=NOW()
                   WHERE id=%s""",
                (error_detail[:2000], run_id),
            )


def recover_stale_runs() -> int:
    """Mark any RUNNING rows as FAILED on worker startup.

    Returns the count of rows recovered.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE workflow_runs
                SET status = 'FAILED',
                    error_detail = 'Interrupted by worker restart before completion',
                    completed_at = NOW(),
                    updated_at = NOW()
                WHERE status = 'RUNNING'
                RETURNING id
                """
            )
            recovered = cur.rowcount
    return recovered


def update_run_step(run_id: int, step: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workflow_runs SET current_step=%s, updated_at=NOW() WHERE id=%s",
                (step, run_id),
            )


def update_run_field(run_id: int, **fields):
    """Update arbitrary columns on a workflow_run row."""
    allowed = {"working_branch", "pr_url"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    set_clause = ", ".join(f"{k}=%s" for k in updates)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE workflow_runs SET {set_clause}, updated_at=NOW() WHERE id=%s",
                (*updates.values(), run_id),
            )
