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
from app.database import (
    get_overview_stats, get_all_control_flags, set_control_flag,
    list_workflow_runs_for_ui, get_workflow_run_detail,
    list_planning_runs, get_planning_run_detail,
    list_clarifications,
    mark_clarification_answered, mark_clarification_cancelled,
    list_agent_reviews, list_test_quality_reviews, list_architecture_reviews,
    list_github_status_updates, list_security_events,
    list_memory_snapshots, list_feedback_events, add_manual_memory,
    record_security_event, is_paused,
    get_active_capability_profile,
    list_deployment_validations, list_deployment_profiles,
)

logger = logging.getLogger("orchestrator.ui")

router = APIRouter(prefix="/admin/ui")
templates = Jinja2Templates(directory="app/templates")


def _fmt_ts(value: str | None) -> str:
    """Format an ISO timestamp string to a readable form, or return '—'."""
    if not value:
        return "—"
    # Trim microseconds and T separator for readability
    return value[:16].replace("T", " ")


templates.env.filters["fmtts"] = _fmt_ts
templates.env.globals["is_paused"] = is_paused


def _env_name() -> str:
    return os.environ.get("ENV_NAME", "DEV")


def _base_ctx(request: Request, token: str, page: str) -> dict:
    """Return the base template context shared by all authenticated pages."""
    return {
        "request": request,
        "csrf": csrf_token(token),
        "env_name": _env_name(),
        "page": page,
        "paused": is_paused(),
    }


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
# Dashboard root — redirect to overview
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
def overview_page(request: Request):
    try:
        token = require_admin_ui(request)
    except _LoginRedirect as exc:
        return redirect_to_login(exc.next_url)

    ctx = _base_ctx(request, token, "overview")
    stats = get_overview_stats()
    flags = get_all_control_flags()

    github_writes = os.environ.get("ALLOW_GITHUB_WRITES", "true").lower() == "true"
    auto_merge = os.environ.get("ALLOW_AUTO_MERGE", "true").lower() == "true"

    return templates.TemplateResponse("admin/overview.html", {
        **ctx,
        "github_writes": github_writes,
        "auto_merge": auto_merge,
        "stats": stats,
        "flags": flags,
    })


# ---------------------------------------------------------------------------
# Workflow runs list (Iteration 2)
# ---------------------------------------------------------------------------

@router.get("/runs", response_class=HTMLResponse)
def runs_list(
    request: Request,
    status: str = "",
    workflow_type: str = "",
    issue_key: str = "",
    release_decision: str = "",
    limit: int = 30,
):
    try:
        token = require_admin_ui(request)
    except _LoginRedirect as exc:
        return redirect_to_login(exc.next_url)

    csrf = csrf_token(token)
    runs = list_workflow_runs_for_ui(
        status=status or None,
        workflow_type=workflow_type or None,
        issue_key=issue_key or None,
        release_decision=release_decision or None,
        limit=limit,
    )
    return templates.TemplateResponse("admin/runs.html", {
        "request": request,
        "csrf": csrf,
        "env_name": _env_name(),
        "page": "runs",
        "runs": runs,
        "filters": {
            "status": status,
            "workflow_type": workflow_type,
            "issue_key": issue_key,
            "release_decision": release_decision,
            "limit": limit,
        },
    })


# ---------------------------------------------------------------------------
# Workflow run detail (Iteration 3)
# ---------------------------------------------------------------------------

@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: int):
    try:
        token = require_admin_ui(request)
    except _LoginRedirect as exc:
        return redirect_to_login(exc.next_url)

    csrf = csrf_token(token)
    run = get_workflow_run_detail(run_id)
    if not run:
        return templates.TemplateResponse("admin/error.html", {
            "request": request,
            "csrf": csrf,
            "env_name": _env_name(),
            "page": "runs",
            "message": f"No workflow run found with id={run_id}",
        }, status_code=404)

    # Phase 15: load full capability profile for the repo this run touched
    capability_profile = None
    repo_slug = (run or {}).get("repo_slug") or ""
    if repo_slug:
        try:
            capability_profile = get_active_capability_profile(repo_slug)
        except Exception:
            pass

    return templates.TemplateResponse("admin/run_detail.html", {
        "request": request,
        "csrf": csrf,
        "env_name": _env_name(),
        "page": "runs",
        "run": run,
        "capability_profile": capability_profile,
    })


