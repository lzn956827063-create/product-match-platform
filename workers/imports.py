import json
import logging
import time
import threading
from sqlalchemy import select
from packages.domain import storage
from packages.domain.db import transaction
from packages.domain.errors import Problem
from packages.domain.models import File, ImportJob, Outbox
from packages.domain.imports import parse_key, read_result
from packages.domain.ingest import mapped_rows, parse_file
from packages.matching.normalize import digest


def process_import(ident):
    with transaction(write=True) as s:
        job = s.scalar(select(ImportJob).where(ImportJob.id == ident).with_for_update())
        if not job or job.status in ('MAPPING_REQUIRED', 'READY', 'FAILED') or job.lease_until > time.time():
            return
        job.lease_until, job.fence_token, job.attempts = time.time()+120, job.fence_token+1, job.attempts+1
        job.status = 'PARSING' if job.kind == 'parse' else 'VALIDATING'
        job.progress = 10
        token, org, kind, config = job.fence_token, job.org_id, job.kind, job.config
        file = s.scalar(select(File).where(File.org_id == org, File.id == job.file_id))
        raw_info = (file.object_key, file.name, file.encoding, file.sha256)
        parent = s.scalar(select(ImportJob).where(ImportJob.org_id == org, ImportJob.kind == 'parse', ImportJob.cache_key == parse_key(file))) if kind != 'parse' else None
        parent_info = (parent.result_key, parent.result_hash, parent.status) if parent else None
    started = time.perf_counter()
    stopped=threading.Event()
    def heartbeat():
        while not stopped.wait(20):
            try:
                with transaction(write=True) as s:
                    current=s.scalar(select(ImportJob).where(ImportJob.id==ident,ImportJob.org_id==org).with_for_update())
                    if current.fence_token!=token or current.lease_until<=time.time():return
                    current.lease_until=time.time()+120
            except Exception:return
    thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
    try:
        if kind == 'parse':
            raw = storage.read(raw_info[0])
            if digest(raw) != raw_info[3]: raise ValueError('FILE_HASH_MISMATCH')
            result = parse_file(raw, raw_info[1], raw_info[2])
            summary = {'sheets': list(result)}
            status = 'MAPPING_REQUIRED'
        else:
            if not parent_info or parent_info[2] != 'MAPPING_REQUIRED': raise ValueError('PARSE_REQUIRED')
            raw = storage.read(parent_info[0])
            if digest(raw) != parent_info[1]: raise ValueError('IMPORT_CACHE_CORRUPT')
            result = mapped_rows(json.loads(raw), config['sheet'], config['header_row'], config['mapping'], config.get('catalog', False))
            errors = [r['row_no'] for r in result if any(i['level']=='error' for i in r['issues'])]
            summary = {'total': len(result), 'valid': len(result)-len(errors), 'error_rows': errors, 'warning_count': sum(any(i['level']=='warning' for i in r['issues']) for r in result)}
            status = 'READY'
        content = json.dumps(result, ensure_ascii=False).encode()
        key = storage.quota_put(org, content, 'json')
        with transaction(write=True) as s:
            job = s.scalar(select(ImportJob).where(ImportJob.id == ident, ImportJob.org_id == org).with_for_update())
            if job.fence_token != token or job.lease_until <= time.time():
                # A deterministic object may already be referenced by the winning worker.
                return
            job.result_key, job.result_hash, job.summary = key, digest(content), {**summary, 'seconds': time.perf_counter()-started}
            job.status, job.progress, job.lease_until, job.error = status, 100, 0, None
            if kind == 'parse': s.get(File, job.file_id).sheets = summary['sheets']
            s.scalar(select(Outbox).where(Outbox.org_id==org, Outbox.event_key=='import:'+ident)).completed = True
    except Exception as exc:
        with transaction(write=True) as s:
            job = s.scalar(select(ImportJob).where(ImportJob.id==ident, ImportJob.org_id==org).with_for_update())
            if job.fence_token == token:
                job.status, job.lease_until = 'FAILED', 0
                job.error = exc.code if isinstance(exc, Problem) else type(exc).__name__
                job.summary = {'message': exc.message if isinstance(exc, Problem) else '导入处理失败，可重试或检查文件'}
                s.scalar(select(Outbox).where(Outbox.org_id==org, Outbox.event_key=='import:'+ident)).completed = True
        logging.getLogger('imports').warning('import_failed job_id=%s kind=%s error_type=%s', ident, kind, type(exc).__name__)

    finally:
        stopped.set();thread.join(timeout=1)
