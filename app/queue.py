import os
import json
import logging
import redis as redislib

logger = logging.getLogger("orchestrator")

QUEUE_NAME = "workflow_queue"
_redis = None


def get_redis():
    global _redis
    if _redis is None:
        _redis = redislib.from_url(os.environ["REDIS_URL"])
    return _redis


def enqueue(run_id: int, workflow_type: str, issue_key: str, issue_type: str, summary: str):
    job = json.dumps({
        "run_id": run_id,
        "workflow_type": workflow_type,
        "issue_key": issue_key,
        "issue_type": issue_type,
        "summary": summary,
    })
    get_redis().lpush(QUEUE_NAME, job)
    logger.info("Job enqueued: %s (run_id=%s)", workflow_type, run_id)


def dequeue(timeout: int = 5):
    result = get_redis().brpop(QUEUE_NAME, timeout=timeout)
    if result:
        _, data = result
        return json.loads(data)
    return None


def enqueue_onboarding_job(onboarding_run_id: int, repo_slug: str, base_branch: str):
    job = json.dumps({
        "run_id": onboarding_run_id,
        "workflow_type": "project_onboarding",
        "repo_slug": repo_slug,
        "base_branch": base_branch,
    })
    get_redis().lpush(QUEUE_NAME, job)
    logger.info("Onboarding job enqueued: repo_slug=%s (run_id=%s)", repo_slug, onboarding_run_id)


def queue_length() -> int:
    return get_redis().llen(QUEUE_NAME)