# ---------------------------------------------------------------------------
# Planning page (Iteration 4)
# ---------------------------------------------------------------------------

@router.get("/planning", response_class=HTMLResponse)
def planning_page(request: Request, limit: int = 20):
    try:
        token = require_admin_ui(request)
    except _LoginRedirect as exc:
        return redirect_to_login(exc.next_url)

    csrf = csrf_token(token)
    runs = list_planning_runs(limit=limit)
    return templates.TemplateResponse("admin/planning.html", {
        "request": request,
        "csrf": csrf,
        "env_name": _env_name(),
        "page": "planning",
        "runs": runs,
        "limit": limit,
    })


@router.get("/planning/{run_id}", response_class=HTMLResponse)
def planning_detail(request: Request, run_id: int):
    try:
        token = require_admin_ui(request)
    except _LoginRedirect as exc:
        return redirect_to_login(exc.next_url)

    csrf = csrf_token(token)
    run = get_planning_run_detail(run_id)
    if not run:
        return templates.TemplateResponse("admin/error.html", {
            "request": request, "csrf": csrf, "env_name": _env_name(),
            "page": "planning",
            "message": f"No planning run found with id={run_id}",
        }, status_code=404)

    return templates.TemplateResponse("admin/planning_detail.html", {
        "request": request,
        "csrf": csrf,
        "env_name": _env_name(),
        "page": "planning",
        "run": run,
    })


# ---------------------------------------------------------------------------
# Clarifications page (Iteration 5)
# ---------------------------------------------------------------------------

@router.get("/clarifications", response_class=HTMLResponse)
def clarifications_page(request: Request, status: str = "PENDING", limit: int = 30):
    try:
        token = require_admin_ui(request)
    except _LoginRedirect as exc:
        return redirect_to_login(exc.next_url)

    csrf = csrf_token(token)
    clarifications = list_clarifications(status=status or None, limit=limit)
    return templates.TemplateResponse("admin/clarifications.html", {
        "request": request,
        "csrf": csrf,
        "env_name": _env_name(),
        "page": "clarifications",
        "clarifications": clarifications,
        "active_status": status,
        "limit": limit,
    })


@router.post("/clarifications/{clar_id}/answer", response_class=HTMLResponse)
async def ui_answer_clarification(
    request: Request,
    clar_id: int,
    answer_text: str = Form(...),
    csrf_submitted: str = Form(alias="csrf"),
):
    try:
        token = require_admin_ui(request)
    except _LoginRedirect as exc:
        return redirect_to_login(exc.next_url)

    if not verify_csrf(token, csrf_submitted):
        return templates.TemplateResponse("admin/error.html", {
            "request": request, "csrf": csrf_token(token), "env_name": _env_name(),
            "page": "clarifications", "message": "CSRF validation failed.",
        }, status_code=403)

    from app.database import get_clarification_by_id
    from app.clarification import resume_workflow_after_clarification
    from app.telegram import send_message as _send

    clar = get_clarification_by_id(clar_id)
    if not clar or clar["status"] != "PENDING":
        return templates.TemplateResponse("admin/error.html", {
            "request": request, "csrf": csrf_token(token), "env_name": _env_name(),
            "page": "clarifications",
            "message": f"Clarification {clar_id} not found or not PENDING.",
        }, status_code=409)

    mark_clarification_answered(clar_id, answer_text.strip())
    resume_workflow_after_clarification(clar["run_id"])
    _send("admin_clarification_answered", "ANSWERED",
          f"UI: admin answered clarification {clar_id} for run {clar['run_id']}")
    return RedirectResponse(url="/admin/ui/clarifications", status_code=302)


