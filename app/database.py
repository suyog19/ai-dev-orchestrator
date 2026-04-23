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


def record_planning_feedback(run_id: int) -> int:
    """Write feedback_events rows for a finished planning run.

    Reads the run's final state and planning_outputs to produce structured
    signals. Should be called after every terminal state transition
    (COMPLETED, REJECTED, REGENERATE_REQUESTED).
    Returns the number of events written.
    """
    from app.feedback import FeedbackSource, FeedbackType, FailureCategory

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT issue_key, approval_status,
                       approval_requested_at, approval_received_at,
                       error_detail
                FROM workflow_runs WHERE id = %s
                """,
                (run_id,),
            )
            row = cur.fetchone()
            if not row:
                return 0
            issue_key, approval_status, req_at, recv_at, error_detail = row

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
    return len(events)


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
