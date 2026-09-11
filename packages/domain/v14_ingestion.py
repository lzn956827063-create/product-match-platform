"""Versioned supplier profiles and durable file-ingestion events."""
import csv
import io
import os
from pathlib import Path

from sqlalchemy import func, select

from . import storage
from .auth import scoped
from .db import now, uid
from .errors import Problem, require
from .ingest import MAX_BYTES, mapped_rows, parse_file
from .models import (
    Batch,
    BatchSupplier,
    File,
    ImportProfile,
    IngestionAttempt,
    IngestionEvent,
    IngestionSource,
    Supplier,
)
from .services import audit, create_revision
from ..matching.normalize import digest


SOURCE_KINDS = {"MANUAL", "DIRECTORY", "S3", "API"}


def directory_path(location):
    root = Path(os.getenv("INGESTION_ROOT", storage.DATA_DIR / "inbox")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    require(location and not Path(location).is_absolute(), 422, "INGESTION_LOCATION", "监控目录必须是接入根目录下的相对路径")
    path = (root / location).resolve()
    require(path.is_relative_to(root), 422, "INGESTION_LOCATION", "监控目录不能超出接入根目录")
    return path


def validate_source(source):
    require(source.kind in SOURCE_KINDS, 422, "INGESTION_KIND", "不支持此接入方式")
    if source.kind == "DIRECTORY":
        directory_path(source.location)
    if source.kind == "S3":
        prefix = source.location.strip("/") + "/"
        require(prefix.startswith(f"{source.org_id}/{source.supplier_id}/"), 422, "INGESTION_PREFIX", "对象收件箱前缀必须按组织和供应商隔离")
    if source.kind in {"MANUAL", "API"}:
        require(not source.location, 422, "INGESTION_LOCATION", "页面或 API 接入不需要配置位置")


def latest_profile(s, source):
    profile = s.scalar(
        select(ImportProfile)
        .where(
            ImportProfile.org_id == source.org_id,
            ImportProfile.supplier_id == source.supplier_id,
            ImportProfile.status == "PUBLISHED",
        )
        .order_by(ImportProfile.effective_from.desc(), ImportProfile.number.desc())
    )
    require(profile is not None, 409, "IMPORT_PROFILE_REQUIRED", "供应商尚无已发布的导入配置")
    return profile


def profile_config(profile, file_id):
    return {
        "file_id": file_id,
        "sheet": profile.sheet,
        "header_row": profile.header_row,
        "mapping": profile.mapping,
        "transformations": profile.transformations,
        "validations": profile.validations,
        "exclude_rows": [],
    }


def create_file(s, org_id, filename, content, encoding="utf-8"):
    require(len(content) <= MAX_BYTES, 413, "FILE_TOO_LARGE", "文件不能超过 20MB")
    suffix = Path(filename).suffix.lower()
    require(suffix in (".csv", ".xlsx"), 422, "INVALID_FILE", "仅支持 CSV 和 XLSX 文件")
    sha = digest(content)
    obj = File(
        id=uid(),
        org_id=org_id,
        name=Path(filename).name[:250],
        object_key=storage.quota_put(org_id, content, suffix[1:], s=s),
        sha256=sha,
        size=len(content),
        encoding=encoding,
        sheets=[],
    )
    s.add(obj)
    s.flush()
    return obj


def _attempt(s, event, status, error=None, detail=None):
    number = (s.scalar(select(func.max(IngestionAttempt.number)).where(IngestionAttempt.org_id == event.org_id, IngestionAttempt.event_id == event.id)) or 0) + 1
    row = IngestionAttempt(
        org_id=event.org_id,
        event_id=event.id,
        number=number,
        stage=event.stage,
        status=status,
        error=error,
        detail=detail or {},
    )
    s.add(row)
    event.version += 1
    return row


def process_event(s, ctx, event):
    source = scoped(s, IngestionSource, event.source_id, ctx, True)
    profile = scoped(s, ImportProfile, event.profile_id, ctx)
    require(source.status == "ACTIVE", 409, "INGESTION_SOURCE_PAUSED", "接入源已停用")
    require(profile.status == "PUBLISHED" and profile.supplier_id == source.supplier_id, 409, "IMPORT_PROFILE_STATE", "接入事件必须绑定本供应商已发布的配置版本")
    file = scoped(s, File, event.file_id, ctx)
    raw = storage.read(file.object_key)
    require(digest(raw) == event.object_sha256 == file.sha256, 409, "FILE_HASH_MISMATCH", "接入文件摘要不一致")
    event.stage = "VALIDATION"
    try:
        parsed = parse_file(raw, file.name, profile.encoding)
        rows = mapped_rows(parsed, profile.sheet, profile.header_row, profile.mapping, False, profile.transformations, profile.validations)
    except Problem as exc:
        event.status, event.last_error = "FAILED", exc.message
        event.summary = {"code": exc.code, "total": 0, "rejected": 0}
        _attempt(s, event, "FAILED", exc.code, {"message": exc.message})
        source.consecutive_failures += 1
        source.last_checked_at = now()
        return event
    errors = [r for r in rows if any(i["level"] == "error" for i in r["issues"])]
    warnings = [r for r in rows if any(i["level"] == "warning" for i in r["issues"])]
    event.summary = {
        "total": len(rows),
        "accepted": len(rows) - len(errors),
        "rejected": len(errors),
        "warnings": len(warnings),
        "field_mapping": profile.mapping,
        "representative_errors": [
            {"row_no": r["row_no"], "messages": [i["message"] for i in r["issues"] if i["level"] == "error"]}
            for r in errors[:20]
        ],
    }
    source.last_checked_at = now()
    if errors:
        event.status, event.last_error = "REJECTED", "文件存在行级错误，未生成业务批次"
        _attempt(s, event, "REJECTED", "ROW_VALIDATION", {"rejected": len(errors)})
        source.consecutive_failures += 1
        return event
    supplier = scoped(s, Supplier, source.supplier_id, ctx)
    batch = Batch(
        id=uid(),
        org_id=ctx.org_id,
        name=f"{supplier.name} · {Path(event.filename).stem}"[:200],
        supplier=supplier.name,
        created_by=event.created_by or source.created_by,
    )
    s.add(batch)
    s.flush()
    revision = create_revision(s, ctx, batch, profile_config(profile, file.id), {"ingestion_event_id": event.id, "profile_id": profile.id})
    s.add(BatchSupplier(org_id=ctx.org_id, batch_id=batch.id, supplier_id=supplier.id, actor_id=event.created_by or source.created_by, reason="由接入事件自动绑定"))
    event.batch_id, event.revision_id = batch.id, revision.id
    event.stage, event.status, event.last_error = "PERSISTENCE", "READY", None
    source.last_success_at, source.consecutive_failures = now(), 0
    _attempt(s, event, "SUCCEEDED", detail={"batch_id": batch.id, "revision_id": revision.id})
    audit(s, ctx, "ingestion.ready", event.id, {"source_id": source.id, "profile_id": profile.id, "sha256": event.object_sha256})
    return event


def receive_file(s, ctx, source, profile, file, external_key, actor_id=None):
    require(source.org_id == ctx.org_id and profile.org_id == ctx.org_id and file.org_id == ctx.org_id, 404, "NOT_FOUND", "资源不存在或无权访问")
    require(profile.supplier_id == source.supplier_id, 422, "PROFILE_SUPPLIER", "导入配置与接入源供应商不一致")
    old_key = s.scalar(select(IngestionEvent).where(IngestionEvent.org_id == ctx.org_id, IngestionEvent.source_id == source.id, IngestionEvent.external_key == external_key))
    if old_key:
        require(old_key.object_sha256 == file.sha256 and old_key.profile_id == profile.id, 409, "INGESTION_REPLAY_CONFLICT", "此事件号已用于不同文件或配置版本")
        return old_key, True
    duplicate = s.scalar(select(IngestionEvent).where(IngestionEvent.org_id == ctx.org_id, IngestionEvent.source_id == source.id, IngestionEvent.profile_id == profile.id, IngestionEvent.object_sha256 == file.sha256))
    if duplicate:
        return duplicate, True
    event = IngestionEvent(
        id=uid(),
        org_id=ctx.org_id,
        source_id=source.id,
        profile_id=profile.id,
        external_key=external_key,
        file_id=file.id,
        object_sha256=file.sha256,
        filename=file.name,
        created_by=actor_id,
    )
    s.add(event)
    s.flush()
    return process_event(s, ctx, event), False


def receive_bytes(s, ctx, source, profile, filename, content, external_key, actor_id=None):
    sha = digest(content)
    old_key = s.scalar(select(IngestionEvent).where(IngestionEvent.org_id == ctx.org_id, IngestionEvent.source_id == source.id, IngestionEvent.external_key == external_key))
    if old_key:
        require(old_key.object_sha256 == sha and old_key.profile_id == profile.id, 409, "INGESTION_REPLAY_CONFLICT", "此事件号已用于不同文件或配置版本")
        return old_key, True
    duplicate = s.scalar(select(IngestionEvent).where(IngestionEvent.org_id == ctx.org_id, IngestionEvent.source_id == source.id, IngestionEvent.profile_id == profile.id, IngestionEvent.object_sha256 == sha))
    if duplicate:
        return duplicate, True
    return receive_file(s, ctx, source, profile, create_file(s, ctx.org_id, filename, content, profile.encoding), external_key, actor_id)


def replay(s, ctx, event):
    require(event.status == "FAILED", 409, "INGESTION_REPLAY_STATE", "只有系统处理失败的事件可以原事件号重放；行级错误请修正文件后重新提交")
    return process_event(s, ctx, event)


def error_csv(event):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["原始行号", "问题原因"])
    for row in event.summary.get("representative_errors", []):
        writer.writerow([row["row_no"], "；".join(row["messages"])])
    return output.getvalue().encode("utf-8-sig")
