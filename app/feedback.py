"""
Phase 7 — feedback and memory constants.

All string literals used across feedback capture, memory snapshots,
and failure categorization live here to prevent typo drift.
"""


class FeedbackSource:
    PLANNING_RUN = "planning_run"
    EXECUTION_RUN = "execution_run"


class FeedbackType:
    # Planning signals
    PLANNING_APPROVED           = "planning_approved"
    PLANNING_REJECTED           = "planning_rejected"
    PLANNING_REGENERATED        = "planning_regenerated"
    STORIES_PROPOSED_COUNT      = "stories_proposed_count"
    STORIES_CREATED_COUNT       = "stories_created_count"
    APPROVAL_LATENCY_SECONDS    = "approval_latency_seconds"
    # Execution signals
    EXECUTION_COMPLETED         = "execution_completed"
    EXECUTION_FAILED            = "execution_failed"
    TEST_STATUS                 = "test_status"
    RETRY_COUNT                 = "retry_count"
    MERGE_STATUS                = "merge_status"
    FILES_CHANGED_COUNT         = "files_changed_count"
    FAILURE_CATEGORY            = "failure_category"
    # Review signals (Phase 8)
    REVIEW_STATUS               = "review_status"
    REVIEW_RISK_LEVEL           = "review_risk_level"
    REVIEW_APPROVED             = "review_approved"
    REVIEW_NEEDS_CHANGES        = "review_needs_changes"
    REVIEW_BLOCKED              = "review_blocked"


class FailureCategory:
    TEST_FAILURE                = "test_failure"
    SYNTAX_FAILURE              = "syntax_failure"
    APPLY_VALIDATION_FAILURE    = "apply_validation_failure"
    JIRA_CREATION_FAILURE       = "jira_creation_failure"
    MERGE_FAILURE               = "merge_failure"
    DUPLICATE_BLOCKED           = "duplicate_blocked"
    APPROVAL_REJECTED           = "approval_rejected"
    APPROVAL_REGENERATED        = "approval_regenerated"
    WORKER_INTERRUPTED          = "worker_interrupted"
    UNKNOWN                     = "unknown"


class MemoryScope:
    RUN    = "run"
    EPIC   = "epic"
    REPO   = "repo"
    GLOBAL = "global"


class MemoryKind:
    PLANNING_GUIDANCE   = "planning_guidance"
    EXECUTION_GUIDANCE  = "execution_guidance"
    MANUAL_NOTE         = "manual_note"


# Phase 8 — Reviewer Agent constants

class AgentName:
    REVIEWER_AGENT = "reviewer_agent"


class ReviewStatus:
    APPROVED_BY_AI = "APPROVED_BY_AI"
    NEEDS_CHANGES  = "NEEDS_CHANGES"
    BLOCKED        = "BLOCKED"
    ERROR          = "ERROR"


class ReviewRiskLevel:
    LOW    = "LOW"
    MEDIUM = "MEDIUM"
    HIGH   = "HIGH"


# Phase 8 config
REVIEW_REQUIRED     = True   # every story_implementation run triggers a review
REVIEW_BLOCKS_MERGE = True   # APPROVED_BY_AI required for auto-merge

# Prompt injection limits
MEMORY_MAX_BULLETS = 5
MEMORY_MAX_CHARS   = 1000


def categorize_execution_failure(
    test_status: str | None,
    merge_status: str | None,
    error_detail: str | None,
    current_step: str | None,
) -> str:
    """Return the most specific FailureCategory for a failed story_implementation run.

    Priority order: most observable signal first, then string patterns, then fallback.
    """
    err = (error_detail or "").lower()

    if test_status in ("FAILED", "ERROR"):
        return FailureCategory.TEST_FAILURE
    if any(p in err for p in ("interrupted by worker restart", "worker restarted mid-run")):
        return FailureCategory.WORKER_INTERRUPTED
    if "syntax error" in err or "syntaxerror" in err:
        return FailureCategory.SYNTAX_FAILURE
    if any(p in err for p in (
        "path traversal", "original text not found",
        "file not found", "no-op", "empty changes",
    )):
        return FailureCategory.APPLY_VALIDATION_FAILURE
    if merge_status == "FAILED" or "no open pr found" in err or "merge" in (current_step or "").lower():
        return FailureCategory.MERGE_FAILURE
    return FailureCategory.UNKNOWN


def categorize_planning_failure(
    error_detail: str | None,
    current_step: str | None,
) -> str:
    """Return the most specific FailureCategory for a failed epic_breakdown run."""
    err = (error_detail or "").lower()

    if "duplicate breakdown blocked" in err:
        return FailureCategory.DUPLICATE_BLOCKED
    if "rejected by user" in err:
        return FailureCategory.APPROVAL_REJECTED
    if "regeneration requested" in err:
        return FailureCategory.APPROVAL_REGENERATED
    if "jira creation failed" in err or (current_step or "") == "creating_jira_issues":
        return FailureCategory.JIRA_CREATION_FAILURE
    if "interrupted by worker restart" in err:
        return FailureCategory.WORKER_INTERRUPTED
    return FailureCategory.UNKNOWN
