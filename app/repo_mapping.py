import logging
from app.database import get_conn

logger = logging.getLogger("orchestrator")


def get_mapping(issue_key: str) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT issue_key, repo_name, target_branch FROM repo_mappings WHERE issue_key = %s",
                (issue_key,),
            )
            row = cur.fetchone()
    if not row:
        logger.warning("No repo mapping found for issue_key: %s", issue_key)
        return None
    return {"issue_key": row[0], "repo_name": row[1], "target_branch": row[2]}


def get_all_mappings() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT issue_key, repo_name, target_branch FROM repo_mappings ORDER BY id")
            rows = cur.fetchall()
    return [{"issue_key": r[0], "repo_name": r[1], "target_branch": r[2]} for r in rows]


def add_mapping(issue_key: str, repo_name: str, target_branch: str = "main") -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO repo_mappings (issue_key, repo_name, target_branch)
                VALUES (%s, %s, %s)
                ON CONFLICT (issue_key) DO UPDATE
                  SET repo_name = EXCLUDED.repo_name,
                      target_branch = EXCLUDED.target_branch
                RETURNING issue_key, repo_name, target_branch
                """,
                (issue_key, repo_name, target_branch),
            )
            row = cur.fetchone()
    return {"issue_key": row[0], "repo_name": row[1], "target_branch": row[2]}
