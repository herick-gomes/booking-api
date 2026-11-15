"""Recoverable dispatch: a crashed enqueue/delivery is retried after the lease."""

import logging
import time
from datetime import timedelta

from redis import RedisError
from rq import Queue, Retry
from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError

from .auth import redis_connection
from .db import SessionLocal
from .domain import now
from .models import Outbox

logger = logging.getLogger(__name__)


def dispatch_once():
    queue = Queue("booking", connection=redis_connection())
    count = 0
    with SessionLocal.begin() as db:
        events = db.scalars(
            select(Outbox)
            .where(
                Outbox.status == "pending",
                Outbox.due_at <= now(),
                or_(Outbox.lease_until.is_(None), Outbox.lease_until < now()),
            )
            .order_by(Outbox.due_at, Outbox.id)
            .limit(100)
            .with_for_update(skip_locked=True)
        ).all()
        for event in events:
            # Queue first. If SQL commit fails, duplicate enqueue is harmless.
            queue.enqueue(
                "app.jobs.deliver",
                str(event.id),
                retry=Retry(max=3, interval=[10, 30, 60]),
                job_timeout=60,
                result_ttl=86400,
                failure_ttl=604800,
            )
            event.lease_until = now() + timedelta(minutes=5)
            event.dispatch_count += 1
            count += 1
    return count


def main():
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            logger.info("Queued %s events", dispatch_once())
        except (RedisError, SQLAlchemyError):
            logger.exception("Dispatch unavailable; pending events remain recoverable")
        time.sleep(30)


if __name__ == "__main__":
    main()
