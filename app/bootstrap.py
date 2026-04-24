"""
Phase 18 Iteration 7 — New project bootstrap workflow.

run_project_bootstrap() clones an empty/near-empty repo, writes a minimal
skeleton from templates/bootstrap/<project_type>/, customizes the README
via Claude, commits, and opens a PR. Never auto-merges.
"""

import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger("orchestrator")

BOOTSTRAP_TEMPLATES_DIR = Path(__file__).parent.parent / "templates" / "bootstrap"
SUPPORTED_PROJECT_TYPES = ("python_fastapi", "static_site")


def _clone_for_bootstrap(repo_slug: str, base_branch: str, work_dir: str) -> str:
    """Shallow clone the repo for bootstrap. Returns repo_path."""
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if not github_token:
        raise RuntimeError("GITHUB_TOKEN env var is not set")
    os.makedirs(work_dir, exist_ok=True)
    repo_path = os.path.join(work_dir, "repo")
    clone_url = f"https://{github_token}@github.com/{repo_slug}.git"
    result = subprocess.run(
        ["git", "clone", "--depth=1", "--branch", base_branch, clone_url, repo_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git clone failed: {result.stderr.strip()}")
    return repo_path


def _check_repo_is_near_empty(repo_path: str) -> list[str]:
    """Return list of non-trivial files. Empty = ok to bootstrap."""
    skip = {".git", ".gitignore", "LICENSE", "license", "README.md", "readme.md"}
    found = []
    for item in Path(repo_path).iterdir():
        if item.name not in skip:
            found.append(item.name)
    return found


def _copy_template(project_type: str, repo_path: str) -> list[str]:
    """Copy template files into repo_path. Returns list of copied relative paths."""
    template_dir = BOOTSTRAP_TEMPLATES_DIR / project_type
    if not template_dir.exists():
        raise RuntimeError(f"No bootstrap template for project_type='{project_type}'")
    copied = []
    for src in template_dir.rglob("*"):
        if src.is_file():
            rel = src.relative_to(template_dir)
            dst = Path(repo_path) / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(str(rel))
    return copied


def _customize_readme(repo_path: str, repo_slug: str, project_type: str, description: str) -> None:
    """Replace placeholder tokens in README.md with actual project info."""
    readme = Path(repo_path) / "README.md"
    if not readme.exists():
        return
    content = readme.read_text()
    content = content.replace("{{REPO_SLUG}}", repo_slug)
    content = content.replace("{{PROJECT_TYPE}}", project_type)
    content = content.replace("{{DESCRIPTION}}", description or f"A {project_type} project")
    readme.write_text(content)


def _git_config(repo_path: str) -> None:
    subprocess.run(["git", "config", "user.email", "orchestrator@ai-dev"], cwd=repo_path, check=True)
    subprocess.run(["git", "config", "user.name", "AI Dev Orchestrator"], cwd=repo_path, check=True)


def run_project_bootstrap(
    repo_slug: str,
    project_type: str,
    base_branch: str = "main",
    description: str = "",
) -> dict:
    """Bootstrap a new project by writing a minimal skeleton and opening a PR.

    Steps:
    1. Validate inputs and project type.
    2. Clone repo — must be empty or near-empty.
    3. Copy template files from templates/bootstrap/<project_type>/.
    4. Customize README with repo_slug and description.
    5. Commit on a new branch and push.
    6. Open a PR against base_branch.
    7. Never auto-merge.

    Returns a summary dict with pr_url, branch, files_created.
    """
    import uuid

    if project_type not in SUPPORTED_PROJECT_TYPES:
        raise ValueError(
            f"Unsupported project_type '{project_type}'. "
            f"Supported: {', '.join(SUPPORTED_PROJECT_TYPES)}"
        )

    run_id = uuid.uuid4().hex[:8]
    work_dir = f"/tmp/bootstrap/{run_id}"

    try:
        logger.info("Bootstrap started: repo=%s type=%s", repo_slug, project_type)

        # Clone
        repo_path = _clone_for_bootstrap(repo_slug, base_branch, work_dir)

        # Check repo is near-empty
        existing = _check_repo_is_near_empty(repo_path)
        if existing:
            raise RuntimeError(
                f"Repo '{repo_slug}' is not empty — found: {existing[:5]}. "
                "Bootstrap only works on empty or near-empty repos."
            )

        # Copy template
        files_created = _copy_template(project_type, repo_path)
        logger.info("Bootstrap: copied %d template files for %s", len(files_created), project_type)

        # Customize README
        _customize_readme(repo_path, repo_slug, project_type, description)

        # Commit on bootstrap branch
        bootstrap_branch = f"ai/bootstrap-{project_type}-{run_id}"
        _git_config(repo_path)
        subprocess.run(["git", "checkout", "-b", bootstrap_branch], cwd=repo_path, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo_path, check=True)
        commit_msg = f"chore: bootstrap {project_type} skeleton\n\nGenerated by AI Dev Orchestrator bootstrap workflow."
        subprocess.run(["git", "commit", "-m", commit_msg], cwd=repo_path, check=True)

        # Push
        github_token = os.environ.get("GITHUB_TOKEN", "")
        remote_url = f"https://{github_token}@github.com/{repo_slug}.git"
        subprocess.run(
            ["git", "push", remote_url, bootstrap_branch],
            cwd=repo_path, check=True, capture_output=True,
        )
        logger.info("Bootstrap: pushed branch %s to %s", bootstrap_branch, repo_slug)

        # Create PR
        from app.github_api import create_pull_request
        pr = create_pull_request(
            repo_name=repo_slug,
            branch=bootstrap_branch,
            base=base_branch,
            title=f"ai: bootstrap {project_type} skeleton",
            body=(
                f"## Bootstrap PR\n\n"
                f"This PR adds a minimal `{project_type}` skeleton generated by the AI Dev Orchestrator.\n\n"
                f"**Project:** {repo_slug}\n"
                f"**Type:** {project_type}\n"
                f"**Description:** {description or '(none provided)'}\n\n"
                f"**Files added:**\n" + "\n".join(f"- `{f}`" for f in files_created) + "\n\n"
                f"Review and merge manually — bootstrap PRs never auto-merge.\n\n"
                f"🤖 Generated by [AI Dev Orchestrator](https://github.com/suyog19/ai-dev-orchestrator)"
            ),
        )

        logger.info("Bootstrap PR created: %s", pr.get("html_url"))
        return {
            "repo_slug": repo_slug,
            "project_type": project_type,
            "bootstrap_branch": bootstrap_branch,
            "files_created": files_created,
            "pr_url": pr.get("html_url"),
            "pr_number": pr.get("number"),
            "status": "PR_CREATED",
            "auto_merge": False,
        }

    finally:
        if os.path.isdir(work_dir):
            try:
                shutil.rmtree(work_dir)
            except Exception:
                pass
