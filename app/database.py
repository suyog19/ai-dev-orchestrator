import os
import time
import logging
from contextlib import contextmanager

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
            # Add error_detail to existing tables that predate this column
            cur.execute("""
                ALTER TABLE workflow_runs
                ADD COLUMN IF NOT EXISTS error_detail TEXT NULL
            """)

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

    logger.info("Database initialized — tables ready")


def fail_run(run_id: int, error_detail: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workflow_runs SET status='FAILED', error_detail=%s, updated_at=NOW() WHERE id=%s",
                (error_detail[:2000], run_id),
            )
