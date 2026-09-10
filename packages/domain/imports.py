"""Durable parsing/validation results cached by tenant, content and configuration."""
import json
from sqlalchemy import select
from . import storage
from .auth import scoped
from .errors import require
from .ingest import mapped_rows, parse_file
from .models import File, ImportJob, Outbox
from .db import uid
from ..matching.normalize import RULE_VERSION, digest

PARSER_VERSION = 'tabular-1.1.0'


def parse_key(file):
    return digest({'sha256': file.sha256, 'encoding': file.encoding, 'parser': PARSER_VERSION, 'format': file.name.rsplit('.', 1)[-1].lower()})


def validation_key(file, config, catalog):
    return digest({'parse': parse_key(file), 'sheet': config['sheet'], 'header_row': config['header_row'], 'mapping': config['mapping'], 'catalog': catalog, 'rule': RULE_VERSION})


def request_job(s, c, file, kind, config=None):
    config = config or {}
    cache_key = parse_key(file) if kind == 'parse' else validation_key(file, config, config.get('catalog', False))
    old = s.scalar(select(ImportJob).where(ImportJob.org_id == c.org_id, ImportJob.kind == kind, ImportJob.cache_key == cache_key))
    if old:
        return old
    obj = ImportJob(id=uid(), org_id=c.org_id, file_id=file.id, kind=kind, cache_key=cache_key, config=config)
    s.add(obj); s.flush()
    s.add(Outbox(org_id=c.org_id, event_key='import:'+obj.id, kind='import', resource_id=obj.id))
    s.flush()
    return obj


def read_result(job):
    raw = storage.read(job.result_key)
    require(digest(raw) == job.result_hash, 409, 'IMPORT_CACHE_CORRUPT', '导入缓存校验失败，请重新解析')
    return json.loads(raw)


def parsed_file(s, c, file, allow_sync=False):
    job = s.scalar(select(ImportJob).where(ImportJob.org_id == c.org_id, ImportJob.kind == 'parse', ImportJob.cache_key == parse_key(file)))
    if job:
        require(job.status == 'MAPPING_REQUIRED', 409, 'IMPORT_NOT_READY', '文件正在解析或解析失败，请查看导入状态')
        return read_result(job)
    require(allow_sync, 409, 'IMPORT_NOT_READY', '请先提交文件解析任务')
    # Compatibility for pre-1.1 files; the new upload workflow never enters this branch.
    raw = storage.read(file.object_key)
    require(digest(raw) == file.sha256, 409, 'FILE_HASH_MISMATCH', '原始文件校验失败')
    return parse_file(raw, file.name, file.encoding)


def cached_rows(s, c, config, catalog=False):
    file = scoped(s, File, config['file_id'], c)
    job = s.scalar(select(ImportJob).where(ImportJob.org_id == c.org_id, ImportJob.kind == 'validate', ImportJob.cache_key == validation_key(file, config, catalog)))
    if job:
        require(job.status == 'READY', 409, 'IMPORT_NOT_READY', '字段校验尚未完成，请查看导入状态')
        return file, read_result(job)
    parsed = parsed_file(s, c, file, allow_sync=True)
    return file, mapped_rows(parsed, config['sheet'], config['header_row'], config['mapping'], catalog)