@router.post("/clarifications/{clar_id}/cancel", response_class=HTMLResponse)
async def ui_cancel_clarification(
    request: Request,
    clar_id: int,
    csrf_submitted: str = Form(alias="csrf"),
):
    try:
        token = require_admin_ui(request)
    except _LoginRedirect as exc:
        return redirect_to_login(exc.next_url)

    if not verify_csrf(token, csrf_submitted):
        return templates.TemplateResponse("admin/error.html", {
            "request": request, "csrf": csrf_token(token), "env_name": _env_name(),
            "page": "clarifications", "message": "CSRF validation failed.",
        }, status_code=403)

    from app.database import get_clarification_by_id, fail_run
    from app.telegram import send_message as _send

    clar = get_clarification_by_id(clar_id)
    if not clar or clar["status"] != "PENDING":
        return templates.TemplateResponse("admin/error.html", {
            "request": request, "csrf": csrf_token(token), "env_name": _env_name(),
            "page": "clarifications",
            "message": f"Clarification {clar_id} not found or not PENDING.",
        }, status_code=409)

    mark_clarification_cancelled(clar_id)
    fail_run(clar["run_id"], f"Clarification {clar_id} cancelled from admin UI")
    _send("admin_clarification_cancelled", "CANCELLED",
          f"UI: admin cancelled clarification {clar_id} — run {clar['run_id']} FAILED")
    return RedirectResponse(url="/admin/ui/clarifications", status_code=302)


@router.post("/clarifications/{clar_id}/resend", response_class=HTMLResponse)
async def ui_resend_clarification(
    request: Request,
    clar_id: int,
    csrf_submitted: str = Form(alias="csrf"),
):
    try:
        token = require_admin_ui(request)
    except _LoginRedirect as exc:
        return redirect_to_login(exc.next_url)

    if not verify_csrf(token, csrf_submitted):
        return templates.TemplateResponse("admin/error.html", {
            "request": request, "csrf": csrf_token(token), "env_name": _env_name(),
            "page": "clarifications", "message": "CSRF validation failed.",
        }, status_code=403)

    from app.database import get_clarification_by_id, update_clarification_telegram_id
    from app.telegram import send_clarification_request

    clar = get_clarification_by_id(clar_id)
    if not clar:
        return templates.TemplateResponse("admin/error.html", {
            "request": request, "csrf": csrf_token(token), "env_name": _env_name(),
            "page": "clarifications",
            "message": f"Clarification {clar_id} not found.",
        }, status_code=404)

    msg_id = send_clarification_request(clar)
    if msg_id:
        update_clarification_telegram_id(clar_id, msg_id)
    return RedirectResponse(url="/admin/ui/clarifications", status_code=302)


# ---------------------------------------------------------------------------
# Agent reviews page (Iteration 6)
# ---------------------------------------------------------------------------

@router.get("/agents", response_class=HTMLResponse)
def agents_page(
    request: Request,
    agent_type: str = "reviewer",
    status: str = "",
    repo_slug: str = "",
    run_id_filter: int | None = None,
    limit: int = 20,
):
    try:
        token = require_admin_ui(request)
    except _LoginRedirect as exc:
        return redirect_to_login(exc.next_url)

    csrf = csrf_token(token)
    run_id_arg = run_id_filter if run_id_filter else None
    repo_arg = repo_slug or None
    status_arg = status or None

    if agent_type == "test_quality":
        reviews = list_test_quality_reviews(run_id=run_id_arg, repo_slug=repo_arg,
                                             quality_status=status_arg, limit=limit)
    elif agent_type == "architecture":
        reviews = list_architecture_reviews(run_id=run_id_arg, repo_slug=repo_arg,
                                             architecture_status=status_arg, limit=limit)
    else:
        agent_type = "reviewer"
        reviews = list_agent_reviews(run_id=run_id_arg, repo_slug=repo_arg,
                                      review_status=status_arg, limit=limit)

    return templates.TemplateResponse("admin/agents.html", {
        "request": request,
        "csrf": csrf,
        "env_name": _env_name(),
        "page": "agents",
        "agent_type": agent_type,
        "reviews": reviews,
        "filters": {"status": status, "repo_slug": repo_slug,
                    "run_id": run_id_filter, "limit": limit},
    })


# ---------------------------------------------------------------------------
# GitHub statuses page (Iteration 7)
# ---------------------------------------------------------------------------

@router.get("/github", response_class=HTMLResponse)
def github_page(request: Request, run_id_filter: int | None = None):
    try:
        token = require_admin_ui(request)
    except _LoginRedirect as exc:
        return redirect_to_login(exc.next_url)

    csrf = csrf_token(token)
    statuses = list_github_status_updates(run_id_filter) if run_id_filter else []
    return templates.TemplateResponse("admin/github.html", {
        "request": request,
        "csrf": csrf,
        "env_name": _env_name(),
        "page": "github",
        "statuses": statuses,
        "run_id_filter": run_id_filter or "",
    })


