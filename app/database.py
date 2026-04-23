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
                # Phase 3/4 columns
                "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS error_detail        TEXT          NULL",
                "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS started_at          TIMESTAMP     NULL",
                "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS completed_at        TIMESTAMP     NULL",
                "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS current_step        VARCHAR(100)  NULL",
                "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS working_branch      VARCHAR(200)  NULL",
                "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS pr_url              VARCHAR(500)  NULL",
                "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS issue_key           VARCHAR(100)  NULL",
                # Phase 5 columns
                "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS test_status         VARCHAR(20)   NULL",
                "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS test_command        VARCHAR(200)  NULL",
                "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS test_output         TEXT          NULL",
                "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS retry_count         INTEGER       NOT NULL DEFAULT 0",
                "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS files_changed_count INTEGER       NULL",
                "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS merge_status        VARCHAR(30)   NULL",
                "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS merged_at           TIMESTAMP     NULL",
                # Phase 6 columns
                "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS parent_issue_key             VARCHAR(100) NULL",
                "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS approval_status              VARCHAR(30)  NULL",
                "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS approval_requested_at        TIMESTAMP    NULL",
                "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS approval_received_at         TIMESTAMP    NULL",
                "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS created_jira_children_count  INTEGER      NULL",
                # Phase 6 Iteration 8 — store planning metadata for API inspection
                "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS assumptions_json    TEXT NULL",
                "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS open_questions_json TEXT NULL",
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
                    id                  SERIAL PRIMARY KEY,
                    jira_project_key    VARCHAR(50)   NOT NULL,
                    issue_type          VARCHAR(50)   NULL,
                    repo_slug           VARCHAR(200)  NOT NULL,
                    base_branch         VARCHAR(100)  NOT NULL DEFAULT 'main',
                    is_active           BOOLEAN       NOT NULL DEFAULT TRUE,
                    notes               TEXT          NULL,
                    created_at          TIMESTAMP     NOT NULL DEFAULT NOW(),
                    updated_at          TIMESTAMP     NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute(
                "ALTER TABLE repo_mappings ADD COLUMN IF NOT EXISTS "
                "auto_merge_enabled BOOLEAN NOT NULL DEFAULT FALSE"
            )

            cur.execute("""
                CREATE TABLE IF NOT EXISTS planning_outputs (
                    id                  SERIAL PRIMARY KEY,
                    run_id              INTEGER       NOT NULL REFERENCES workflow_runs(id),
                    parent_issue_key    VARCHAR(100)  NOT NULL,
                    parent_issue_type   VARCHAR(50)   NOT NULL,
                    proposed_issue_type VARCHAR(50)   NOT NULL,
                    sequence_number     INTEGER       NOT NULL,
                    title               TEXT          NOT NULL,
                    description         TEXT          NULL,
                    acceptance_criteria TEXT          NULL,
                    rationale           TEXT          NULL,
                    dependency_notes    TEXT          NULL,
                    risk_notes          TEXT          NULL,
                    confidence          VARCHAR(20)   NULL,
                    status              VARCHAR(30)   NOT NULL DEFAULT 'PROPOSED',
                    created_issue_key   VARCHAR(100)  NULL,
                    created_at          TIMESTAMP     NOT NULL DEFAULT NOW(),
                    updated_at          TIMESTAMP     NOT NULL DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS workflow_attempts (
                    id              SERIAL PRIMARY KEY,
                    run_id          INTEGER       NOT NULL REFERENCES workflow_runs(id),
                    attempt_number  INTEGER       NOT NULL,
                    attempt_type    VARCHAR(20)   NOT NULL,
                    model_used      VARCHAR(100)  NULL,
                    status          VARCHAR(20)   NOT NULL DEFAULT 'RUNNING',
                    started_at      TIMESTAMP     NOT NULL DEFAULT NOW(),
                    completed_at    TIMESTAMP     NULL,
                    failure_summary TEXT          NULL,
                    test_status     VARCHAR(20)   NULL,
                    files_touched   TEXT          NULL
                )
            """)

            # Phase 7 — feedback and memory tables
            cur.execute("""
                CREATE TABLE IF NOT EXISTS feedback_events (
                    id              SERIAL PRIMARY KEY,
                    source_type     VARCHAR(50)   NOT NULL,
                    source_run_id   INTEGER       NOT NULL REFERENCES workflow_runs(id),
                    epic_key        VARCHAR(100)  NULL,
                    story_key       VARCHAR(100)  NULL,
                    repo_slug       VARCHAR(200)  NULL,
                    feedback_type   VARCHAR(100)  NOT NULL,
                    feedback_value  VARCHAR(500)  NULL,
                    details_json    TEXT          NULL,
                    created_at      TIMESTAMP     NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS memory_snapshots (
                    id              SERIAL PRIMARY KEY,
                    scope_type      VARCHAR(50)   NOT NULL,
                    scope_key       VARCHAR(200)  NOT NULL,
                    memory_kind     VARCHAR(50)   NOT NULL,
                    summary         TEXT          NOT NULL,
                    evidence_json   TEXT          NULL,
                    created_at      TIMESTAMP     NOT NULL DEFAULT NOW(),
                    updated_at      TIMESTAMP     NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_snapshots_scope_kind
                ON memory_snapshots (scope_type, scope_key, memory_kind)
            """)
            cur.execute(
                "ALTER TABLE memory_snapshots ADD COLUMN IF NOT EXISTS "
                "source VARCHAR(20) NOT NULL DEFAULT 'derived'"
            )

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


