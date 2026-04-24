"""
Phase 16 — Deployment Validator.

run_http_smoke_test()  — execute a single HTTP smoke check
run_deployment_validation() — orchestrate all smoke tests for a run
"""

import json
import logging
import time

import requests

from app.database import (
    get_deployment_profile,
    store_deployment_validation,
)
from app.feedback import DeploymentValidationStatus

logger = logging.getLogger("worker")

_SAFE_RESPONSE_EXCERPT_CHARS = 500


def run_http_smoke_test(
    base_url: str,
    smoke_test: dict,
    timeout_seconds: int = 30,
) -> dict:
    """Execute a single HTTP smoke check.

    Args:
        base_url: Root URL of the deployment (e.g. "https://dev.example.com").
        smoke_test: Dict with keys:
            name            — human-readable test name
            type            — must be "http"
            method          — HTTP method (currently only GET supported)
            path            — URL path appended to base_url
            expected_status — expected HTTP status code (int)
            expected_contains — optional substring expected in response body
        timeout_seconds: Max seconds to wait for the response.

    Returns dict:
        name, status (PASSED|FAILED|ERROR), url, status_code, duration_ms, summary
    """
    name = smoke_test.get("name", "unnamed")
    method = (smoke_test.get("method") or "GET").upper()
    path = smoke_test.get("path", "/")
    expected_status = smoke_test.get("expected_status", 200)
    expected_contains = smoke_test.get("expected_contains")

    # Build full URL safely — never pass headers with secrets
    if not path.startswith("/"):
        path = f"/{path}"
    url = base_url.rstrip("/") + path

    result: dict = {
        "name": name,
        "status": "ERROR",
        "url": url,
        "status_code": None,
        "duration_ms": None,
        "summary": "",
    }

    start = time.monotonic()
    try:
        if method != "GET":
            result["summary"] = f"Unsupported HTTP method: {method}"
            return result

        resp = requests.get(url, timeout=timeout_seconds, allow_redirects=True)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        result["status_code"] = resp.status_code
        result["duration_ms"] = elapsed_ms

        if resp.status_code != expected_status:
            result["status"] = "FAILED"
            result["summary"] = (
                f"Expected HTTP {expected_status}, got {resp.status_code} "
                f"({elapsed_ms}ms)"
            )
            return result

        if expected_contains:
            body_excerpt = resp.text[:_SAFE_RESPONSE_EXCERPT_CHARS]
            if expected_contains not in resp.text:
                result["status"] = "FAILED"
                result["summary"] = (
                    f"Response did not contain expected string '{expected_contains}'. "
                    f"Body excerpt: {body_excerpt!r}"
                )
                return result

        result["status"] = "PASSED"
        result["summary"] = f"HTTP {resp.status_code} in {elapsed_ms}ms"

    except requests.exceptions.ConnectionError as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        result["duration_ms"] = elapsed_ms
        result["summary"] = f"Connection error: {str(exc)[:200]}"
    except requests.exceptions.Timeout:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        result["duration_ms"] = elapsed_ms
        result["summary"] = f"Request timed out after {timeout_seconds}s"
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        result["duration_ms"] = elapsed_ms
        result["summary"] = f"Unexpected error: {str(exc)[:200]}"

    return result


