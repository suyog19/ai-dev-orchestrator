import os
import logging
import requests

logger = logging.getLogger("github_api")

GITHUB_API = "https://api.github.com"


def _headers() -> dict:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("GITHUB_TOKEN env var is not set")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _normalize_slug(repo_name: str) -> str:
    return (
        repo_name
        .removeprefix("https://github.com/")
        .removeprefix("http://github.com/")
        .removeprefix("github.com/")
        .removesuffix(".git")
    )


def create_pull_request(
    repo_name: str,
    head_branch: str,
    base_branch: str,
    title: str,
    body: str,
) -> dict:
    """Create a PR on GitHub. If one already exists for the head branch, return it.

    Returns dict with keys: number, url, title.
    """
    slug = _normalize_slug(repo_name)
    url = f"{GITHUB_API}/repos/{slug}/pulls"

    response = requests.post(
        url,
        json={"title": title, "body": body, "head": head_branch, "base": base_branch},
        headers=_headers(),
        timeout=15,
    )

    if response.status_code == 422:
        # PR already exists for this head branch — find and return it
        logger.info("PR already exists for %s, fetching existing PR", head_branch)
        return _get_existing_pr(slug, head_branch, base_branch)

    response.raise_for_status()
    data = response.json()
    logger.info("PR created: #%s — %s", data["number"], data["html_url"])
    return {"number": data["number"], "url": data["html_url"], "title": data["title"]}


def _get_existing_pr(slug: str, head_branch: str, base_branch: str) -> dict:
    """Fetch the open PR for a given head branch."""
    # GitHub requires head filter in format "owner:branch"
    owner = slug.split("/")[0]
    response = requests.get(
        f"{GITHUB_API}/repos/{slug}/pulls",
        params={"state": "open", "head": f"{owner}:{head_branch}", "base": base_branch},
        headers=_headers(),
        timeout=15,
    )
    response.raise_for_status()
    prs = response.json()
    if not prs:
        raise RuntimeError(f"No open PR found for {head_branch} → {base_branch}")
    data = prs[0]
    logger.info("Found existing PR: #%s — %s", data["number"], data["html_url"])
    return {"number": data["number"], "url": data["html_url"], "title": data["title"]}


def ensure_label(repo_name: str, name: str, color: str = "0075ca", description: str = "") -> None:
    """Create the label if it doesn't already exist on the repo. Silently no-ops if present."""
    slug = _normalize_slug(repo_name)
    response = requests.post(
        f"{GITHUB_API}/repos/{slug}/labels",
        json={"name": name, "color": color, "description": description},
        headers=_headers(),
        timeout=15,
    )
    if response.status_code == 422:
        return  # already exists
    response.raise_for_status()
    logger.info("Label created: %s on %s", name, slug)


def add_label_to_pr(repo_name: str, pr_number: int, label_name: str) -> None:
    """Apply a label to an existing PR by number."""
    slug = _normalize_slug(repo_name)
    response = requests.post(
        f"{GITHUB_API}/repos/{slug}/issues/{pr_number}/labels",
        json={"labels": [label_name]},
        headers=_headers(),
        timeout=15,
    )
    response.raise_for_status()
    logger.info("Label '%s' applied to PR #%s", label_name, pr_number)