@router.post("/github/republish", response_class=HTMLResponse)
async def ui_republish_github_statuses(
    request: Request,
    run_id: int = Form(...),
    repo_slug_val: str = Form(alias="repo_slug"),
    csrf_submitted: str = Form(alias="csrf"),
):
    try:
        token = require_admin_ui(request)
    except _LoginRedirect as exc:
        return redirect_to_login(exc.next_url)

    if not verify_csrf(token, csrf_submitted):
        return templates.TemplateResponse("admin/error.html", {
            "request": request, "csrf": csrf_token(token), "env_name": _env_name(),
            "page": "github", "message": "CSRF validation failed.",
        }, status_code=403)

    from app.github_status_publisher import publish_github_statuses_for_run
    result = publish_github_statuses_for_run(run_id, repo_slug_val)
    return RedirectResponse(url=f"/admin/ui/github?run_id_filter={run_id}", status_code=302)


@router.post("/github/validate", response_class=HTMLResponse)
async def ui_validate_branch_protection(
    request: Request,
    repo_slug_val: str = Form(alias="repo_slug"),
    branch: str = Form(default="main"),
    csrf_submitted: str = Form(alias="csrf"),
):
    try:
        token = require_admin_ui(request)
    except _LoginRedirect as exc:
        return redirect_to_login(exc.next_url)

    if not verify_csrf(token, csrf_submitted):
        return templates.TemplateResponse("admin/error.html", {
            "request": request, "csrf": csrf_token(token), "env_name": _env_name(),
            "page": "github", "message": "CSRF validation failed.",
        }, status_code=403)

    from app.github_api import get_branch_protection
    result = get_branch_protection(repo_slug_val, branch)
    return templates.TemplateResponse("admin/github.html", {
        "request": request,
        "csrf": csrf_token(token),
        "env_name": _env_name(),
        "page": "github",
        "statuses": [],
        "run_id_filter": "",
        "branch_protection_result": result,
        "branch_protection_repo": repo_slug_val,
        "branch_protection_branch": branch,
    })


# ---------------------------------------------------------------------------
# Memory page (Iteration 8)
# ---------------------------------------------------------------------------

@router.get("/memory", response_class=HTMLResponse)
def memory_page(request: Request, scope_type: str = "", scope_key: str = ""):
    try:
        token = require_admin_ui(request)
    except _LoginRedirect as exc:
        return redirect_to_login(exc.next_url)

    csrf = csrf_token(token)
    snapshots = list_memory_snapshots(scope_type=scope_type or None, scope_key=scope_key or None)
    feedback = list_feedback_events(limit=30)
    return templates.TemplateResponse("admin/memory.html", {
        "request": request,
        "csrf": csrf,
        "env_name": _env_name(),
        "page": "memory",
        "snapshots": snapshots,
        "feedback": feedback,
        "filters": {"scope_type": scope_type, "scope_key": scope_key},
    })


@router.post("/memory/note", response_class=HTMLResponse)
async def ui_add_memory_note(
    request: Request,
    scope_type: str = Form(...),
    scope_key: str = Form(...),
    content: str = Form(...),
    csrf_submitted: str = Form(alias="csrf"),
):
    try:
        token = require_admin_ui(request)
    except _LoginRedirect as exc:
        return redirect_to_login(exc.next_url)

    if not verify_csrf(token, csrf_submitted):
        return templates.TemplateResponse("admin/error.html", {
            "request": request, "csrf": csrf_token(token), "env_name": _env_name(),
            "page": "memory", "message": "CSRF validation failed.",
        }, status_code=403)

    if content.strip():
        add_manual_memory(scope_type, scope_key, content.strip())
    return RedirectResponse(url="/admin/ui/memory", status_code=302)


# ---------------------------------------------------------------------------
# Security page (Iteration 9)
# ---------------------------------------------------------------------------

