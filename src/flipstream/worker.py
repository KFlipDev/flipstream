"""The RQ worker: `python -m flipstream.worker`.

One worker process, so one job at a time. The scheduler runs inside it, which
RQ needs for retries with a wait between tries. On SIGTERM (docker stop, a
deploy) RQ finishes the running job before it exits; compose gives it time
with stop_grace_period.
"""

import logging

from redis import Redis
from rq import Worker

from .config import Settings
from .jobs import QUEUE


def main() -> None:
    """Connect to Redis and work the queue until the process is stopped."""
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt)
    settings = Settings.from_env()
    conn = Redis.from_url(settings.redis_url)
    Worker([QUEUE], connection=conn).work(with_scheduler=True)


if __name__ == "__main__":
    main()