def run_deployment_validation(
    run_id: int,
    repo_slug: str,
    environment: str = "dev",
    commit_sha: str | None = None,
    pr_number: int | None = None,
    timeout_seconds: int = 30,
    retry_count: int = 3,
    retry_delay_seconds: int = 10,
) -> dict:
    """Orchestrate deployment validation for a workflow run.

    Steps:
      1. Load active deployment_profile for repo_slug + environment.
      2. If none, store NOT_CONFIGURED and return.
      3. If disabled, store SKIPPED and return.
      4. Run each smoke test with retries.
      5. Store result in deployment_validations + update workflow_runs.

    Returns a dict with keys:
        status, summary, smoke_results, validation_id, profile_id
    """
    profile = get_deployment_profile(repo_slug, environment)

    if not profile:
        logger.info(
            "run_deployment_validation: no profile for %s/%s — NOT_CONFIGURED",
            repo_slug, environment,
        )
        val_id = store_deployment_validation(
            run_id=run_id,
            repo_slug=repo_slug,
            environment=environment,
            status=DeploymentValidationStatus.NOT_CONFIGURED,
            summary="No deployment profile configured for this repo/environment.",
            commit_sha=commit_sha,
            pr_number=pr_number,
        )
        return {
            "status": DeploymentValidationStatus.NOT_CONFIGURED,
            "summary": "No deployment profile configured.",
            "smoke_results": [],
            "validation_id": val_id,
            "profile_id": None,
        }

    if not profile.get("enabled", True):
        logger.info(
            "run_deployment_validation: profile disabled for %s/%s — SKIPPED",
            repo_slug, environment,
        )
        val_id = store_deployment_validation(
            run_id=run_id,
            repo_slug=repo_slug,
            environment=environment,
            status=DeploymentValidationStatus.SKIPPED,
            summary="Deployment profile is disabled.",
            commit_sha=commit_sha,
            pr_number=pr_number,
            deployment_profile_id=profile["id"],
        )
        return {
            "status": DeploymentValidationStatus.SKIPPED,
            "summary": "Deployment profile is disabled.",
            "smoke_results": [],
            "validation_id": val_id,
            "profile_id": profile["id"],
        }

    base_url = profile.get("base_url") or ""
    smoke_tests = profile.get("smoke_tests") or []

    if not base_url:
        val_id = store_deployment_validation(
            run_id=run_id,
            repo_slug=repo_slug,
            environment=environment,
            status=DeploymentValidationStatus.ERROR,
            summary="Deployment profile has no base_url configured.",
            commit_sha=commit_sha,
            pr_number=pr_number,
            deployment_profile_id=profile["id"],
        )
        return {
            "status": DeploymentValidationStatus.ERROR,
            "summary": "No base_url in deployment profile.",
            "smoke_results": [],
            "validation_id": val_id,
            "profile_id": profile["id"],
        }

    if not smoke_tests:
        # No tests configured — treat as SKIPPED
        val_id = store_deployment_validation(
            run_id=run_id,
            repo_slug=repo_slug,
            environment=environment,
            status=DeploymentValidationStatus.SKIPPED,
            summary="No smoke tests configured in profile.",
            commit_sha=commit_sha,
            pr_number=pr_number,
            deployment_profile_id=profile["id"],
        )
        return {
            "status": DeploymentValidationStatus.SKIPPED,
            "summary": "No smoke tests configured.",
            "smoke_results": [],
            "validation_id": val_id,
            "profile_id": profile["id"],
        }

    # Run smoke tests with retries
    all_results: list[dict] = []
    failed_names: list[str] = []

    for smoke_test in smoke_tests:
        test_type = smoke_test.get("type", "http")
        if test_type != "http":
            all_results.append({
                "name": smoke_test.get("name", "unknown"),
                "status": "ERROR",
                "url": "",
                "status_code": None,
                "duration_ms": None,
                "summary": f"Unsupported smoke test type: {test_type}",
            })
            failed_names.append(smoke_test.get("name", "unknown"))
            continue

        last_result: dict = {}
        for attempt in range(1, retry_count + 1):
            last_result = run_http_smoke_test(
                base_url=base_url,
                smoke_test=smoke_test,
                timeout_seconds=timeout_seconds,
            )
            if last_result["status"] == "PASSED":
                break
            if attempt < retry_count:
                logger.info(
                    "Smoke test '%s' attempt %d/%d failed (%s) — retrying in %ds",
                    smoke_test.get("name"), attempt, retry_count,
                    last_result["summary"], retry_delay_seconds,
                )
                time.sleep(retry_delay_seconds)

        all_results.append(last_result)
        if last_result["status"] != "PASSED":
            failed_names.append(smoke_test.get("name", "unknown"))

    passed = sum(1 for r in all_results if r["status"] == "PASSED")
    total = len(all_results)

    if failed_names:
        final_status = DeploymentValidationStatus.FAILED
        summary = (
            f"Smoke tests: {passed}/{total} passed. "
            f"Failed: {', '.join(failed_names)}"
        )
    else:
        final_status = DeploymentValidationStatus.PASSED
        summary = f"All smoke tests passed: {passed}/{total}"

    logger.info(
        "run_deployment_validation: run_id=%s repo=%s status=%s (%s)",
        run_id, repo_slug, final_status, summary,
    )

    val_id = store_deployment_validation(
        run_id=run_id,
        repo_slug=repo_slug,
        environment=environment,
        status=final_status,
        summary=summary,
        smoke_results=all_results,
        commit_sha=commit_sha,
        pr_number=pr_number,
        deployment_profile_id=profile["id"],
    )

    return {
        "status": final_status,
        "summary": summary,
        "smoke_results": all_results,
        "validation_id": val_id,
        "profile_id": profile["id"],
    }