def record_attempt(run_id: int, attempt_number: int, attempt_type: str, model_used: str | None = None) -> int:
    """Insert a new workflow_attempts row and return its id."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO workflow_attempts
                   (run_id, attempt_number, attempt_type, model_used, status)
                   VALUES (%s, %s, %s, %s, 'RUNNING')
                   RETURNING id""",
                (run_id, attempt_number, attempt_type, model_used),
            )
            return cur.fetchone()[0]


def complete_attempt(
    attempt_id: int,
    status: str,
    failure_summary: str | None = None,
    test_status: str | None = None,
    files_touched: str | None = None,
):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE workflow_attempts
                   SET status=%s, completed_at=NOW(),
                       failure_summary=%s, test_status=%s, files_touched=%s
                   WHERE id=%s""",
                (status, failure_summary, test_status, files_touched, attempt_id),
            )


def update_run_field(run_id: int, **fields):
    """Update arbitrary columns on a workflow_run row."""
    allowed = {
        "working_branch", "pr_url",
        "test_status", "test_command", "test_output",
        "retry_count", "files_changed_count", "merge_status", "merged_at",
        # Phase 6
        "parent_issue_key", "approval_status",
        "approval_requested_at", "approval_received_at",
        "created_jira_children_count",
    }
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


# ---------------------------------------------------------------------------
# Phase 6 — planning helpers
# ---------------------------------------------------------------------------

def add_planning_output(
    run_id: int,
    parent_issue_key: str,
    parent_issue_type: str,
    proposed_issue_type: str,
    sequence_number: int,
    title: str,
    description: str | None = None,
    acceptance_criteria: str | None = None,
    rationale: str | None = None,
    dependency_notes: str | None = None,
    risk_notes: str | None = None,
    confidence: str | None = None,
) -> int:
    """Insert one proposed child item into planning_outputs. Returns the new row id."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO planning_outputs
                    (run_id, parent_issue_key, parent_issue_type, proposed_issue_type,
                     sequence_number, title, description, acceptance_criteria,
                     rationale, dependency_notes, risk_notes, confidence)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (run_id, parent_issue_key, parent_issue_type, proposed_issue_type,
                 sequence_number, title, description, acceptance_criteria,
                 rationale, dependency_notes, risk_notes, confidence),
            )
            return cur.fetchone()[0]


def get_planning_outputs(run_id: int) -> list[dict]:
    """Return all planning_outputs rows for a run, ordered by sequence_number."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, run_id, parent_issue_key, parent_issue_type, proposed_issue_type,
                       sequence_number, title, description, acceptance_criteria,
                       rationale, dependency_notes, risk_notes, confidence,
                       status, created_issue_key, created_at, updated_at
                FROM planning_outputs
                WHERE run_id = %s
                ORDER BY sequence_number
                """,
                (run_id,),
            )
            cols = [
                "id", "run_id", "parent_issue_key", "parent_issue_type", "proposed_issue_type",
                "sequence_number", "title", "description", "acceptance_criteria",
                "rationale", "dependency_notes", "risk_notes", "confidence",
                "status", "created_issue_key", "created_at", "updated_at",
            ]
            return [
                {c: (v.isoformat() if hasattr(v, "isoformat") else v) for c, v in zip(cols, row)}
                for row in cur.fetchall()
            ]


def update_planning_output_status(output_id: int, status: str, created_issue_key: str | None = None):
    """Update status (and optionally created_issue_key) on a planning_outputs row."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE planning_outputs
                SET status=%s, created_issue_key=%s, updated_at=NOW()
                WHERE id=%s
                """,
                (status, created_issue_key, output_id),
            )


def request_planning_approval(run_id: int):
    """Mark a planning run as awaiting approval."""
    update_run_field(
        run_id,
        approval_status="PENDING",
        approval_requested_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )


def set_planning_approval(run_id: int, approval_status: str):
    """Record the approval decision (APPROVED | REJECTED | REGENERATE_REQUESTED)."""
    update_run_field(
        run_id,
        approval_status=approval_status,
        approval_received_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )


