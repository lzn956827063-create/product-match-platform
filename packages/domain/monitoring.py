"""Dedicated read-only metrics identity; safe labels and durable global gauges."""
import os
import resource
import secrets
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from sqlalchemy import select,func,text
from .db import transaction,engine
from .models import *
from . import telemetry
from .config import DATA_DIR


def token():
    filename=os.getenv('METRICS_TOKEN_FILE')
    return Path(filename).read_text().strip() if filename else os.getenv('METRICS_TOKEN','')


def authorized(header):
    expected=token()
    return bool(expected) and secrets.compare_digest(expected,header.removeprefix('Bearer '))


def render(include_durable=True):
    lines=telemetry.render()
    import sys
    rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)
    lines.extend([f'product_match_process_peak_rss_bytes {rss}',f'product_match_disk_free_bytes {shutil.disk_usage(DATA_DIR).free}'])
    pool=engine.pool
    if hasattr(pool,'checkedout'):lines.append(f'product_match_db_connections_checked_out {pool.checkedout()}')
    if include_durable:
        with transaction() as s:
            for cls,name in ((Run,'runs'),(ImportJob,'imports'),(BatchRelease,'releases'),(ReleaseArtifact,'release_artifacts'),(Delivery,'deliveries')):
                for state,count in s.execute(select(cls.status,func.count()).group_by(cls.status)):lines.append(f'product_match_{name}{{status="{state}"}} {count}')
            for cls,name,condition in ((Chunk,'expired_leases',(Chunk.status=='RUNNING')&(Chunk.lease_until<time.time())),(Outbox,'outbox_pending',Outbox.completed.is_(False)),(ArtifactDeletion,'cleanup_pending',ArtifactDeletion.deleted_at.is_(None))):
                lines.append(f'product_match_{name} {s.scalar(select(func.count()).select_from(cls).where(condition))}')
            from datetime import timedelta,timezone
            cutoff=(datetime.now(timezone.utc)-timedelta(hours=24)).isoformat()
            business=((Delivery,'receipt_overdue',(Delivery.status.in_(['RECEIVED','RECONCILE']))&(Delivery.receipt_due_at<=time.time())),(ReleaseArtifact,'release_files_overdue',(ReleaseArtifact.status!='SUCCEEDED')&(ReleaseArtifact.created_at<cutoff)),(ImpactTask,'impacts_open',ImpactTask.status.in_(['OPEN','NOTICE'])),(Item,'needs_info_overdue',(Item.status=='NEEDS_INFO')&(Item.created_at<cutoff)))
            for cls,name,condition in business:lines.append(f'product_match_{name} {s.scalar(select(func.count()).select_from(cls).where(condition))}')
            lines.append(f'product_match_receipt_owner_missing {s.scalar(select(func.count()).select_from(Integration).where(Integration.active.is_(True),Integration.reconciliation_owner_id.is_(None)))}')
            oldest=s.scalar(select(func.min(Outbox.created_at)).where(Outbox.completed.is_(False)))
            age=max(0,time.time()-datetime.fromisoformat(oldest).timestamp()) if oldest else 0
            lines.append(f'product_match_outbox_oldest_seconds {age}')
            oldest_run=s.scalar(select(func.min(Run.created_at)).where(Run.status=='QUEUED'))
            lines.append(f'product_match_queue_oldest_seconds {max(0,time.time()-datetime.fromisoformat(oldest_run).timestamp()) if oldest_run else 0}')
            recoveries=s.scalars(select(Run.timings)).all();lines.append(f'product_match_lease_recoveries_total {sum(t.get("lease_recoveries",0) for t in recoveries)}')
    return '\n'.join(lines)+'\n'


def start_server():
    port=int(os.getenv('WORKER_METRICS_PORT','0'))
    if not port:return
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path!='/metrics' or not authorized(self.headers.get('Authorization','')):self.send_error(403);return
            try:content=render(include_durable=False).encode()
            except Exception:self.send_error(503);return
            self.send_response(200);self.send_header('Content-Type','text/plain');self.end_headers();self.wfile.write(content)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('0.0.0.0',port),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
