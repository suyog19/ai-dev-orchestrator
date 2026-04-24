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


def post_pr_comment(repo_name: str, pr_number: int, body: str) -> dict:
    """Post a top-level comment on a PR (uses the issues comments endpoint).

    Returns the created comment dict with at least 'id' and 'html_url'.
    Raises on HTTP errors.
    """
    slug = _normalize_slug(repo_name)
    response = requests.post(
        f"{GITHUB_API}/repos/{slug}/issues/{pr_number}/comments",
        json={"body": body},
        headers=_headers(),
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    logger.info("PR #%s comment posted — id=%s", pr_number, data.get("id"))
    return {"id": data["id"], "html_url": data["html_url"]}


def get_branch_protection(repo_name: str, branch: str = "main") -> dict:
    """Fetch branch protection info and return an audit summary.

    Returns a dict with: repo_slug, branch, protected, required_reviews,
    required_status_checks, allow_force_pushes, allow_deletions, warnings,
    orchestrator_check_status (Phase 13: checks for required orchestrator contexts).
    """
    from app.feedback import GitHubStatusContext, GITHUB_REQUIRED_CHECK

    _OPTIONAL_ORCHESTRATOR_CHECKS = [
        GitHubStatusContext.TESTS,
        GitHubStatusContext.REVIEWER,
        GitHubStatusContext.TEST_QUALITY,
        GitHubStatusContext.ARCHITECTURE,
    ]

    slug = _normalize_slug(repo_name)
    response = requests.get(
        f"{GITHUB_API}/repos/{slug}/branches/{branch}/protection",
        headers=_headers(),
        timeout=10,
    )
    if response.status_code == 404:
        return {
            "repo_slug": slug,
            "branch": branch,
            "protected": False,
            "required_reviews": False,
            "required_status_checks": [],
            "allow_force_pushes": True,
            "allow_deletions": True,
            "warnings": [
                f"Branch '{branch}' has no protection rules",
                f"CRITICAL: '{GITHUB_REQUIRED_CHECK}' is not a required check — manual merge can bypass AI gates",
            ],
            "orchestrator_check_status": {
                "release_gate_required": False,
                "missing_required": [GITHUB_REQUIRED_CHECK],
                "optional_configured": [],
            },
        }
    response.raise_for_status()
    data = response.json()
    warnings = []

    req_reviews = data.get("required_pull_request_reviews") or {}
    required_reviews = bool(req_reviews)
    if not required_reviews:
        warnings.append("No required PR reviews")

    req_checks = data.get("required_status_checks") or {}
    # Contexts can appear as plain strings or as {"context": str} check objects
    raw_contexts = req_checks.get("contexts", [])
    raw_checks = req_checks.get("checks", [])
    check_names = set(raw_contexts)
    for c in raw_checks:
        if isinstance(c, dict):
            check_names.add(c.get("context", ""))
        else:
            check_names.add(str(c))

    if not check_names:
        warnings.append("No required status checks configured")

    # Phase 13 — verify orchestrator required check
    release_gate_required = GITHUB_REQUIRED_CHECK in check_names
    if not release_gate_required:
        warnings.append(
            f"CRITICAL: '{GITHUB_REQUIRED_CHECK}' is not a required check — "
            "manual merge can bypass AI release gate"
        )

    optional_configured = [c for c in _OPTIONAL_ORCHESTRATOR_CHECKS if c in check_names]

    force_push = (data.get("allow_force_pushes") or {}).get("enabled", False)
    if force_push:
        warnings.append("Force pushes are allowed on this branch")

    allow_del = (data.get("allow_deletions") or {}).get("enabled", False)
    if allow_del:
        warnings.append("Branch deletions are allowed")

    return {
        "repo_slug": slug,
        "branch": branch,
        "protected": True,
        "required_reviews": required_reviews,
        "required_reviews_count": req_reviews.get("required_approving_review_count", 0),
        "dismiss_stale_reviews": req_reviews.get("dismiss_stale_reviews", False),
        "required_status_checks": sorted(check_names),
        "allow_force_pushes": force_push,
        "allow_deletions": allow_del,
        "warnings": warnings,
        "orchestrator_check_status": {
            "release_gate_required": release_gate_required,
            "missing_required": [] if release_gate_required else [GITHUB_REQUIRED_CHECK],
            "optional_configured": optional_configured,
        },
    }


def get_pr_diff(repo_name: str, pr_number: int) -> str:
    """Fetch the unified diff for a GitHub PR (used for review resume after clarification)."""
    slug = _normalize_slug(repo_name)
    response = requests.get(
        f"{GITHUB_API}/repos/{slug}/pulls/{pr_number}",
        headers={**_headers(), "Accept": "application/vnd.github.v3.diff"},
        timeout=15,
    )
    response.raise_for_status()
    return response.text


def get_pr_details(repo_slug: str, pr_number: int) -> dict:
    """Fetch PR metadata including the head commit SHA.

    Returns dict with: number, head_sha, head_ref, base_ref, state, html_url.
    Raises on HTTP errors.
    """
    slug = _normalize_slug(repo_slug)
    response = requests.get(
        f"{GITHUB_API}/repos/{slug}/pulls/{pr_number}",
        headers=_headers(),
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    return {
        "number":   data["number"],
        "head_sha": data["head"]["sha"],
        "head_ref": data["head"]["ref"],
        "base_ref": data["base"]["ref"],
        "state":    data["state"],
        "html_url": data["html_url"],
    }


_VALID_GITHUB_STATES = {"success", "failure", "pending", "error"}


def create_commit_status(
    repo_slug: str,
    sha: str,
    state: str,
    context: str,
    description: str,
    target_url: str | None = None,
) -> dict:
    """Publish a GitHub commit status on a specific SHA.

    state must be one of: success, failure, pending, error.
    Raises ValueError for invalid state, RuntimeError/HTTPError on API failures.
    Returns the GitHub API response dict.
    """
    if state not in _VALID_GITHUB_STATES:
        raise ValueError(f"Invalid GitHub status state '{state}'. Must be one of {_VALID_GITHUB_STATES}")
    if not sha or len(sha) < 7:
        raise ValueError(f"Invalid commit SHA: {sha!r}")

    # GitHub descriptions >140 chars are silently truncated — enforce here so logs are accurate
    if len(description) > 140:
        description = description[:137] + "..."

    slug = _normalize_slug(repo_slug)
    payload: dict = {"state": state, "context": context, "description": description}
    if target_url:
        payload["target_url"] = target_url

    response = requests.post(
        f"{GITHUB_API}/repos/{slug}/statuses/{sha}",
        json=payload,
        headers=_headers(),
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    logger.info(
        "Commit status published: repo=%s sha=%.8s context=%s state=%s",
        slug, sha, context, state,
    )
    return data


def merge_pull_request(repo_name: str, pr_number: int, commit_title: str) -> dict:
    """Squash-merge a PR. Returns {"sha": str, "merged": bool, "message": str}.

    Raises RuntimeError on non-mergeable (405) or conflict (409).
    """
    slug = _normalize_slug(repo_name)
    response = requests.put(
        f"{GITHUB_API}/repos/{slug}/pulls/{pr_number}/merge",
        json={"commit_title": commit_title, "merge_method": "squash"},
        headers=_headers(),
        timeout=15,
    )
    if response.status_code == 405:
        raise RuntimeError(f"PR #{pr_number} is not mergeable: {response.json().get('message', '')}")
    if response.status_code == 409:
        raise RuntimeError(f"PR #{pr_number} has a merge conflict: {response.json().get('message', '')}")
    response.raise_for_status()
    data = response.json()
    logger.info("PR #%s merged (squash) — sha %s", pr_number, data.get("sha", "")[:8])
    return data
