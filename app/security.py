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
_PUBLIC_PATHS = {"/healthz", "/webhooks/jira", "/webhooks/telegram"}


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
        # Key not configured — warn but allow (dev mode)
        logger.warning("ADMIN_API_KEY not set — admin endpoints are unprotected")
        return True
    provided = request.headers.get(ADMIN_KEY_HEADER, "")
    return provided == expected


async def admin_key_middleware(request: Request, call_next):
    """Middleware: enforce admin key on /debug/* and /admin/* paths."""
    path = request.url.path
    if is_admin_protected(path):
        if not check_admin_key(request):
            logger.warning(
                "Admin auth failed: %s %s (client=%s)",
                request.method, path, request.client.host if request.client else "unknown",
            )
            return JSONResponse(
                {"detail": "Unauthorized — X-Orchestrator-Admin-Key required"},
                status_code=403,
            )
    return await call_next(request)
