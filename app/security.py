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


async def admin_key_middleware(request: Request, call_next):
    """Middleware: enforce admin key on /debug/* and /admin/* paths."""
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
