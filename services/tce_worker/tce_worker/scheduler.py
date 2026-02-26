from __future__ import annotations

from datetime import UTC, datetime, timedelta

from redis import Redis
from rq import Queue, Retry

from .config import get_settings


def enqueue_maintenance_jobs() -> None:
    settings = get_settings()
    redis_conn = Redis.from_url(settings.redis_url)
    patterns_queue_name = getattr(settings, "queue_patterns_name", "tce-default")
    lifecycle_queue_name = getattr(settings, "queue_lifecycle_name", "tce-default")
    patterns_queue = Queue(patterns_queue_name, connection=redis_conn)
    lifecycle_queue = Queue(lifecycle_queue_name, connection=redis_conn)
    retry = Retry(max=settings.worker_retry_max, interval=settings.retry_intervals)

    now = datetime.now(tz=UTC)
    window_start = (now - timedelta(days=7)).isoformat()
    window_end = now.isoformat()

    patterns_queue.enqueue("tce_worker.jobs.compaction.run", window_start, window_end, retry=retry)
    if settings.semantic_consolidation_enabled:
        patterns_queue.enqueue(
            "tce_worker.jobs.semantic_consolidation.run",
            None,
            None,
            settings.semantic_consolidation_lookback_days,
            retry=retry,
        )
    for domain in ["coding", "planning", "ops", "research"]:
        patterns_queue.enqueue("tce_worker.jobs.patterns.run", domain, 30, retry=retry)
    if settings.event_lifecycle_enabled:
        lifecycle_queue.enqueue(
            "tce_worker.jobs.archive_events.run",
            settings.event_retention_days,
            settings.lifecycle_dry_run,
            retry=retry,
        )
    lifecycle_queue.enqueue(
        "tce_worker.jobs.graph_health.run",
        retry=retry,
    )
    if bool(getattr(settings, "qdrant_enabled", False)) and bool(getattr(settings, "qdrant_sync_enabled", True)):
        lifecycle_queue.enqueue(
            "tce_worker.jobs.qdrant_sync.run",
            int(getattr(settings, "qdrant_sync_max_batches", 10)),
            int(getattr(settings, "qdrant_sync_batch_size", 500)),
            retry=retry,
        )


if __name__ == "__main__":
    enqueue_maintenance_jobs()