def set_run_waiting_for_approval(run_id: int):
    """Transition a planning run to WAITING_FOR_APPROVAL — persists across worker restarts."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workflow_runs SET status='WAITING_FOR_APPROVAL', updated_at=NOW() WHERE id=%s",
                (run_id,),
            )


# ---------------------------------------------------------------------------
# Phase 6 — Approval gate helpers
# ---------------------------------------------------------------------------

def get_pending_planning_run(run_id: int) -> dict | None:
    """Return a planning run dict if it is WAITING_FOR_APPROVAL with approval_status=PENDING.

    Includes issue_key, workflow_type, related_event_id, parent_issue_key, and summary
    extracted from the original workflow_events payload.
    Returns None if the run doesn't exist or is not in a pending approval state.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT wr.id, wr.issue_key, wr.workflow_type, wr.related_event_id,
                       wr.parent_issue_key, we.payload_json
                FROM workflow_runs wr
                LEFT JOIN workflow_events we ON we.id = wr.related_event_id
                WHERE wr.id = %s
                  AND wr.status = 'WAITING_FOR_APPROVAL'
                  AND wr.approval_status = 'PENDING'
                """,
                (run_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    cols = ["id", "issue_key", "workflow_type", "related_event_id", "parent_issue_key", "payload_json"]
    result = dict(zip(cols, row))
    try:
        payload = json.loads(result.pop("payload_json") or "{}")
        result["summary"] = payload.get("issue", {}).get("fields", {}).get("summary", "")
    except Exception:
        result["summary"] = ""
    return result


def approve_planning_run(run_id: int):
    """Transition a planning run to APPROVED — ready for Jira child creation."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE workflow_runs
                SET status='APPROVED', approval_status='APPROVED',
                    approval_received_at=NOW(), updated_at=NOW()
                WHERE id=%s
                """,
                (run_id,),
            )


def reject_planning_run(run_id: int):
    """Transition a planning run to FAILED/REJECTED and mark all its proposals as REJECTED."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE workflow_runs
                SET status='FAILED', approval_status='REJECTED',
                    approval_received_at=NOW(),
                    error_detail='Rejected by user via Telegram',
                    completed_at=NOW(), updated_at=NOW()
                WHERE id=%s
                """,
                (run_id,),
            )
            cur.execute(
                "UPDATE planning_outputs SET status='REJECTED', updated_at=NOW() WHERE run_id=%s",
                (run_id,),
            )


def request_regeneration(run_id: int):
    """Close an existing planning run as superseded and mark its proposals as REJECTED."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE workflow_runs
                SET status='FAILED', approval_status='REGENERATE_REQUESTED',
                    approval_received_at=NOW(),
                    error_detail='Regeneration requested by user via Telegram',
                    completed_at=NOW(), updated_at=NOW()
                WHERE id=%s
                """,
                (run_id,),
            )
            cur.execute(
                "UPDATE planning_outputs SET status='REJECTED', updated_at=NOW() WHERE run_id=%s",
                (run_id,),
            )


def create_planning_run(issue_key: str, workflow_type: str, related_event_id: int | None) -> int:
    """Insert a new QUEUED workflow_run for a planning workflow and return its id."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO workflow_runs (workflow_type, status, related_event_id, issue_key)
                VALUES (%s, 'QUEUED', %s, %s)
                RETURNING id
                """,
                (workflow_type, related_event_id, issue_key),
            )
            return cur.fetchone()[0]


def complete_planning_run(run_id: int, created_count: int):
    """Transition a planning run to COMPLETED after all Jira children are created."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE workflow_runs
                SET status='COMPLETED', completed_at=NOW(), updated_at=NOW(),
                    created_jira_children_count=%s
                WHERE id=%s
                """,
                (created_count, run_id),
            )


def generate_epic_outcome_rollup(epic_key: str) -> dict | None:
    """Compute and upsert an Epic-level execution outcome rollup into memory_snapshots.

    Aggregates all Stories ever created for this Epic (via planning_outputs with
    created_issue_key set) and their most-recent execution run outcomes.
    Returns None if no stories were ever created for this Epic.
    Called on_write after execution feedback when the story has an Epic parent.
    """
    from app.feedback import MemoryScope, MemoryKind
    from app.telegram import send_message

    with get_conn() as conn:
        with conn.cursor() as cur:
            # Stories actually created in Jira for this Epic (across all planning runs)
            # Use DISTINCT ON to deduplicate if the same story_key appears in multiple runs.
            cur.execute(
                """
                SELECT DISTINCT ON (po.created_issue_key)
                    po.created_issue_key                             AS story_key,
                    wr.status                                        AS run_status,
                    wr.retry_count,
                    wr.test_status,
                    wr.merge_status,
                    wr.id                                            AS run_id
                FROM planning_outputs po
                LEFT JOIN LATERAL (
                    SELECT status, retry_count, test_status, merge_status, id
                    FROM workflow_runs
                    WHERE issue_key = po.created_issue_key
                      AND workflow_type = 'story_implementation'
                    ORDER BY id DESC
                    LIMIT 1
                ) wr ON TRUE
                WHERE po.parent_issue_key = %s
                  AND po.created_issue_key IS NOT NULL
                ORDER BY po.created_issue_key
                """,
                (epic_key,),
            )
            story_rows = cur.fetchall()

    if not story_rows:
        return None

    stories_created  = len(story_rows)
    stories_executed = sum(1 for _, rs, *_ in story_rows if rs is not None)
    stories_completed = sum(1 for _, rs, *_ in story_rows if rs == "COMPLETED")
    stories_failed   = sum(1 for _, rs, *_ in story_rows if rs == "FAILED")
    retry_heavy      = sum(1 for _, rs, rc, *_ in story_rows if (rc or 0) >= 1 and rs == "COMPLETED")
    merged           = sum(1 for _, _rs, _rc, _ts, ms, *_ in story_rows if ms == "MERGED")

    bullets = [f"Epic {epic_key}: {stories_created} Stories created from planning"]
    if stories_executed > 0:
        bullets.append(
            f"{stories_executed} executed: {stories_completed} completed, {stories_failed} failed"
        )
    if merged > 0:
        bullets.append(f"{merged} merged to main branch")
    if retry_heavy > 0:
        bullets.append(f"{retry_heavy} required a fix attempt before passing")
    if stories_executed < stories_created:
        pending = stories_created - stories_executed
        bullets.append(f"{pending} not yet executed")

    summary = "\n".join(f"- {b}" for b in bullets)

    story_details = [
        {
            "story_key":   sk,
            "run_status":  rs,
            "retry_count": rc,
            "test_status": ts,
            "merge_status": ms,
            "run_id":      rid,
        }
        for sk, rs, rc, ts, ms, rid in story_rows
    ]
    evidence = {
        "epic_key":          epic_key,
        "stories_created":   stories_created,
        "stories_executed":  stories_executed,
        "stories_completed": stories_completed,
        "stories_failed":    stories_failed,
        "retry_heavy":       retry_heavy,
        "merged":            merged,
        "stories":           story_details,
    }

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO memory_snapshots
                    (scope_type, scope_key, memory_kind, summary, evidence_json)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (scope_type, scope_key, memory_kind) DO UPDATE
                    SET summary       = EXCLUDED.summary,
                        evidence_json = EXCLUDED.evidence_json,
                        updated_at    = NOW()
                RETURNING id, (xmax = 0) AS is_insert
                """,
                (
                    MemoryScope.EPIC, epic_key, MemoryKind.EXECUTION_GUIDANCE,
                    summary, json.dumps(evidence),
                ),
            )
            snap_id, is_insert = cur.fetchone()

    logger.info(
        "generate_epic_outcome_rollup: upserted snapshot for %s (id=%s, new=%s)",
        epic_key, snap_id, is_insert,
    )
    if is_insert:
        send_message(
            "epic_outcome_ready", "READY",
            f"{epic_key}: Epic outcome rollup created (snapshot_id={snap_id})\n{summary}",
        )
    return {"id": snap_id, "is_new": bool(is_insert), "summary": summary}