@router.get("/security", response_class=HTMLResponse)
def security_page(
    request: Request,
    event_type: str = "",
    source: str = "",
    status: str = "",
    limit: int = 50,
):
    try:
        token = require_admin_ui(request)
    except _LoginRedirect as exc:
        return redirect_to_login(exc.next_url)

    csrf = csrf_token(token)
    events = list_security_events(
        event_type=event_type or None,
        source=source or None,
        status=status or None,
        limit=limit,
    )
    return templates.TemplateResponse("admin/security.html", {
        "request": request,
        "csrf": csrf,
        "env_name": _env_name(),
        "page": "security",
        "events": events,
        "filters": {"event_type": event_type, "source": source, "status": status, "limit": limit},
    })


# ---------------------------------------------------------------------------
# Control page (Iteration 10)
# ---------------------------------------------------------------------------

@router.get("/control", response_class=HTMLResponse)
def control_page(request: Request):
    try:
        token = require_admin_ui(request)
    except _LoginRedirect as exc:
        return redirect_to_login(exc.next_url)

    csrf = csrf_token(token)
    flags = get_all_control_flags()
    paused = is_paused()
    github_writes = os.environ.get("ALLOW_GITHUB_WRITES", "true").lower() == "true"
    auto_merge = os.environ.get("ALLOW_AUTO_MERGE", "true").lower() == "true"
    return templates.TemplateResponse("admin/control.html", {
        "request": request,
        "csrf": csrf,
        "env_name": _env_name(),
        "page": "control",
        "flags": flags,
        "paused": paused,
        "github_writes": github_writes,
        "auto_merge": auto_merge,
    })


@router.post("/control/pause", response_class=HTMLResponse)
async def ui_pause(request: Request, csrf_submitted: str = Form(alias="csrf")):
    try:
        token = require_admin_ui(request)
    except _LoginRedirect as exc:
        return redirect_to_login(exc.next_url)

    if not verify_csrf(token, csrf_submitted):
        return templates.TemplateResponse("admin/error.html", {
            "request": request, "csrf": csrf_token(token), "env_name": _env_name(),
            "page": "control", "message": "CSRF validation failed.",
        }, status_code=403)

    set_control_flag("orchestrator_paused", "true")
    record_security_event("automation_paused_jira_blocked", "ui",
                          actor=request.client.host if request.client else "unknown",
                          endpoint="/admin/ui/control/pause", method="POST",
                          status="PAUSED")
    return RedirectResponse(url="/admin/ui/control", status_code=302)


@router.post("/control/resume", response_class=HTMLResponse)
async def ui_resume(request: Request, csrf_submitted: str = Form(alias="csrf")):
    try:
        token = require_admin_ui(request)
    except _LoginRedirect as exc:
        return redirect_to_login(exc.next_url)

    if not verify_csrf(token, csrf_submitted):
        return templates.TemplateResponse("admin/error.html", {
            "request": request, "csrf": csrf_token(token), "env_name": _env_name(),
            "page": "control", "message": "CSRF validation failed.",
        }, status_code=403)

    set_control_flag("orchestrator_paused", "false")
    record_security_event("automation_paused_jira_blocked", "ui",
                          actor=request.client.host if request.client else "unknown",
                          endpoint="/admin/ui/control/resume", method="POST",
                          status="RESUMED")
    return RedirectResponse(url="/admin/ui/control", status_code=302)


# ---------------------------------------------------------------------------
# Phase 16 — Deployment Validations list page
# ---------------------------------------------------------------------------

@router.get("/deployments", response_class=HTMLResponse)
async def ui_deployments(
    request: Request,
    repo_slug: str | None = None,
    status: str | None = None,
    limit: int = 50,
):
    try:
        token = require_admin_ui(request)
    except _LoginRedirect as exc:
        return redirect_to_login(exc.next_url)

    validations = list_deployment_validations(repo_slug=repo_slug, status=status, limit=limit)
    profiles = list_deployment_profiles()
    return templates.TemplateResponse("admin/deployments.html", {
        "request": request,
        "csrf": csrf_token(token),
        "env_name": _env_name(),
        "page": "deployments",
        "validations": validations,
        "profiles": profiles,
        "filter_repo_slug": repo_slug or "",
        "filter_status": status or "",
        "limit": limit,
    })


