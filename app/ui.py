"""
Phase 14 — Admin UI routes.

Cookie-based auth; all routes require require_admin_ui() except login/logout.
"""
import os
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.ui_auth import (
    COOKIE_NAME,
    SESSION_MAX_AGE,
    check_admin_key,
    create_session_token,
    csrf_token,
    require_admin_ui,
    redirect_to_login,
    verify_csrf,
    _LoginRedirect,
)

logger = logging.getLogger("orchestrator.ui")

router = APIRouter(prefix="/admin/ui")
templates = Jinja2Templates(directory="app/templates")


def _env_name() -> str:
    return os.environ.get("ENV_NAME", "DEV")


# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_get(request: Request, next: str = "/admin/ui"):
    return templates.TemplateResponse("admin/login.html", {
        "request": request,
        "next": next,
        "error": None,
        "env_name": _env_name(),
    })


@router.post("/login", response_class=HTMLResponse)
async def login_post(
    request: Request,
    admin_key: str = Form(...),
    next: str = Form(default="/admin/ui"),
):
    if not check_admin_key(admin_key):
        logger.warning("Admin UI login failed from %s", request.client.host if request.client else "unknown")
        return templates.TemplateResponse("admin/login.html", {
            "request": request,
            "next": next,
            "error": "Invalid admin key.",
            "env_name": _env_name(),
        }, status_code=401)

    token = create_session_token()
    response = RedirectResponse(url=next or "/admin/ui", status_code=302)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    logger.info("Admin UI login successful from %s", request.client.host if request.client else "unknown")
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse(url="/admin/ui/login", status_code=302)
    response.delete_cookie(key=COOKIE_NAME)
    return response


# ---------------------------------------------------------------------------
# Dashboard root — redirect to overview (Iteration 1 will add the real page)
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def dashboard_root(request: Request):
    try:
        token = require_admin_ui(request)
    except _LoginRedirect as exc:
        return redirect_to_login(exc.next_url)
    return RedirectResponse(url="/admin/ui/overview", status_code=302)


@router.get("/overview", response_class=HTMLResponse)
def overview_placeholder(request: Request):
    try:
        token = require_admin_ui(request)
    except _LoginRedirect as exc:
        return redirect_to_login(exc.next_url)

    csrf = csrf_token(token)
    return templates.TemplateResponse("admin/overview.html", {
        "request": request,
        "csrf": csrf,
        "env_name": _env_name(),
        "page": "overview",
    })
