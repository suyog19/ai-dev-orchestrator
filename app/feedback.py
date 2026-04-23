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


# Prompt injection limits
MEMORY_MAX_BULLETS = 5
MEMORY_MAX_CHARS   = 1000