@router.post("/runs/{run_id}/run-deployment-validation", response_class=HTMLResponse)
async def ui_rerun_deployment_validation(
    request: Request,
    run_id: int,
    csrf_submitted: str = Form(alias="csrf"),
):
    try:
        token = require_admin_ui(request)
    except _LoginRedirect as exc:
        return redirect_to_login(exc.next_url)

    if not verify_csrf(token, csrf_submitted):
        return templates.TemplateResponse("admin/error.html", {
            "request": request, "csrf": csrf_token(token), "env_name": _env_name(),
            "page": "runs", "message": "CSRF validation failed.",
        }, status_code=403)

    from app.deployment_validator import run_deployment_validation as _run_dv
    from app.database import get_conn as _gc
    import os

    # Resolve repo_slug from run's mapping
    repo_slug = None
    with _gc() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT repo_slug FROM repo_mappings rm "
                "JOIN workflow_runs wr ON wr.id=%s "
                "WHERE rm.jira_project_key = split_part(wr.issue_key, '-', 1) "
                "LIMIT 1",
                (run_id,),
            )
            row = cur.fetchone()
            if row:
                repo_slug = row[0]

    if repo_slug:
        _run_dv(
            run_id=run_id,
            repo_slug=repo_slug,
            timeout_seconds=int(os.environ.get("DEPLOYMENT_VALIDATION_TIMEOUT_SECONDS", "120")),
            retry_count=int(os.environ.get("DEPLOYMENT_VALIDATION_RETRY_COUNT", "3")),
            retry_delay_seconds=int(os.environ.get("DEPLOYMENT_VALIDATION_RETRY_DELAY_SECONDS", "10")),
        )

    return RedirectResponse(url=f"/admin/ui/runs/{run_id}", status_code=302)


# ---------------------------------------------------------------------------
# Phase 17 — Project Onboarding Dashboard (placeholder, expanded in Iteration 8)
# ---------------------------------------------------------------------------

@router.get("/projects", response_class=HTMLResponse)
async def ui_projects(request: Request, repo_slug: str | None = None):
    try:
        token = require_admin_ui(request)
    except _LoginRedirect as exc:
        return redirect_to_login(exc.next_url)

    from app.database import list_onboarding_runs, list_knowledge_snapshots
    runs = list_onboarding_runs(repo_slug=repo_slug, limit=50)
    # Collect distinct repos that have onboarding runs
    seen: set[str] = set()
    repos = []
    for r in runs:
        if r["repo_slug"] not in seen:
            seen.add(r["repo_slug"])
            repos.append(r["repo_slug"])

    return templates.TemplateResponse("admin/projects.html", {
        "request": request,
        "csrf": csrf_token(token),
        "env_name": _env_name(),
        "page": "projects",
        "runs": runs,
        "repos": repos,
        "filter_repo_slug": repo_slug or "",
    })


@router.get("/projects/{repo_slug:path}", response_class=HTMLResponse)
async def ui_project_detail(request: Request, repo_slug: str):
    try:
        token = require_admin_ui(request)
    except _LoginRedirect as exc:
        return redirect_to_login(exc.next_url)

    from app.database import (
        list_onboarding_runs, get_onboarding_run, list_knowledge_snapshots,
        get_active_capability_profile, get_deployment_profile,
    )
    runs = list_onboarding_runs(repo_slug=repo_slug, limit=10)
    snapshots = list_knowledge_snapshots(repo_slug=repo_slug)
    capability_profile = get_active_capability_profile(repo_slug)
    deployment_profile = get_deployment_profile(repo_slug, environment="dev")

    # Get full detail for latest run (includes architecture_summary, structure_scan_json, etc.)
    latest_run = None
    if runs:
        latest_run = get_onboarding_run(runs[0]["id"])

    snaps_by_kind = {s["snapshot_kind"]: s for s in snapshots}

    return templates.TemplateResponse("admin/project_detail.html", {
        "request": request,
        "csrf": csrf_token(token),
        "env_name": _env_name(),
        "page": "projects",
        "repo_slug": repo_slug,
        "runs": runs,
        "latest_run": latest_run,
        "snapshots": snapshots,
        "snapshots_by_kind": snaps_by_kind,
        "capability_profile": capability_profile,
        "deployment_profile": deployment_profile,
    })
