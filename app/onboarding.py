import json
import logging
import os
import shutil
import subprocess

from app.claude_client import generate_onboarding_architecture_summary, generate_onboarding_coding_conventions
from app.database import update_onboarding_run, upsert_capability_profile, upsert_knowledge_snapshot, get_deployment_profile, upsert_deployment_profile
from app.repo_profiler import (
    detect_repo_capability_profile,
    get_test_command_for_profile,
    get_build_command_for_profile,
    get_lint_command_for_profile,
)
from app.repo_scanner import scan_repo_structure
from app.test_runner import run_tests, run_build, run_lint

logger = logging.getLogger("orchestrator")


def _clone_repo_readonly(run_id: int, repo_slug: str, base_branch: str) -> str:
    """Clone repo at base_branch into /tmp/onboarding/<run_id>/repo (no working branch).

    Returns absolute path to the cloned directory.
    Raises RuntimeError on failure.
    """
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if not github_token:
        raise RuntimeError("GITHUB_TOKEN env var is not set")

    work_dir = f"/tmp/onboarding/{run_id}"
    os.makedirs(work_dir, exist_ok=True)
    repo_path = os.path.join(work_dir, "repo")

    clone_url = f"https://{github_token}@github.com/{repo_slug}.git"
    logger.info("Cloning %s (branch: %s) into %s", repo_slug, base_branch, repo_path)

    result = subprocess.run(
        ["git", "clone", "--depth=1", "--branch", base_branch, clone_url, repo_path],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git clone failed: {result.stderr.strip()}")

    logger.info("Onboarding clone ready at %s", repo_path)
    return repo_path


def _infer_deployment_type(deploy_files: list[str], top_level: list[str]) -> str:
    """Infer likely deployment type from file names in the structure scan."""
    combined = [f.lower() for f in deploy_files + top_level]
    combined_str = " ".join(combined)
    if "dockerfile" in combined_str:
        return "docker"
    if ".github" in combined_str or "github" in combined_str:
        return "github_actions"
    if "procfile" in combined_str:
        return "heroku"
    if "app.yaml" in combined_str:
        return "google_app_engine"
    if "serverless.yml" in combined_str or "serverless.yaml" in combined_str:
        return "serverless"
    return "unknown"


def _check_deployment_profile(
    repo_slug: str,
    structure_scan: dict,
    environment: str = "dev",
) -> tuple[str, dict]:
    """Check or create a deployment profile for the repo.

    Returns (status_string, notes_dict).
    Status values:
      CONFIGURED_ENABLED  — existing profile is enabled
      CONFIGURED_DISABLED — existing profile exists but is disabled
      DRAFT_CREATED       — no profile existed; created disabled draft
      NOT_CONFIGURED      — no profile and couldn't infer enough to create one
    """
    existing = get_deployment_profile(repo_slug=repo_slug, environment=environment)
    deploy_files = structure_scan.get("deploy_files", [])
    top_level = structure_scan.get("top_level_dirs", []) + structure_scan.get("top_level_files", [])

    if existing:
        status = "CONFIGURED_ENABLED" if existing.get("enabled") else "CONFIGURED_DISABLED"
        notes = {
            "summary": f"Deployment profile found for {repo_slug}/{environment}: type={existing.get('deployment_type')}, enabled={existing.get('enabled')}",
            "deployment_type": existing.get("deployment_type"),
            "base_url": existing.get("base_url"),
            "enabled": existing.get("enabled"),
            "recommendations": ["Profile exists. Enable and add smoke tests when base_url is confirmed."] if not existing.get("enabled") else [],
        }
        logger.info("Deployment profile found for %s: type=%s enabled=%s", repo_slug, existing.get("deployment_type"), existing.get("enabled"))
        return status, notes

    # No existing profile — infer deployment type and create disabled draft
    inferred_type = _infer_deployment_type(deploy_files, top_level)
    recommendations = []
    if inferred_type == "docker":
        recommendations.append("Dockerfile detected. Add base_url and enable profile when deployment URL is known.")
    elif inferred_type == "github_actions":
        recommendations.append("GitHub Actions CI detected. Confirm deployment target and add base_url for smoke tests.")
    else:
        recommendations.append(f"Deployment type unclear (inferred: {inferred_type}). Set base_url manually when deployed.")

    recommendations.append("Auto-merge is disabled for this repo until deployment validation is configured and tested.")

    # Create disabled draft
    draft_data = {
        "repo_slug": repo_slug,
        "environment": environment,
        "deployment_type": inferred_type,
        "base_url": None,
        "healthcheck_path": None,
        "enabled": False,
        "smoke_tests": [],
    }
    upsert_deployment_profile(draft_data)

    notes = {
        "summary": f"No deployment profile found for {repo_slug}/{environment}. Created disabled draft (type={inferred_type}). Set base_url to enable.",
        "deployment_type": inferred_type,
        "inferred_from": deploy_files[:5],
        "base_url": None,
        "enabled": False,
        "recommendations": recommendations,
    }
    logger.info("Deployment draft created for %s: type=%s", repo_slug, inferred_type)
    return "DRAFT_CREATED", notes


def run_project_onboarding(onboarding_run_id: int, repo_slug: str, base_branch: str):
    """Execute the project onboarding workflow.

    Steps so far:
      1. Clone repo (read-only)
      2. Detect capability profile
      3. Command validation dry-run (test / build / lint)
      4. Repo structure scan (stored as structure_scan_json)
      5. Architecture summary via Claude (stored as knowledge snapshot)
      6. Coding conventions snapshot via Claude
      7. Deployment profile check (create disabled draft if missing)

    Workspace is cleaned up in the finally block regardless of outcome.
    Status transitions are managed by the worker (_execute_onboarding).
    """
    work_dir = f"/tmp/onboarding/{onboarding_run_id}"

    try:
        logger.info(
            "Project onboarding started: repo_slug=%s branch=%s (run_id=%s)",
            repo_slug, base_branch, onboarding_run_id,
        )

        # ------------------------------------------------------------------
        # Step 1: clone
        # ------------------------------------------------------------------
        update_onboarding_run(onboarding_run_id, current_step="cloning")
        repo_path = _clone_repo_readonly(onboarding_run_id, repo_slug, base_branch)

        # ------------------------------------------------------------------
        # Step 2: detect capability profile
        # ------------------------------------------------------------------
        update_onboarding_run(onboarding_run_id, current_step="profile_detection")
        profile = detect_repo_capability_profile(repo_path, repo_slug)
        profile_name = profile["profile_name"]

        upsert_capability_profile(repo_slug, profile)
        logger.info("Profile detected and stored for %s: %s", repo_slug, profile_name)

        test_cmd = get_test_command_for_profile(profile)
        build_cmd = get_build_command_for_profile(profile)
        lint_cmd = get_lint_command_for_profile(profile)

        update_onboarding_run(
            onboarding_run_id,
            current_step="profile_detected",
            capability_profile_name=profile_name,
            test_command=test_cmd,
            build_command=build_cmd,
            lint_command=lint_cmd,
        )

        # ------------------------------------------------------------------
        # Step 3: command validation dry-run
        # ------------------------------------------------------------------
        update_onboarding_run(onboarding_run_id, current_step="command_validation")

        # Test: always attempt if the profile says it supports tests
        test_result = "NOT_RUN"
        if test_cmd:
            logger.info("Onboarding command validation: running tests for %s", repo_slug)
            tr = run_tests(
                repo_path=repo_path,
                timeout=300,
                profile_command=test_cmd,
                profile_name=profile_name,
            )
            test_result = tr["status"]
            logger.info("Onboarding test result: %s", test_result)
        else:
            logger.info("Onboarding command validation: no test command — skipping")

        # Build: attempt if profile supports it
        build_result = "NOT_RUN"
        if build_cmd:
            logger.info("Onboarding command validation: running build for %s", repo_slug)
            br = run_build(repo_path=repo_path, build_command=build_cmd, profile_name=profile_name, timeout=300)
            build_result = br["status"]
            logger.info("Onboarding build result: %s", build_result)
        else:
            logger.info("Onboarding command validation: no build command — skipping")

        # Lint: attempt if configured
        lint_result = "NOT_RUN"
        if lint_cmd:
            logger.info("Onboarding command validation: running lint for %s", repo_slug)
            lr = run_lint(repo_path=repo_path, lint_command=lint_cmd, profile_name=profile_name, timeout=120)
            lint_result = lr["status"]
            logger.info("Onboarding lint result: %s", lint_result)
        else:
            logger.info("Onboarding command validation: no lint command — skipping")

        update_onboarding_run(
            onboarding_run_id,
            current_step="commands_validated",
            test_result=test_result,
            build_result=build_result,
            lint_result=lint_result,
        )

        logger.info(
            "Project onboarding command validation done: run_id=%s test=%s build=%s lint=%s",
            onboarding_run_id, test_result, build_result, lint_result,
        )

        # ------------------------------------------------------------------
        # Step 4: repo structure scan
        # ------------------------------------------------------------------
        update_onboarding_run(onboarding_run_id, current_step="structure_scan")
        structure = scan_repo_structure(repo_path, profile_name=profile_name)
        update_onboarding_run(
            onboarding_run_id,
            current_step="structure_scanned",
            structure_scan_json=json.dumps(structure),
        )
        logger.info(
            "Project onboarding structure scan done: run_id=%s total_files=%d dirs=%s",
            onboarding_run_id, structure["total_files"], structure["top_level_dirs"][:5],
        )

        # ------------------------------------------------------------------
        # Step 5: architecture summary via Claude
        # ------------------------------------------------------------------
        update_onboarding_run(onboarding_run_id, current_step="architecture_summary")
        arch = generate_onboarding_architecture_summary(
            repo_path=repo_path,
            repo_slug=repo_slug,
            structure_scan=structure,
            profile=profile,
        )

        arch_summary = arch.get("architecture_summary", "")
        upsert_knowledge_snapshot(
            repo_slug=repo_slug,
            snapshot_kind="architecture",
            summary=arch_summary,
            details=arch,
            source_files=structure.get("routing_files", []) + structure.get("config_files", []),
        )

        # Also persist open_questions as a separate snapshot if any
        open_questions = arch.get("open_questions", [])
        if open_questions:
            upsert_knowledge_snapshot(
                repo_slug=repo_slug,
                snapshot_kind="open_questions",
                summary="\n".join(f"- {q}" for q in open_questions),
                details={"open_questions": open_questions},
                source_files=None,
            )

        update_onboarding_run(
            onboarding_run_id,
            current_step="architecture_summarized",
            architecture_summary=arch_summary,
        )
        logger.info(
            "Project onboarding architecture summary done: run_id=%s (%d chars, %d open questions)",
            onboarding_run_id, len(arch_summary), len(open_questions),
        )

        # ------------------------------------------------------------------
        # Step 6: coding conventions snapshot via Claude
        # ------------------------------------------------------------------
        update_onboarding_run(onboarding_run_id, current_step="coding_conventions")
        conventions = generate_onboarding_coding_conventions(
            repo_path=repo_path,
            repo_slug=repo_slug,
            structure_scan=structure,
            profile=profile,
        )

        upsert_knowledge_snapshot(
            repo_slug=repo_slug,
            snapshot_kind="coding_conventions",
            summary=conventions.get("summary", ""),
            details=conventions,
            source_files=structure.get("routing_files", []) + structure.get("test_files", []),
        )

        update_onboarding_run(onboarding_run_id, current_step="conventions_captured")
        logger.info(
            "Project onboarding conventions captured: run_id=%s (%d patterns_to_follow)",
            onboarding_run_id, len(conventions.get("patterns_to_follow", [])),
        )

        # ------------------------------------------------------------------
        # Step 7: deployment profile check
        # ------------------------------------------------------------------
        update_onboarding_run(onboarding_run_id, current_step="deployment_check")
        deploy_status, deploy_notes = _check_deployment_profile(
            repo_slug=repo_slug,
            structure_scan=structure,
            environment="dev",
        )
        upsert_knowledge_snapshot(
            repo_slug=repo_slug,
            snapshot_kind="deployment",
            summary=deploy_notes["summary"],
            details=deploy_notes,
            source_files=structure.get("deploy_files", []),
        )
        update_onboarding_run(
            onboarding_run_id,
            current_step="deployment_checked",
            deployment_profile_status=deploy_status,
        )
        logger.info(
            "Project onboarding deployment check done: run_id=%s status=%s",
            onboarding_run_id, deploy_status,
        )

    finally:
        if os.path.isdir(work_dir):
            try:
                shutil.rmtree(work_dir)
                logger.info("Onboarding workspace cleaned up: %s", work_dir)
            except Exception as exc:
                logger.warning("Onboarding workspace cleanup failed: %s", exc)