def generate_repo_memory_snapshot(repo_slug: str) -> dict:
    """Compute and upsert repo-level planning + execution memory snapshots.

    Derives structured guidance from feedback_events for the given repo.
    Called on_write after feedback capture (memory_refresh_mode = 'on_write').
    Returns dict with snapshot IDs for execution_guidance and planning_guidance.
    """
    from app.feedback import MemoryScope, MemoryKind
    from app.telegram import send_message

    with get_conn() as conn:
        with conn.cursor() as cur:

            # --- Resolve all project_keys mapped to this repo ---
            cur.execute(
                "SELECT DISTINCT jira_project_key FROM repo_mappings WHERE repo_slug=%s AND is_active=TRUE",
                (repo_slug,),
            )
            project_keys = [row[0] for row in cur.fetchall()]

            # --- Execution stats ---
            cur.execute(
                """
                SELECT
                    COUNT(DISTINCT source_run_id) FILTER (WHERE feedback_type='execution_completed') AS completed,
                    COUNT(DISTINCT source_run_id) FILTER (WHERE feedback_type='execution_failed') AS failed,
                    ROUND(AVG(feedback_value::numeric) FILTER (WHERE feedback_type='retry_count'), 2) AS avg_retry,
                    ROUND(AVG(feedback_value::numeric) FILTER (WHERE feedback_type='files_changed_count'), 1) AS avg_files
                FROM feedback_events
                WHERE source_type='execution_run' AND repo_slug=%s
                """,
                (repo_slug,),
            )
            exec_row = cur.fetchone()
            completed, failed, avg_retry, avg_files = exec_row if exec_row else (0, 0, None, None)

            cur.execute(
                """
                SELECT feedback_value, COUNT(*) AS cnt
                FROM feedback_events
                WHERE source_type='execution_run' AND repo_slug=%s AND feedback_type='failure_category'
                GROUP BY feedback_value ORDER BY cnt DESC
                """,
                (repo_slug,),
            )
            exec_categories = cur.fetchall()

            # --- Planning stats (all project_keys mapped to this repo) ---
            plan_row = None
            if project_keys:
                # Match epic_key prefixes for all project keys: 'KAN-%', 'SANDBOX-%', etc.
                like_patterns = [f"{pk}-%" for pk in project_keys]
                cur.execute(
                    """
                    SELECT
                        COUNT(DISTINCT source_run_id) FILTER (WHERE feedback_type='planning_approved')    AS approved,
                        COUNT(DISTINCT source_run_id) FILTER (WHERE feedback_type='planning_rejected')    AS rejected,
                        COUNT(DISTINCT source_run_id) FILTER (WHERE feedback_type='planning_regenerated') AS regenerated,
                        ROUND(AVG(feedback_value::numeric) FILTER (WHERE feedback_type='stories_proposed_count'), 1) AS avg_proposed,
                        ROUND(AVG(feedback_value::numeric) FILTER (WHERE feedback_type='stories_created_count'), 1) AS avg_created,
                        ROUND(AVG(feedback_value::numeric) FILTER (WHERE feedback_type='approval_latency_seconds'), 0) AS avg_latency
                    FROM feedback_events
                    WHERE source_type='planning_run'
                      AND epic_key LIKE ANY(%s)
                    """,
                    (like_patterns,),
                )
                plan_row = cur.fetchone()

            # --- Build execution guidance ---
            total_exec = (completed or 0) + (failed or 0)
            exec_bullets = []
            exec_evidence: dict = {"repo_slug": repo_slug, "total_runs": total_exec}

            if total_exec > 0:
                pct = round(100 * (completed or 0) / total_exec)
                exec_bullets.append(
                    f"{completed or 0} of {total_exec} execution run(s) completed ({pct}%)"
                )
                exec_evidence.update({
                    "completed": int(completed or 0),
                    "failed": int(failed or 0),
                })
            if avg_retry is not None:
                exec_bullets.append(f"Average retry count: {avg_retry}")
                exec_evidence["avg_retry_count"] = float(avg_retry)
            if avg_files is not None:
                exec_bullets.append(f"Average files changed per run: {avg_files}")
                exec_evidence["avg_files_changed"] = float(avg_files)
            if exec_categories:
                top_cat, top_cnt = exec_categories[0]
                exec_bullets.append(f"Most common failure: {top_cat} ({top_cnt} run(s))")
                exec_evidence["failure_categories"] = {
                    cat: int(cnt) for cat, cnt in exec_categories
                }

            exec_summary = (
                "\n".join(f"- {b}" for b in exec_bullets)
                if exec_bullets else "No execution runs recorded yet."
            )

            # --- Build planning guidance ---
            plan_summary = "No planning runs recorded yet."
            plan_evidence: dict = {"repo_slug": repo_slug}

            if plan_row and project_keys:
                approved, rejected, regenerated, avg_proposed, avg_created, avg_latency = plan_row
                total_plan = (approved or 0) + (rejected or 0) + (regenerated or 0)
                plan_bullets = []

                if total_plan > 0:
                    plan_bullets.append(
                        f"{total_plan} planning run(s): {approved or 0} approved, "
                        f"{rejected or 0} rejected, {regenerated or 0} regenerated"
                    )
                    plan_evidence.update({
                        "total_planning_runs": total_plan,
                        "approved": int(approved or 0),
                        "rejected": int(rejected or 0),
                        "regenerated": int(regenerated or 0),
                    })
                if avg_proposed is not None:
                    plan_bullets.append(f"Average stories proposed per Epic: {avg_proposed}")
                    plan_evidence["avg_stories_proposed"] = float(avg_proposed)
                if avg_created is not None:
                    plan_bullets.append(f"Average stories created per Epic: {avg_created}")
                    plan_evidence["avg_stories_created"] = float(avg_created)
                if avg_latency is not None:
                    latency_i = int(float(avg_latency))
                    plan_bullets.append(f"Average approval latency: {latency_i}s")
                    plan_evidence["avg_approval_latency_seconds"] = latency_i

                if plan_bullets:
                    plan_summary = "\n".join(f"- {b}" for b in plan_bullets)

            # --- Upsert both snapshots ---
            result: dict = {}
            new_snapshots = []

            for memory_kind, summary, evidence in [
                (MemoryKind.EXECUTION_GUIDANCE, exec_summary, exec_evidence),
                (MemoryKind.PLANNING_GUIDANCE,  plan_summary,  plan_evidence),
            ]:
                cur.execute(
                    """
                    INSERT INTO memory_snapshots
                        (scope_type, scope_key, memory_kind, summary, evidence_json)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (scope_type, scope_key, memory_kind) DO UPDATE
                        SET summary       = EXCLUDED.summary,
                            evidence_json = EXCLUDED.evidence_json,
                            updated_at    = NOW()
                    RETURNING id, (xmax = 0) AS is_insert
                    """,
                    (MemoryScope.REPO, repo_slug, memory_kind, summary, json.dumps(evidence)),
                )
                snap_id, is_insert = cur.fetchone()
                result[memory_kind] = {"id": snap_id, "is_new": bool(is_insert)}
                if is_insert:
                    new_snapshots.append(memory_kind)

    logger.info(
        "generate_repo_memory_snapshot: upserted for %s — exec_id=%s plan_id=%s",
        repo_slug,
        result.get(MemoryKind.EXECUTION_GUIDANCE, {}).get("id"),
        result.get(MemoryKind.PLANNING_GUIDANCE, {}).get("id"),
    )
    if new_snapshots:
        send_message(
            "memory_snapshot_updated", "UPDATED",
            f"{repo_slug}: new memory snapshot(s) created — {', '.join(new_snapshots)}",
        )
    return result


