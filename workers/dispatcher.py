import logging
import time
from sqlalchemy import or_, select
from packages.domain.config import QUEUE_MODE
from packages.domain.db import initialize, transaction
from packages.domain.models import Outbox
from workers.jobs import process_export, process_run


def dispatch_once():
    with transaction() as s:
        events = [(e.id, e.kind, e.resource_id) for e in s.scalars(select(Outbox).where(Outbox.completed.is_(False), or_(Outbox.published_at.is_(None), Outbox.published_at < time.time()-30)).order_by(Outbox.created_at).limit(20))]
    for event_id, kind, resource_id in events:
        if QUEUE_MODE == "celery":
            from workers.tasks import execute
            execute.apply_async(args=[kind, resource_id], task_id=event_id)
        else:
            (process_run if kind == "match" else process_export)(resource_id)
        # Crash here is safe: the same event will be delivered again.
        with transaction(write=True) as s:
            event = s.get(Outbox, event_id)
            event.published_at, event.attempts = time.time(), event.attempts+1
    return len(events)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    initialize()
    while True:
        try:
            dispatch_once()
        except Exception as exc:
            logging.getLogger("dispatcher").error("dispatch_failed error_type=%s; durable outbox retained", type(exc).__name__)
        time.sleep(2)


if __name__ == "__main__":
    main()
