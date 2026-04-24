"""
Security helpers — admin key authentication, control flags, GitHub write guard.
"""
import logging
import os

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("orchestrator.security")

# ---------------------------------------------------------------------------
# Admin key auth
# ---------------------------------------------------------------------------

ADMIN_KEY_HEADER = "X-Orchestrator-Admin-Key"
_PROTECTED_PREFIXES = ("/debug/", "/admin/")

# Methods that mutate state — auth success for these is logged
_MUTATING_METHODS = {"POST", "PUT", "DELETE", "PATCH"}


def _get_admin_key() -> str:
    return os.environ.get("ADMIN_API_KEY", "")


def is_admin_protected(path: str) -> bool:
    """Return True if the path requires admin key auth."""
    for prefix in _PROTECTED_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


def check_admin_key(request: Request) -> bool:
    """Return True if the request carries a valid admin key."""
    expected = _get_admin_key()
    if not expected:
        logger.warning("ADMIN_API_KEY not set — admin endpoints are unprotected")
        return True
    provided = request.headers.get(ADMIN_KEY_HEADER, "")
    return provided == expected


def _actor(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# GitHub write guard
# ---------------------------------------------------------------------------

_GITHUB_WRITE_ACTIONS = {"push", "create_pr", "post_comment", "add_label", "merge_pr"}


def ensure_github_writes_allowed(action: str, repo_slug: str = "", run_id: int | None = None) -> None:
    """Raise RuntimeError if GitHub writes are globally disabled or the orchestrator is paused.

    Call this before any GitHub write action (push, PR creation, merge, etc.).
    Raises RuntimeError so the workflow's existing try/except catches it cleanly.
    """
    import os as _os
    # Check pause state first (cheapest — env var, no DB call)
    paused = _os.environ.get("ORCHESTRATOR_PAUSED", "false").lower() == "true"
    if not paused:
        try:
            from app.database import is_paused
            paused = is_paused()
        except Exception:
            pass
    if paused:
        _log_github_block(action, repo_slug, run_id, "orchestrator_paused")
        raise RuntimeError(f"GitHub write blocked — orchestrator is paused (action={action})")

    # Check ALLOW_GITHUB_WRITES flag
    if _os.environ.get("ALLOW_GITHUB_WRITES", "true").lower() != "true":
        _log_github_block(action, repo_slug, run_id, "allow_github_writes=false")
        raise RuntimeError(f"GitHub writes disabled (action={action})")

    # For auto-merge, also check ALLOW_AUTO_MERGE
    if action == "merge_pr" and _os.environ.get("ALLOW_AUTO_MERGE", "true").lower() != "true":
        _log_github_block(action, repo_slug, run_id, "allow_auto_merge=false")
        raise RuntimeError("Auto-merge disabled by ALLOW_AUTO_MERGE=false")


def _log_github_block(action: str, repo_slug: str, run_id: int | None, reason: str) -> None:
    logger.warning("GitHub write blocked: action=%s repo=%s run_id=%s reason=%s", action, repo_slug, run_id, reason)
    try:
        from app.database import record_security_event
        record_security_event(
            event_type="github_write_blocked",
            source="workflow",
            actor=repo_slug or "unknown",
            endpoint=f"github/{action}",
            method="WRITE",
            status="BLOCKED",
            details={"action": action, "repo_slug": repo_slug, "run_id": run_id, "reason": reason},
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Rate limiting (Redis-backed sliding window)
# ---------------------------------------------------------------------------

# Default limits: (max_requests, window_seconds)
_RATE_LIMITS: dict[str, tuple[int, int]] = {
    "/webhooks/jira":     (30, 60),   # 30 per minute
    "/webhooks/telegram": (10, 60),   # 10 per minute per chat_id
    "_admin_mutating":    (20, 60),   # 20 per minute for admin mutating endpoints
}


def _rate_limit_key(prefix: str, identifier: str, window_seconds: int) -> str:
    import time
    window = int(time.time()) // window_seconds
    return f"rl:{prefix}:{identifier}:{window}"


def check_rate_limit(path: str, identifier: str) -> bool:
    """Return True if the request is within the rate limit, False if exceeded."""
    try:
        from app.queue import get_redis
        redis = get_redis()

        if path == "/webhooks/jira":
            max_req, window = _RATE_LIMITS["/webhooks/jira"]
            key = _rate_limit_key("jira", "global", window)
        elif path == "/webhooks/telegram":
            max_req, window = _RATE_LIMITS["/webhooks/telegram"]
            key = _rate_limit_key("telegram", identifier, window)
        elif is_admin_protected(path):
            max_req, window = _RATE_LIMITS["_admin_mutating"]
            key = _rate_limit_key("admin", identifier, window)
        else:
            return True  # no limit

        count = redis.incr(key)
        if count == 1:
            redis.expire(key, window)
        return int(count) <= max_req
    except Exception:
        return True  # fail open on Redis error


async def admin_key_middleware(request: Request, call_next):
    """Middleware: enforce admin key on /debug/* and /admin/* paths; rate-limit admin mutating calls."""
    path = request.url.path
    method = request.method

    if is_admin_protected(path):
        if not check_admin_key(request):
            actor = _actor(request)
            logger.warning("Admin auth failed: %s %s (client=%s)", method, path, actor)
            try:
                from app.database import record_security_event
                record_security_event(
                    event_type="admin_auth_failed",
                    source="http",
                    actor=actor,
                    endpoint=path,
                    method=method,
                    status="REJECTED",
                )
            except Exception:
                pass  # never let security logging crash the request
            return JSONResponse(
                {"detail": "Unauthorized — X-Orchestrator-Admin-Key required"},
                status_code=403,
            )

        # Rate-limit admin mutating calls
        if method in _MUTATING_METHODS:
            actor = _actor(request)
            if not check_rate_limit(path, actor):
                logger.warning("Admin rate limit exceeded: %s %s (client=%s)", method, path, actor)
                return JSONResponse(
                    {"detail": "Rate limit exceeded — too many admin requests"},
                    status_code=429,
                )

        # Auth succeeded — log mutating calls for audit trail
        if method in _MUTATING_METHODS:
            actor = _actor(request)
            try:
                from app.database import record_security_event
                record_security_event(
                    event_type="admin_auth_success",
                    source="http",
                    actor=actor,
                    endpoint=path,
                    method=method,
                    status="ALLOWED",
                )
            except Exception:
                pass

    return await call_next(request)
