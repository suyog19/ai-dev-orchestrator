"""
Phase 14 — Admin UI authentication helpers.

Cookie-based auth using itsdangerous signed tokens.
The admin key is the shared secret; no separate password is needed.
"""
import hashlib
import logging
import os

from fastapi import Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

logger = logging.getLogger("orchestrator.ui_auth")

COOKIE_NAME = "orchestrator_admin_session"
SESSION_MAX_AGE = 8 * 3600  # 8 hours


def _signer() -> URLSafeTimedSerializer:
    secret = os.environ.get("ADMIN_API_KEY", "default-unsafe-secret")
    # Derive a signing key from the admin key so the raw key is never stored
    signing_key = hashlib.sha256(f"ui-session:{secret}".encode()).hexdigest()
    return URLSafeTimedSerializer(signing_key, salt="admin-ui-session")


def create_session_token() -> str:
    """Return a signed, time-stamped session token."""
    return _signer().dumps("admin")


def verify_session_token(token: str) -> bool:
    """Return True if the token is valid and not expired."""
    try:
        _signer().loads(token, max_age=SESSION_MAX_AGE)
        return True
    except (SignatureExpired, BadSignature):
        return False


def check_admin_key(provided: str) -> bool:
    """Return True if the provided key matches ADMIN_API_KEY."""
    expected = os.environ.get("ADMIN_API_KEY", "")
    if not expected:
        logger.warning("ADMIN_API_KEY not set — admin UI is unprotected")
        return True
    return provided == expected


def csrf_token(session_token: str) -> str:
    """Derive a CSRF token bound to this session token."""
    return hashlib.sha256(f"csrf:{session_token}".encode()).hexdigest()[:32]


def verify_csrf(session_token: str, submitted: str) -> bool:
    return submitted == csrf_token(session_token)


def get_session_token(request: Request) -> str | None:
    """Extract the session token from the request cookie."""
    return request.cookies.get(COOKIE_NAME)


def require_admin_ui(request: Request) -> str:
    """Return the session token if valid; raise redirect to login otherwise.

    Call at the top of each UI route handler that requires auth:
        token = require_admin_ui(request)
    The token is needed for CSRF generation in templates.
    """
    token = get_session_token(request)
    if not token or not verify_session_token(token):
        raise _LoginRedirect(str(request.url))
    return token


class _LoginRedirect(Exception):
    def __init__(self, next_url: str):
        self.next_url = next_url


def redirect_to_login(next_url: str = "/admin/ui") -> RedirectResponse:
    return RedirectResponse(url=f"/admin/ui/login?next={next_url}", status_code=302)
