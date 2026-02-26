from __future__ import annotations

from redis import Redis
from rq import Connection, Worker

from .config import get_settings
from .otel import setup_otel


def main() -> None:
    settings = get_settings()
    setup_otel("tce-worker")
    redis_conn = Redis.from_url(settings.redis_url)
    queue_names = settings.listen_queues
    with Connection(redis_conn):
        worker = Worker(queue_names)
        worker.work(with_scheduler=bool(settings.worker_enable_scheduler))


if __name__ == "__main__":
    main()
