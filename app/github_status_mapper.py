"""
Phase 13 — Pure functions mapping internal orchestrator verdicts to GitHub commit status payloads.

Each mapper returns a dict with: state, description, context.
Unknown values always map to 'error' so GitHub shows something is wrong rather than silent success.
"""

from app.feedback import GitHubStatusContext, GitHubState


def map_test_status_to_github(test_status: str | None) -> dict:
    """Map workflow_runs.test_status → GitHub commit status payload."""
    match test_status:
        case "PASSED":
            return {
                "state": GitHubState.SUCCESS,
                "description": "Tests passed: pytest -q",
                "context": GitHubStatusContext.TESTS,
            }
        case "FAILED":
            return {
                "state": GitHubState.FAILURE,
                "description": "Tests failed: pytest -q",
                "context": GitHubStatusContext.TESTS,
            }
        case "NOT_RUN" | None:
            return {
                "state": GitHubState.FAILURE,
                "description": "Tests were not run for this repo",
                "context": GitHubStatusContext.TESTS,
            }
        case _:
            return {
                "state": GitHubState.ERROR,
                "description": f"Unknown test status: {test_status}",
                "context": GitHubStatusContext.TESTS,
            }


def map_reviewer_status_to_github(review_status: str | None) -> dict:
    """Map workflow_runs.review_status → GitHub commit status payload."""
    match review_status:
        case "APPROVED_BY_AI":
            return {
                "state": GitHubState.SUCCESS,
                "description": "Reviewer agent approved PR",
                "context": GitHubStatusContext.REVIEWER,
            }
        case "NEEDS_CHANGES":
            return {
                "state": GitHubState.FAILURE,
                "description": "Reviewer agent: changes requested",
                "context": GitHubStatusContext.REVIEWER,
            }
        case "BLOCKED":
            return {
                "state": GitHubState.FAILURE,
                "description": "Reviewer agent blocked merge",
                "context": GitHubStatusContext.REVIEWER,
            }
        case "ERROR":
            return {
                "state": GitHubState.ERROR,
                "description": "Reviewer agent encountered an error",
                "context": GitHubStatusContext.REVIEWER,
            }
        case None:
            return {
                "state": GitHubState.PENDING,
                "description": "Reviewer agent has not run",
                "context": GitHubStatusContext.REVIEWER,
            }
        case _:
            return {
                "state": GitHubState.ERROR,
                "description": f"Unknown reviewer status: {review_status}",
                "context": GitHubStatusContext.REVIEWER,
            }


def map_test_quality_status_to_github(test_quality_status: str | None) -> dict:
    """Map workflow_runs.test_quality_status → GitHub commit status payload."""
    match test_quality_status:
        case "TEST_QUALITY_APPROVED":
            return {
                "state": GitHubState.SUCCESS,
                "description": "Test quality approved",
                "context": GitHubStatusContext.TEST_QUALITY,
            }
        case "TESTS_WEAK":
            return {
                "state": GitHubState.FAILURE,
                "description": "Test quality: tests are weak",
                "context": GitHubStatusContext.TEST_QUALITY,
            }
        case "TESTS_BLOCKING":
            return {
                "state": GitHubState.FAILURE,
                "description": "Test quality agent blocked merge",
                "context": GitHubStatusContext.TEST_QUALITY,
            }
        case "ERROR":
            return {
                "state": GitHubState.ERROR,
                "description": "Test quality agent encountered an error",
                "context": GitHubStatusContext.TEST_QUALITY,
            }
        case None:
            return {
                "state": GitHubState.PENDING,
                "description": "Test quality agent has not run",
                "context": GitHubStatusContext.TEST_QUALITY,
            }
        case _:
            return {
                "state": GitHubState.ERROR,
                "description": f"Unknown test quality status: {test_quality_status}",
                "context": GitHubStatusContext.TEST_QUALITY,
            }


def map_architecture_status_to_github(architecture_status: str | None) -> dict:
    """Map workflow_runs.architecture_status → GitHub commit status payload."""
    match architecture_status:
        case "ARCHITECTURE_APPROVED":
            return {
                "state": GitHubState.SUCCESS,
                "description": "Architecture approved",
                "context": GitHubStatusContext.ARCHITECTURE,
            }
        case "ARCHITECTURE_NEEDS_REVIEW":
            return {
                "state": GitHubState.FAILURE,
                "description": "Architecture agent: human review recommended",
                "context": GitHubStatusContext.ARCHITECTURE,
            }
        case "ARCHITECTURE_BLOCKED":
            return {
                "state": GitHubState.FAILURE,
                "description": "Architecture agent blocked merge",
                "context": GitHubStatusContext.ARCHITECTURE,
            }
        case "ERROR":
            return {
                "state": GitHubState.ERROR,
                "description": "Architecture agent encountered an error",
                "context": GitHubStatusContext.ARCHITECTURE,
            }
        case None:
            return {
                "state": GitHubState.PENDING,
                "description": "Architecture agent has not run",
                "context": GitHubStatusContext.ARCHITECTURE,
            }
        case _:
            return {
                "state": GitHubState.ERROR,
                "description": f"Unknown architecture status: {architecture_status}",
                "context": GitHubStatusContext.ARCHITECTURE,
            }


def map_release_decision_to_github(release_decision: str | None) -> dict:
    """Map workflow_runs.release_decision → GitHub commit status payload."""
    match release_decision:
        case "RELEASE_APPROVED":
            return {
                "state": GitHubState.SUCCESS,
                "description": "Release approved: all gates passed",
                "context": GitHubStatusContext.RELEASE_GATE,
            }
        case "RELEASE_SKIPPED":
            return {
                "state": GitHubState.FAILURE,
                "description": "Release skipped: one or more gates did not pass",
                "context": GitHubStatusContext.RELEASE_GATE,
            }
        case "RELEASE_BLOCKED":
            return {
                "state": GitHubState.FAILURE,
                "description": "Release blocked: a gate explicitly blocked merge",
                "context": GitHubStatusContext.RELEASE_GATE,
            }
        case None:
            return {
                "state": GitHubState.PENDING,
                "description": "Release gate has not evaluated yet",
                "context": GitHubStatusContext.RELEASE_GATE,
            }
        case _:
            return {
                "state": GitHubState.ERROR,
                "description": f"Unknown release decision: {release_decision}",
                "context": GitHubStatusContext.RELEASE_GATE,
            }