def record_execution_feedback(run_id: int) -> int:
    """Write feedback_events rows for a finished story_implementation run.

    Auto-resolves repo_slug from repo_mappings using the run's issue_key.
    Should be called from the worker after the run reaches its final status
    (COMPLETED or FAILED). Returns the number of events written.
    """
    from app.feedback import FeedbackSource, FeedbackType, categorize_execution_failure

    repo_slug_out = None   # captured for on_write repo snapshot refresh
    issue_key_out = None   # captured for on_write epic rollup

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT issue_key, status, test_status, retry_count,
                       merge_status, files_changed_count, error_detail, current_step
                FROM workflow_runs WHERE id = %s
                """,
                (run_id,),
            )
            row = cur.fetchone()
            if not row:
                return 0
            issue_key, status, test_status, retry_count, merge_status, \
                files_changed_count, error_detail, current_step = row
            issue_key_out = issue_key

            # Resolve repo_slug from active mapping for this project
            repo_slug = None
            repo_slug_out = None
            if issue_key:
                project_key = issue_key.split("-")[0]
                cur.execute(
                    """
                    SELECT repo_slug FROM repo_mappings
                    WHERE jira_project_key = %s AND is_active = TRUE
                    ORDER BY (issue_type = 'Story') DESC NULLS LAST
                    LIMIT 1
                    """,
                    (project_key,),
                )
                rs = cur.fetchone()
                if rs:
                    repo_slug = rs[0]
                    repo_slug_out = repo_slug

            events = []

            def _ev(ftype, fvalue, details=None):
                events.append((
                    FeedbackSource.EXECUTION_RUN, run_id,
                    None, issue_key, repo_slug,
                    ftype, str(fvalue) if fvalue is not None else None,
                    json.dumps(details) if details else None,
                ))

            if status == "COMPLETED":
                _ev(FeedbackType.EXECUTION_COMPLETED, "true")
            else:
                _ev(FeedbackType.EXECUTION_FAILED, "true")
                category = categorize_execution_failure(
                    test_status, merge_status, error_detail, current_step,
                )
                _ev(FeedbackType.FAILURE_CATEGORY, category)

            if test_status:
                _ev(FeedbackType.TEST_STATUS, test_status)
            if retry_count is not None:
                _ev(FeedbackType.RETRY_COUNT, retry_count)
            if merge_status:
                _ev(FeedbackType.MERGE_STATUS, merge_status)
            if files_changed_count is not None:
                _ev(FeedbackType.FILES_CHANGED_COUNT, files_changed_count)

            if not events:
                return 0

            cur.executemany(
                """
                INSERT INTO feedback_events
                    (source_type, source_run_id, epic_key, story_key, repo_slug,
                     feedback_type, feedback_value, details_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                events,
            )
    n = len(events)
    if n > 0:
        if repo_slug_out:
            try:
                generate_repo_memory_snapshot(repo_slug_out)
            except Exception as exc:
                logger.warning("record_execution_feedback: repo snapshot refresh failed — %s", exc)
        if issue_key_out:
            try:
                # Look up parent Epic via planning_outputs and refresh Epic rollup
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT DISTINCT parent_issue_key
                            FROM planning_outputs
                            WHERE created_issue_key = %s
                              AND parent_issue_key IS NOT NULL
                            LIMIT 1
                            """,
                            (issue_key_out,),
                        )
                        epic_row = cur.fetchone()
                if epic_row:
                    generate_epic_outcome_rollup(epic_row[0])
            except Exception as exc:
                logger.warning("record_execution_feedback: epic rollup refresh failed — %s", exc)
    return n


def record_planning_feedback(run_id: int) -> int:
    """Write feedback_events rows for a finished planning run.

    Reads the run's final state and planning_outputs to produce structured
    signals. Should be called after every terminal state transition
    (COMPLETED, REJECTED, REGENERATE_REQUESTED).
    Returns the number of events written.
    """
    from app.feedback import FeedbackSource, FeedbackType, FailureCategory, categorize_planning_failure

    repo_slug_out = None  # captured for on_write snapshot refresh

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT issue_key, approval_status,
                       approval_requested_at, approval_received_at,
                       error_detail, status, current_step
                FROM workflow_runs WHERE id = %s
                """,
                (run_id,),
            )
            row = cur.fetchone()
            if not row:
                return 0
            issue_key, approval_status, req_at, recv_at, error_detail, run_status, current_step = row

            # Resolve repo_slug for on_write snapshot refresh
            if issue_key:
                project_key = issue_key.split("-")[0]
                cur.execute(
                    """
                    SELECT repo_slug FROM repo_mappings
                    WHERE jira_project_key = %s AND is_active = TRUE LIMIT 1
                    """,
                    (project_key,),
                )
                rs = cur.fetchone()
                if rs:
                    repo_slug_out = rs[0]

            cur.execute(
                "SELECT COUNT(*) FROM planning_outputs WHERE run_id = %s",
                (run_id,),
            )
            proposed_count = cur.fetchone()[0]

            cur.execute(
                "SELECT COUNT(*) FROM planning_outputs WHERE run_id = %s AND status = 'CREATED'",
                (run_id,),
            )
            created_count = cur.fetchone()[0]

            events = []

            def _ev(ftype, fvalue, details=None):
                events.append((
                    FeedbackSource.PLANNING_RUN, run_id,
                    issue_key, None, None,
                    ftype, str(fvalue) if fvalue is not None else None,
                    json.dumps(details) if details else None,
                ))

            if proposed_count:
                _ev(FeedbackType.STORIES_PROPOSED_COUNT, proposed_count)

            if approval_status == "APPROVED":
                _ev(FeedbackType.PLANNING_APPROVED, "true")
                _ev(FeedbackType.STORIES_CREATED_COUNT, created_count)
                if req_at and recv_at:
                    latency = int((recv_at - req_at).total_seconds())
                    _ev(FeedbackType.APPROVAL_LATENCY_SECONDS, latency)

            elif approval_status == "REJECTED":
                _ev(FeedbackType.PLANNING_REJECTED, "true",
                    {"reason": error_detail} if error_detail else None)
                _ev(FeedbackType.FAILURE_CATEGORY, FailureCategory.APPROVAL_REJECTED)

            elif approval_status == "REGENERATE_REQUESTED":
                _ev(FeedbackType.PLANNING_REGENERATED, "true",
                    {"reason": error_detail} if error_detail else None)
                _ev(FeedbackType.FAILURE_CATEGORY, FailureCategory.APPROVAL_REGENERATED)

            # Catch FAILED runs not covered by the approval_status branches above:
            # duplicate_blocked, jira_creation_failure, worker_interrupted, etc.
            elif run_status == "FAILED":
                category = categorize_planning_failure(error_detail, current_step)
                _ev(FeedbackType.FAILURE_CATEGORY, category)

            if not events:
                return 0

            cur.executemany(
                """
                INSERT INTO feedback_events
                    (source_type, source_run_id, epic_key, story_key, repo_slug,
                     feedback_type, feedback_value, details_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                events,
            )
    n = len(events)
    if n > 0 and repo_slug_out:
        try:
            generate_repo_memory_snapshot(repo_slug_out)
        except Exception as exc:
            logger.warning("record_planning_feedback: snapshot refresh failed — %s", exc)
    return n


def store_planning_metadata(run_id: int, assumptions: list, open_questions: list):
    """Persist assumptions and open_questions lists from a Claude planning response."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE workflow_runs
                SET assumptions_json=%s, open_questions_json=%s, updated_at=NOW()
                WHERE id=%s
                """,
                (json.dumps(assumptions), json.dumps(open_questions), run_id),
            )


def list_planning_runs(limit: int = 10) -> list[dict]:
    """Return recent planning runs, newest first."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, issue_key, workflow_type, status, approval_status,
                       current_step, created_jira_children_count,
                       error_detail, created_at, completed_at,
                       approval_requested_at, approval_received_at
                FROM workflow_runs
                WHERE workflow_type IN ('epic_breakdown', 'feature_breakdown')
                ORDER BY id DESC
                LIMIT %s
                """,
                (min(limit, 50),),
            )
            cols = [
                "id", "issue_key", "workflow_type", "status", "approval_status",
                "current_step", "created_jira_children_count",
                "error_detail", "created_at", "completed_at",
                "approval_requested_at", "approval_received_at",
            ]
            return [
                {c: (v.isoformat() if hasattr(v, "isoformat") else v) for c, v in zip(cols, row)}
                for row in cur.fetchall()
            ]


def get_planning_run_detail(run_id: int) -> dict | None:
    """Return full detail for a planning run including all proposed/created items."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, issue_key, workflow_type, status, approval_status,
                       current_step, created_jira_children_count, error_detail,
                       created_at, completed_at,
                       approval_requested_at, approval_received_at,
                       assumptions_json, open_questions_json
                FROM workflow_runs
                WHERE id = %s
                  AND workflow_type IN ('epic_breakdown', 'feature_breakdown')
                """,
                (run_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [
                "id", "issue_key", "workflow_type", "status", "approval_status",
                "current_step", "created_jira_children_count", "error_detail",
                "created_at", "completed_at",
                "approval_requested_at", "approval_received_at",
                "assumptions_json", "open_questions_json",
            ]
            result = {c: (v.isoformat() if hasattr(v, "isoformat") else v) for c, v in zip(cols, row)}
            result["assumptions"] = json.loads(result.pop("assumptions_json") or "[]")
            result["open_questions"] = json.loads(result.pop("open_questions_json") or "[]")

            cur.execute(
                """
                SELECT sequence_number, title, status, created_issue_key,
                       confidence, description, acceptance_criteria,
                       rationale, dependency_notes, risk_notes
                FROM planning_outputs
                WHERE run_id = %s
                ORDER BY sequence_number
                """,
                (run_id,),
            )
            item_cols = [
                "sequence_number", "title", "status", "created_issue_key",
                "confidence", "description", "acceptance_criteria",
                "rationale", "dependency_notes", "risk_notes",
            ]
            result["items"] = [dict(zip(item_cols, r)) for r in cur.fetchall()]
    return result


def get_created_children_for_epic(issue_key: str, exclude_run_id: int) -> dict | None:
    """Return info about an existing completed breakdown for this epic, or None.

    Looks for any planning_outputs row with status='CREATED' belonging to a run for
    issue_key that is not exclude_run_id. Used to block accidental duplicate breakdowns.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT wr.id, COUNT(po.id) AS created_count
                FROM planning_outputs po
                JOIN workflow_runs wr ON wr.id = po.run_id
                WHERE po.parent_issue_key = %s
                  AND po.status = 'CREATED'
                  AND wr.id != %s
                GROUP BY wr.id
                ORDER BY wr.id DESC
                LIMIT 1
                """,
                (issue_key, exclude_run_id),
            )
            row = cur.fetchone()
    if not row:
        return None
    return {"run_id": row[0], "count": row[1]}


def get_planning_run_for_regeneration(run_id: int) -> dict | None:
    """Return a planning run for REGENERATE, accepting both pending and completed runs.

    Matches:
      - WAITING_FOR_APPROVAL + approval_status=PENDING  (normal regenerate mid-approval)
      - COMPLETED + approval_status=APPROVED            (regenerate after children already created)

    Returns the same shape as get_pending_planning_run.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT wr.id, wr.issue_key, wr.workflow_type, wr.related_event_id,
                       wr.parent_issue_key, we.payload_json
                FROM workflow_runs wr
                LEFT JOIN workflow_events we ON we.id = wr.related_event_id
                WHERE wr.id = %s
                  AND (
                    (wr.status = 'WAITING_FOR_APPROVAL' AND wr.approval_status = 'PENDING')
                    OR
                    (wr.status = 'COMPLETED' AND wr.approval_status = 'APPROVED')
                  )
                """,
                (run_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    cols = ["id", "issue_key", "workflow_type", "related_event_id", "parent_issue_key", "payload_json"]
    result = dict(zip(cols, row))
    try:
        payload = json.loads(result.pop("payload_json") or "{}")
        result["summary"] = payload.get("issue", {}).get("fields", {}).get("summary", "")
    except Exception:
        result["summary"] = ""
    return result


def get_planning_memory(repo_slug: str, epic_key: str | None = None) -> str:
    """Retrieve repo-level and optional epic-level memory formatted for prompt injection.

    Combines:
      - repo planning_guidance  (approval patterns, avg story counts)
      - repo execution_guidance (completion rates, failure categories)
      - epic execution_guidance (if epic_key provided and a snapshot exists)

    Returns a bullet-point block bounded to MEMORY_MAX_BULLETS and
    MEMORY_MAX_CHARS. Returns empty string when no useful snapshots exist.
    """
    from app.feedback import MEMORY_MAX_BULLETS, MEMORY_MAX_CHARS

    _SKIP = frozenset(["no planning runs recorded yet.", "no execution runs recorded yet."])
    bullets: list[str] = []

    with get_conn() as conn:
        with conn.cursor() as cur:
            queries: list[tuple[str, str, str]] = [
                ("repo", repo_slug, "manual_note"),       # human-authored first — never dropped by cap
                ("repo", repo_slug, "planning_guidance"),
                ("repo", repo_slug, "execution_guidance"),
            ]
            if epic_key:
                queries.append(("epic", epic_key, "manual_note"))
                queries.append(("epic", epic_key, "execution_guidance"))

            for scope_type, scope_key, memory_kind in queries:
                cur.execute(
                    """
                    SELECT summary FROM memory_snapshots
                    WHERE scope_type = %s AND scope_key = %s AND memory_kind = %s
                    """,
                    (scope_type, scope_key, memory_kind),
                )
                row = cur.fetchone()
                if not row or not row[0]:
                    continue
                text = row[0].strip()
                if text.lower() in _SKIP:
                    continue
                for line in text.splitlines():
                    line = line.strip().lstrip("- ").strip()
                    if line:
                        bullets.append(line)

    if not bullets:
        return ""

    bullets = bullets[:MEMORY_MAX_BULLETS]
    block = "\n".join(f"- {b}" for b in bullets)
    if len(block) > MEMORY_MAX_CHARS:
        block = block[:MEMORY_MAX_CHARS].rsplit("\n", 1)[0]

    return block


def get_execution_memory(repo_slug: str) -> str:
    """Retrieve repo-level execution guidance formatted for prompt injection.

    Fetches only the execution_guidance snapshot for this repo (not planning).
    Returns a bullet-point block bounded to MEMORY_MAX_BULLETS and
    MEMORY_MAX_CHARS. Returns empty string when no snapshot exists yet.
    """
    from app.feedback import MEMORY_MAX_BULLETS, MEMORY_MAX_CHARS

    _SKIP = frozenset(["no execution runs recorded yet."])
    bullets: list[str] = []

    with get_conn() as conn:
        with conn.cursor() as cur:
            for memory_kind in ("manual_note", "execution_guidance"):
                cur.execute(
                    """
                    SELECT summary FROM memory_snapshots
                    WHERE scope_type = 'repo' AND scope_key = %s AND memory_kind = %s
                    """,
                    (repo_slug, memory_kind),
                )
                row = cur.fetchone()
                if not row or not row[0]:
                    continue
                text = row[0].strip()
                if text.lower() in _SKIP:
                    continue
                for line in text.splitlines():
                    line = line.strip().lstrip("- ").strip()
                    if line:
                        bullets.append(line)

    if not bullets:
        return ""

    bullets = bullets[:MEMORY_MAX_BULLETS]
    block = "\n".join(f"- {b}" for b in bullets)
    if len(block) > MEMORY_MAX_CHARS:
        block = block[:MEMORY_MAX_CHARS].rsplit("\n", 1)[0]

    return block


def add_manual_memory(scope_type: str, scope_key: str, content: str) -> dict:
    """Upsert a human-authored memory note for the given scope.

    Uses memory_kind='manual_note' and source='human' so it is distinguishable
    from derived (auto-generated) snapshots. The content is stored verbatim as
    the summary; evidence_json is null for manual notes.

    Returns the snapshot row dict. Sends a Telegram notification on first creation.
    """
    from app.feedback import MemoryKind
    from app.telegram import send_message

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO memory_snapshots
                    (scope_type, scope_key, memory_kind, summary, evidence_json, source)
                VALUES (%s, %s, %s, %s, NULL, 'human')
                ON CONFLICT (scope_type, scope_key, memory_kind) DO UPDATE
                    SET summary    = EXCLUDED.summary,
                        source     = 'human',
                        updated_at = NOW()
                RETURNING id, scope_type, scope_key, memory_kind, summary,
                          source, created_at, updated_at,
                          (xmax = 0) AS is_insert
                """,
                (scope_type, scope_key, MemoryKind.MANUAL_NOTE, content),
            )
            row = cur.fetchone()

    snap_id, scope_type_out, scope_key_out, memory_kind_out, summary, \
        source, created_at, updated_at, is_insert = row

    if is_insert:
        send_message(
            "manual_memory_added", "ADDED",
            f"{scope_type_out}/{scope_key_out}: manual note stored",
        )

    logger.info(
        "add_manual_memory: %s for %s/%s (id=%s, new=%s)",
        MemoryKind.MANUAL_NOTE, scope_type_out, scope_key_out, snap_id, bool(is_insert),
    )

    return {
        "id":          snap_id,
        "scope_type":  scope_type_out,
        "scope_key":   scope_key_out,
        "memory_kind": memory_kind_out,
        "source":      source,
        "summary":     summary,
        "created_at":  created_at.isoformat(),
        "updated_at":  updated_at.isoformat(),
    }
