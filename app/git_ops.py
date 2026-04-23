import os
import logging
import subprocess

logger = logging.getLogger("git_ops")


def clone_repo(run_id: int, issue_key: str, repo_name: str, target_branch: str) -> str:
    """Clone repo at target_branch, create working branch ai/issue-<issue_key>.

    Returns the absolute path to the cloned repo directory.
    Raises RuntimeError if GITHUB_TOKEN is missing or git commands fail.
    """
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if not github_token:
        raise RuntimeError("GITHUB_TOKEN env var is not set")

    # Normalize to bare "owner/repo" — accept any prefix variant
    repo_slug = (
        repo_name
        .removeprefix("https://github.com/")
        .removeprefix("http://github.com/")
        .removeprefix("github.com/")
        .removesuffix(".git")
    )

    work_dir = f"/tmp/workflows/{run_id}"
    os.makedirs(work_dir, exist_ok=True)

    repo_path = os.path.join(work_dir, "repo")
    clone_url = f"https://{github_token}@github.com/{repo_slug}.git"
    working_branch = f"ai/{issue_key}/{run_id}"

    logger.info("Cloning %s (branch: %s) into %s", repo_slug, target_branch, repo_path)
    result = subprocess.run(
        ["git", "clone", "--depth=1", "--branch", target_branch, clone_url, repo_path],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git clone failed: {result.stderr.strip()}")

    logger.info("Creating working branch %s", working_branch)
    result = subprocess.run(
        ["git", "checkout", "-b", working_branch],
        capture_output=True,
        text=True,
        cwd=repo_path,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git checkout failed: {result.stderr.strip()}")

    logger.info("Repo ready at %s on branch %s", repo_path, working_branch)
    return repo_path


def commit_and_push(repo_path: str, issue_key: str, run_id: int, commit_message: str) -> str:
    """Stage all changes, commit, and push the working branch to origin.

    Returns the working branch name.
    Raises RuntimeError if any git command fails.
    """
    working_branch = f"ai/{issue_key}/{run_id}"

    def _git(*args):
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=repo_path,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    # Set commit identity (required in a fresh container environment)
    _git("config", "user.email", "orchestrator@ai-dev-orchestrator.local")
    _git("config", "user.name", "AI Dev Orchestrator")

    _git("add", "-A")

    # Check there is actually something to commit
    status = _git("status", "--porcelain")
    if not status:
        logger.info("Nothing to commit in %s", repo_path)
        return working_branch

    _git("commit", "-m", commit_message)
    logger.info("Committed: %s", commit_message)

    _git("push", "--force", "origin", working_branch)
    logger.info("Pushed branch %s to origin", working_branch)

    return working_branch
