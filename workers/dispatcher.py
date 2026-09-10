import logging
import time
from sqlalchemy import func, or_, select
from packages.domain.config import QUEUE_MODE
from packages.domain.db import initialize, transaction
from packages.domain.models import Outbox
from workers.routing import execute_job


def dispatch_once(pools=None, inflight=None):
    with transaction() as s:
        # Take one event per organization/kind before second events, so an old
        # backlog cannot hide another organization's runnable work or short jobs.
        ranked=select(Outbox.id,Outbox.kind,Outbox.resource_id,Outbox.created_at,func.row_number().over(partition_by=(Outbox.org_id,Outbox.kind),order_by=(Outbox.created_at,Outbox.id)).label('position')).where(Outbox.completed.is_(False),or_(Outbox.published_at.is_(None),Outbox.published_at<time.time()-30)).subquery()
        events=list(s.execute(select(ranked.c.id,ranked.c.kind,ranked.c.resource_id).order_by(ranked.c.position,ranked.c.created_at,ranked.c.id).limit(100)))
    for event_id, kind, resource_id in events:
        if pools and event_id in inflight and not inflight[event_id].done():continue
        if QUEUE_MODE == "celery":
            from workers.tasks import execute
            execute.apply_async(args=[kind, resource_id], task_id=event_id, queue="match" if kind=="match" else "short")
        elif pools:
            pool=pools["match" if kind=="match" else "short"]
            active=sum(not f.done() for k,f in inflight.items() if getattr(f,"job_kind",None)==("match" if kind=="match" else "short"))
            if active>=2:continue
            future=pool.submit(execute_job,kind,resource_id);future.job_kind="match" if kind=="match" else "short";inflight[event_id]=future
        else:
            execute_job(kind, resource_id)
        # Crash here is safe: the same event will be delivered again.
        with transaction(write=True) as s:
            event = s.get(Outbox, event_id)
            event.published_at, event.attempts = time.time(), event.attempts+1
    return len(events)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    initialize()
    from packages.domain.monitoring import start_server
    start_server()
    from concurrent.futures import ThreadPoolExecutor
    pools={"match":ThreadPoolExecutor(max_workers=2),"short":ThreadPoolExecutor(max_workers=2)} if QUEUE_MODE!="celery" else None
    inflight={}
    cleanup_at = 0
    receipts_at = 0
    while True:
        try:
            if time.time()-cleanup_at > 3600:
                from workers.maintenance import schedule_cleanup
                schedule_cleanup(); cleanup_at=time.time()
            if time.time()-receipts_at >= 30:
                from workers.deliveries import scan_receipts
                scan_receipts()
                from workers.quality import schedule_quality
                schedule_quality();receipts_at=time.time()
            dispatch_once(pools,inflight)
            for ident,future in list(inflight.items()):
                if future.done():
                    try:future.result()
                    except Exception as exc:logging.getLogger("dispatcher").error("job_failed event_id=%s error_type=%s",ident,type(exc).__name__)
                    del inflight[ident]
        except Exception as exc:
            logging.getLogger("dispatcher").error("dispatch_failed error_type=%s; durable outbox retained", type(exc).__name__)
        time.sleep(2)


if __name__ == "__main__":
    main()
