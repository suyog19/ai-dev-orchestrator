import logging
from app.database import get_conn

logger = logging.getLogger("orchestrator")


def _row_to_dict(row) -> dict:
    return {
        "id": row[0],
        "jira_project_key": row[1],
        "issue_type": row[2],
        "repo_slug": row[3],
        "base_branch": row[4],
        "is_active": row[5],
        "notes": row[6],
        "created_at": row[7].isoformat() if row[7] else None,
        "updated_at": row[8].isoformat() if row[8] else None,
    }


_SELECT = """
    SELECT id, jira_project_key, issue_type, repo_slug, base_branch,
           is_active, notes, created_at, updated_at
    FROM repo_mappings
"""


def get_mapping(jira_project_key: str, issue_type: str) -> dict | None:
    """Return the most specific active mapping for a project key + issue type.

    Prefers exact issue_type match over catch-all (NULL issue_type).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                _SELECT + """
                WHERE jira_project_key = %s
                  AND (issue_type = %s OR issue_type IS NULL)
                  AND is_active = TRUE
                ORDER BY issue_type NULLS LAST
                LIMIT 1
                """,
                (jira_project_key, issue_type),
            )
            row = cur.fetchone()
    if not row:
        logger.warning("No active mapping for project=%s issue_type=%s", jira_project_key, issue_type)
        return None
    return _row_to_dict(row)


def get_mapping_by_id(mapping_id: int) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT + "WHERE id = %s", (mapping_id,))
            row = cur.fetchone()
    return _row_to_dict(row) if row else None


def get_all_mappings() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT + "ORDER BY id")
            rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows]


def add_mapping(
    jira_project_key: str,
    repo_slug: str,
    base_branch: str = "main",
    issue_type: str | None = None,
    notes: str | None = None,
) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO repo_mappings
                    (jira_project_key, issue_type, repo_slug, base_branch, notes)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (jira_project_key, issue_type, repo_slug, base_branch, notes),
            )
            new_id = cur.fetchone()[0]
    return get_mapping_by_id(new_id)


def update_mapping(mapping_id: int, **fields) -> dict | None:
    allowed = {"jira_project_key", "issue_type", "repo_slug", "base_branch", "is_active", "notes"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return get_mapping_by_id(mapping_id)
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    values = list(updates.values()) + [mapping_id]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE repo_mappings SET {set_clause}, updated_at=NOW() WHERE id=%s RETURNING id",
                values,
            )
            if not cur.fetchone():
                return None
    return get_mapping_by_id(mapping_id)


def disable_mapping(mapping_id: int) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE repo_mappings SET is_active=FALSE, updated_at=NOW() WHERE id=%s RETURNING id",
                (mapping_id,),
            )
            return cur.fetchone() is not None


def upsert_seed_mappings(mappings: list[dict]) -> int:
    """Insert seed mappings that do not already exist as active entries.

    Matches on (jira_project_key, issue_type, repo_slug). Skips rows where
    an active match already exists. Returns the count of rows inserted.
    """
    inserted = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for m in mappings:
                cur.execute(
                    """
                    SELECT id FROM repo_mappings
                    WHERE jira_project_key = %s
                      AND (issue_type = %s OR (issue_type IS NULL AND %s IS NULL))
                      AND repo_slug = %s
                      AND is_active = TRUE
                    LIMIT 1
                    """,
                    (m["jira_project_key"], m.get("issue_type"), m.get("issue_type"), m["repo_slug"]),
                )
                if cur.fetchone():
                    continue
                cur.execute(
                    """
                    INSERT INTO repo_mappings
                        (jira_project_key, issue_type, repo_slug, base_branch, notes)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        m["jira_project_key"],
                        m.get("issue_type"),
                        m["repo_slug"],
                        m.get("base_branch", "main"),
                        m.get("notes"),
                    ),
                )
                inserted += 1
    if inserted:
        logger.info("Seed mappings: inserted %d new mapping(s)", inserted)
    else:
        logger.info("Seed mappings: all entries already present — nothing inserted")
    return inserted
