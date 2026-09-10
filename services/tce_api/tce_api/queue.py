from __future__ import annotations

from rq import Queue, Retry

from .cache_clients import get_redis_client
from .config import get_settings


def get_queue(name: str | None = None) -> Queue:
    settings = get_settings()
    resolved_name = name or settings.queue_default_name
    redis_conn = get_redis_client(settings.redis_url)
    if redis_conn is None:
        raise RuntimeError("redis client unavailable for queue operations")
    return Queue(resolved_name, connection=redis_conn)


def get_queue_depth(name: str | None = None) -> int:
    queue = get_queue(name)
    return int(queue.count)


def queue_name_for_job(job_name: str) -> str:
    settings = get_settings()
    if job_name in {
        "tce_worker.jobs.embedding.run",
        "tce_worker.jobs.embed_observations.run",
        "tce_worker.jobs.episode_extraction.run",
    }:
        return settings.queue_embeddings_name
    if job_name in {
        "tce_worker.jobs.compaction.run",
        "tce_worker.jobs.patterns.run",
        "tce_worker.jobs.validation.run",
        "tce_worker.jobs.workflow.run",
        "tce_worker.jobs.semantic_consolidation.run",
        "tce_worker.jobs.reflection.run",
    }:
        return settings.queue_patterns_name
    if job_name in {"tce_worker.jobs.archive_events.run"}:
        return settings.queue_lifecycle_name
    if job_name in {
        "tce_worker.jobs.decision_extraction.run",
        "tce_worker.jobs.planning.run",
        "tce_worker.jobs.dream_synthesis.run",
    }:
        return settings.queue_default_name
    return settings.queue_default_name


def enqueue_job(job_name: str, *args: object, queue_name: str | None = None) -> str | None:
    resolved_queue = queue_name or queue_name_for_job(job_name)
    queue = get_queue(resolved_queue)
    job = queue.enqueue(job_name, *args, retry=Retry(max=3, interval=[10, 30, 120]))
    return str(job.id) if job and job.id else None
