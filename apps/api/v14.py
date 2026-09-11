"""v1.4 ingestion, evaluation, threshold and learning-feedback routes."""
import os
import time
from pathlib import Path
from typing import Literal

from fastapi import Depends, File as UploadParam, Header, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import Field
from sqlalchemy import func, select

from .schemas import Input, Reason, VersionInput
from packages.domain import integrations, quotas, storage, v14_ingestion, v14_learning
from packages.domain.auth import Context, scoped
from packages.domain.db import now, uid
from packages.domain.errors import require
from packages.domain.ingest import FIELDS, mapped_rows, parse_file
from packages.domain.models import *
from packages.domain.services import audit
from packages.matching.normalize import digest


class SourceInput(Input):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["MANUAL", "DIRECTORY", "S3", "API"]
    supplier_id: str
    location: str = Field(default="", max_length=1000)
    config: dict = Field(default_factory=dict)


class SourceState(Input):
    active: bool


class ProfileInput(Input):
    supplier_id: str
    name: str = Field(min_length=1, max_length=200)
    file_type: Literal["csv", "xlsx"]
    sheet: str = Field(default="CSV", min_length=1, max_length=200)
    header_row: int = Field(default=1, ge=1, le=100)
    encoding: Literal["utf-8", "gb18030"] = "utf-8"
    mapping: dict[str, str]
    transformations: dict[str, dict] = Field(default_factory=dict)
    validations: dict = Field(default_factory=dict)
    change_note: str = Field(min_length=2, max_length=2000)


class DryRunInput(Input):
    file_id: str


class EventInput(Input):
    source_id: str
    profile_id: str | None = None
    file_id: str
    external_key: str = Field(min_length=1, max_length=200)


class EvaluationInput(Input):
    name: str = Field(min_length=1, max_length=200)
    dataset_id: str
    model_id: str | None = None
    baseline_name: str = Field(min_length=1, max_length=200)
    scope: Literal["simulation", "public", "authorized"]
    config: dict = Field(default_factory=dict)
    metrics: dict
    slices: dict = Field(default_factory=dict)
    confidence_intervals: dict = Field(default_factory=dict)
    regressions: list[dict] = Field(default_factory=list, max_length=10000)


class ThresholdInput(Input):
    name: str = Field(min_length=1, max_length=200)
    evaluation_id: str
    calibration_method: Literal["platt", "isotonic"]
    thresholds: dict


class ThresholdApproval(Reason):
    base_policy_id: str
    activate_default: bool = False


class LabelUpdate(Input):
    error_type: Literal[
        "candidate_not_recalled",
        "ranking_error",
        "catalog_missing",
        "normalization_error",
        "key_field_conflict",
        "insufficient_source",
        "duplicate_product",
        "annotation_dispute",
    ]
    status: Literal["ELIGIBLE", "DISPUTED", "HOLD"]


def install(app, db, ctx, data, page):
    P = "/api/v1"

    def source_data(row):
        return data(row, ("secret_cipher",))

    def event_data(s, row, duplicate=False):
        source = s.get(IngestionSource, row.source_id)
        profile = s.get(ImportProfile, row.profile_id)
        return {
            **data(row),
            "source_name": source.name if source else None,
            "profile_name": f"{profile.name} v{profile.number}" if profile else None,
            "duplicate": duplicate,
        }

    @app.get(P + "/ingestion-sources")
    def sources(s=Depends(db), c=Depends(ctx)):
        rows = s.scalars(select(IngestionSource).where(IngestionSource.org_id == c.org_id).order_by(IngestionSource.created_at.desc())).all()
        counts = dict(s.execute(select(IngestionEvent.source_id, func.count()).where(IngestionEvent.org_id == c.org_id).group_by(IngestionEvent.source_id)).all())
        return {"items": [{**source_data(row), "event_count": counts.get(row.id, 0)} for row in rows]}

    @app.post(P + "/ingestion-sources", status_code=201)
    def source_create(body: SourceInput, s=Depends(db), c=Depends(ctx)):
        c.permit("operator", "admin", "integration_manager")
        scoped(s, Supplier, body.supplier_id, c)
        require(not s.scalar(select(IngestionSource.id).where(IngestionSource.org_id == c.org_id, IngestionSource.name == body.name)), 409, "INGESTION_SOURCE_NAME", "接入源名称已存在")
        require(set(body.config) <= {"archive_after_success", "stable_seconds", "suffixes"}, 422, "INGESTION_CONFIG", "接入配置包含不支持的字段")
        row = IngestionSource(id=uid(), org_id=c.org_id, created_by=c.user_id, status="ACTIVE", **body.model_dump())
        secret = None
        if row.kind == "API":
            import secrets
            secret = secrets.token_urlsafe(32)
            row.secret_cipher = integrations.cipher().encrypt(secret.encode()).decode()
        v14_ingestion.validate_source(row)
        s.add(row)
        s.flush()
        audit(s, c, "ingestion_source.create", row.id, {"kind": row.kind, "supplier_id": row.supplier_id})
        return {**source_data(row), **({"signing_secret": secret} if secret else {})}

    @app.put(P + "/ingestion-sources/{ident}")
    def source_state(ident: str, body: SourceState, s=Depends(db), c=Depends(ctx)):
        c.permit("operator", "admin", "integration_manager")
        row = scoped(s, IngestionSource, ident, c, True)
        row.status = "ACTIVE" if body.active else "PAUSED"
        audit(s, c, "ingestion_source.state", row.id, {"status": row.status})
        return source_data(row)

    @app.post(P + "/ingestion-sources/{ident}/check")
    def source_check(ident: str, s=Depends(db), c=Depends(ctx)):
        c.permit("operator", "admin", "integration_manager")
        row = scoped(s, IngestionSource, ident, c, True)
        ok, detail = True, "配置有效"
        try:
            if row.kind == "DIRECTORY":
                path = v14_ingestion.directory_path(row.location)
                ok, detail = path.is_dir(), f"监控目录 {'可读取' if path.is_dir() else '尚不存在'}"
            elif row.kind == "S3":
                result = storage.client().list_objects_v2(Bucket=os.getenv("S3_BUCKET", "product-match"), Prefix=row.location.strip("/") + "/", MaxKeys=1)
                detail = f"对象收件箱可访问，当前探测到 {result.get('KeyCount', 0)} 个对象"
            elif row.kind == "API":
                detail = "签名 API 已启用，时间窗为 5 分钟"
            else:
                detail = "页面上传入口可用"
        except Exception as exc:
            ok, detail = False, f"连通性检查失败：{type(exc).__name__}"
        row.last_checked_at = now()
        row.consecutive_failures = 0 if ok else row.consecutive_failures + 1
        return {"ok": ok, "detail": detail, "checked_at": row.last_checked_at}

    @app.get(P + "/import-profiles")
    def profiles(supplier_id: str = "", s=Depends(db), c=Depends(ctx)):
        query = select(ImportProfile).where(ImportProfile.org_id == c.org_id)
        if supplier_id:
            query = query.where(ImportProfile.supplier_id == supplier_id)
        rows = s.scalars(query.order_by(ImportProfile.created_at.desc())).all()
        return {"items": [data(row) for row in rows], "fields": FIELDS}

    @app.post(P + "/import-profiles", status_code=201)
    def profile_create(body: ProfileInput, s=Depends(db), c=Depends(ctx)):
        c.permit("operator", "admin")
        scoped(s, Supplier, body.supplier_id, c)
        require(body.mapping and set(body.mapping) <= set(FIELDS) and len(set(body.mapping.values())) == len(body.mapping), 422, "INVALID_MAPPING", "字段映射包含未知字段或重复来源列")
        latest = s.scalar(select(ImportProfile).where(ImportProfile.org_id == c.org_id, ImportProfile.supplier_id == body.supplier_id, ImportProfile.name == body.name).order_by(ImportProfile.number.desc()))
        row = ImportProfile(id=uid(), org_id=c.org_id, number=(latest.number if latest else 0) + 1, created_by=c.user_id, status="DRAFT", **body.model_dump())
        s.add(row)
        s.flush()
        audit(s, c, "import_profile.create", row.id, {"number": row.number, "supplier_id": row.supplier_id})
        return data(row)

    @app.post(P + "/import-profiles/{ident}/dry-run")
    def profile_dry_run(ident: str, body: DryRunInput, s=Depends(db), c=Depends(ctx)):
        c.permit("operator", "admin")
        profile = scoped(s, ImportProfile, ident, c)
        file = scoped(s, File, body.file_id, c)
        raw = storage.read(file.object_key)
        require(digest(raw) == file.sha256, 409, "FILE_HASH_MISMATCH", "原文件摘要校验失败")
        require(Path(file.name).suffix.lower() == "." + profile.file_type, 422, "PROFILE_FILE_TYPE", "文件类型与导入配置不一致")
        rows = mapped_rows(parse_file(raw, file.name, profile.encoding), profile.sheet, profile.header_row, profile.mapping, False, profile.transformations, profile.validations)
        errors = [row for row in rows if any(issue["level"] == "error" for issue in row["issues"])]
        warnings = [row for row in rows if any(issue["level"] == "warning" for issue in row["issues"])]
        return {
            "total": len(rows),
            "accepted": len(rows) - len(errors),
            "rejected": len(errors),
            "warnings": len(warnings),
            "field_mapping": profile.mapping,
            "rows": sorted(rows, key=lambda row: (not any(issue["level"] == "error" for issue in row["issues"]), row["row_no"]))[:50],
        }

    @app.post(P + "/import-profiles/{ident}/publish")
    def profile_publish(ident: str, body: VersionInput, s=Depends(db), c=Depends(ctx)):
        c.permit("admin")
        row = scoped(s, ImportProfile, ident, c, True)
        require(row.status == "DRAFT" and row.lock_version == body.expected_version, 409, "PROFILE_VERSION", "配置已变化或不是待发布状态")
        for active in s.scalars(select(ImportProfile).where(ImportProfile.org_id == c.org_id, ImportProfile.supplier_id == row.supplier_id, ImportProfile.status == "PUBLISHED")):
            active.status = "RETIRED"
        row.status, row.effective_from, row.approved_by, row.lock_version = "PUBLISHED", now(), c.user_id, row.lock_version + 1
        audit(s, c, "import_profile.publish", row.id, {"number": row.number})
        return data(row)

    @app.post(P + "/import-profiles/{ident}/rollback", status_code=201)
    def profile_rollback(ident: str, body: Reason, s=Depends(db), c=Depends(ctx)):
        c.permit("admin")
        source = scoped(s, ImportProfile, ident, c)
        latest = s.scalar(select(func.max(ImportProfile.number)).where(ImportProfile.org_id == c.org_id, ImportProfile.supplier_id == source.supplier_id, ImportProfile.name == source.name)) or 0
        for active in s.scalars(select(ImportProfile).where(ImportProfile.org_id == c.org_id, ImportProfile.supplier_id == source.supplier_id, ImportProfile.status == "PUBLISHED")):
            active.status = "RETIRED"
        row = ImportProfile(
            id=uid(), org_id=c.org_id, supplier_id=source.supplier_id, name=source.name, number=latest + 1,
            file_type=source.file_type, sheet=source.sheet, header_row=source.header_row, encoding=source.encoding,
            mapping=source.mapping, transformations=source.transformations, validations=source.validations,
            status="PUBLISHED", lock_version=1, effective_from=now(), change_note=f"回退到 v{source.number}：{body.reason}",
            created_by=c.user_id, approved_by=c.user_id, based_on_id=source.id,
        )
        s.add(row)
        s.flush()
        audit(s, c, "import_profile.rollback", row.id, {"based_on": source.id, "reason": body.reason})
        return data(row)

    @app.get(P + "/ingestion-events")
    def events(status: str = "", source_id: str = "", cursor: str | None = None, limit: int = Query(50, ge=1, le=100), s=Depends(db), c=Depends(ctx)):
        query = select(IngestionEvent).where(IngestionEvent.org_id == c.org_id)
        if status:
            query = query.where(IngestionEvent.status == status)
        if source_id:
            query = query.where(IngestionEvent.source_id == source_id)
        rows, nxt = page(s, query, IngestionEvent, cursor, limit)
        return {"items": [event_data(s, row) for row in rows], "next_cursor": nxt}

    @app.post(P + "/ingestion-events", status_code=201)
    def event_create(body: EventInput, s=Depends(db), c=Depends(ctx)):
        c.permit("operator", "admin", "integration_manager")
        source = scoped(s, IngestionSource, body.source_id, c)
        profile = scoped(s, ImportProfile, body.profile_id, c) if body.profile_id else v14_ingestion.latest_profile(s, source)
        file = scoped(s, File, body.file_id, c)
        row, duplicate = v14_ingestion.receive_file(s, c, source, profile, file, body.external_key, c.user_id)
        return event_data(s, row, duplicate)

    @app.post(P + "/ingestion-sources/{ident}/upload", status_code=201)
    def source_upload(ident: str, file: UploadFile = UploadParam(...), profile_id: str | None = None, external_key: str | None = None, s=Depends(db), c=Depends(ctx)):
        c.permit("operator", "admin", "integration_manager")
        source = scoped(s, IngestionSource, ident, c)
        require(source.kind == "MANUAL", 409, "INGESTION_KIND", "此入口只用于页面上传接入源")
        profile = scoped(s, ImportProfile, profile_id, c) if profile_id else v14_ingestion.latest_profile(s, source)
        content = file.file.read(v14_ingestion.MAX_BYTES + 1)
        event_key = external_key or f"manual-{uid()}"
        row, duplicate = v14_ingestion.receive_bytes(s, c, source, profile, file.filename or "upload.csv", content, event_key, c.user_id)
        return event_data(s, row, duplicate)

    @app.post(P + "/ingestion-events/{ident}/replay")
    def event_replay(ident: str, body: Reason, s=Depends(db), c=Depends(ctx)):
        c.permit("operator", "admin", "integration_manager")
        row = v14_ingestion.replay(s, c, scoped(s, IngestionEvent, ident, c, True))
        audit(s, c, "ingestion.replay", row.id, {"reason": body.reason, "attempt": row.version})
        return event_data(s, row)

    @app.get(P + "/ingestion-events/{ident}/attempts")
    def attempts(ident: str, s=Depends(db), c=Depends(ctx)):
        scoped(s, IngestionEvent, ident, c)
        rows = s.scalars(select(IngestionAttempt).where(IngestionAttempt.org_id == c.org_id, IngestionAttempt.event_id == ident).order_by(IngestionAttempt.number)).all()
        return {"items": [data(row) for row in rows]}

    @app.get(P + "/ingestion-events/{ident}/errors")
    def event_errors(ident: str, s=Depends(db), c=Depends(ctx)):
        row = scoped(s, IngestionEvent, ident, c)
        require(row.status in {"FAILED", "REJECTED"}, 409, "INGESTION_NO_ERRORS", "此事件没有可下载的问题报告")
        return Response(v14_ingestion.error_csv(row), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="ingestion-errors.csv"'})

    @app.post(P + "/service/ingestion-sources/{ident}/events", status_code=201)
    async def service_ingestion(
        ident: str,
        request: Request,
        x_organization_id: str = Header(...),
        x_event_id: str = Header(..., min_length=1, max_length=200),
        x_timestamp: str = Header(...),
        x_signature: str = Header(...),
        x_filename: str = Header(..., min_length=1, max_length=250),
        x_profile_id: str | None = Header(default=None),
        x_content_sha256: str | None = Header(default=None),
        s=Depends(db),
    ):
        source = s.scalar(select(IngestionSource).where(IngestionSource.id == ident, IngestionSource.org_id == x_organization_id))
        require(source and source.kind == "API" and source.status == "ACTIVE", 404, "NOT_FOUND", "接入源不存在或未启用")
        account = integrations.service_auth(s, request.headers.get("authorization", ""), x_organization_id, "ingestion:write")
        quotas.count_request(s, Context(source.org_id, account.id, [], request.state.request_id))
        content_length = int(request.headers.get("content-length", "0") or 0)
        require(content_length <= v14_ingestion.MAX_BYTES, 413, "FILE_TOO_LARGE", "文件不能超过 20MB")
        content = await request.body()
        require(len(content) <= v14_ingestion.MAX_BYTES, 413, "FILE_TOO_LARGE", "文件不能超过 20MB")
        secret = integrations.cipher().decrypt(source.secret_cipher.encode()).decode()
        require(integrations.verify_signature(secret, x_event_id, x_timestamp, content, x_signature), 401, "INGESTION_SIGNATURE", "接入签名无效或时间已超过 5 分钟")
        if x_content_sha256:
            require(digest(content) == x_content_sha256, 409, "FILE_HASH_MISMATCH", "请求文件摘要不一致")
        machine = Context(source.org_id, source.created_by, ["operator"], request.state.request_id)
        profile = scoped(s, ImportProfile, x_profile_id, machine) if x_profile_id else v14_ingestion.latest_profile(s, source)
        row, duplicate = v14_ingestion.receive_bytes(s, machine, source, profile, x_filename, content, x_event_id, source.created_by)
        s.add(ServiceAccess(org_id=source.org_id, account_id=account.id, action="ingestion.write", resource_id=row.id))
        return event_data(s, row, duplicate)

    @app.get(P + "/evaluations")
    def evaluations(s=Depends(db), c=Depends(ctx)):
        c.permit("admin", "annotator", "adjudicator")
        return {"items": [data(row) for row in s.scalars(select(EvaluationRun).where(EvaluationRun.org_id == c.org_id).order_by(EvaluationRun.created_at.desc()))]}

    @app.post(P + "/evaluations", status_code=201)
    def evaluation_create(body: EvaluationInput, s=Depends(db), c=Depends(ctx)):
        c.permit("admin")
        dataset = scoped(s, DatasetVersion, body.dataset_id, c)
        require(dataset.status == "FROZEN" and dataset.content_hash, 409, "DATASET_NOT_FROZEN", "评测只能绑定已冻结且摘要完整的数据集")
        if body.model_id:
            require(s.get(ModelVersion, body.model_id) is not None, 404, "MODEL_NOT_FOUND", "模型版本不存在")
        required = {"recall_at_20", "top1", "ndcg_at_5", "ece", "brier", "suggestion_precision", "suggestion_precision_ci_lower", "suggestion_coverage", "hard_conflict_suggestions", "inference_p95_ms", "sample_size"}
        require(required <= set(body.metrics), 422, "EVALUATION_METRICS", "评测指标不完整")
        require(body.metrics["sample_size"] > 0 and all(isinstance(body.metrics[key], (int, float)) for key in required), 422, "EVALUATION_METRICS", "评测指标类型或样本量无效")
        expected_scope = dataset.provenance.get("source_type")
        require(expected_scope == body.scope, 422, "EVALUATION_SCOPE", "评测范围必须与数据集来源类型一致")
        payload = body.model_dump()
        content_hash = digest({**payload, "dataset_hash": dataset.content_hash})
        old = s.scalar(select(EvaluationRun).where(EvaluationRun.org_id == c.org_id, EvaluationRun.content_hash == content_hash))
        if old:
            return data(old)
        row = EvaluationRun(id=uid(), org_id=c.org_id, created_by=c.user_id, content_hash=content_hash, **payload)
        s.add(row)
        s.flush()
        audit(s, c, "evaluation.register", row.id, {"dataset_id": dataset.id, "scope": row.scope, "content_hash": row.content_hash})
        return data(row)

    @app.get(P + "/threshold-policies")
    def threshold_policies(s=Depends(db), c=Depends(ctx)):
        return {"items": [data(row) for row in s.scalars(select(ThresholdPolicy).where(ThresholdPolicy.org_id == c.org_id).order_by(ThresholdPolicy.created_at.desc()))]}

    @app.post(P + "/threshold-policies", status_code=201)
    def threshold_create(body: ThresholdInput, s=Depends(db), c=Depends(ctx)):
        c.permit("admin")
        scoped(s, EvaluationRun, body.evaluation_id, c)
        required = {"confirm_min", "review_min", "margin_min"}
        require(required <= set(body.thresholds) and set(body.thresholds) <= required, 422, "THRESHOLD_FIELDS", "阈值字段必须包含建议确认、重点复核和候选分差")
        confirm, review, margin = (body.thresholds[key] for key in ("confirm_min", "review_min", "margin_min"))
        require(0 <= review <= confirm <= 1 and 0 <= margin <= 1, 422, "THRESHOLD_ORDER", "阈值范围或区间顺序不正确")
        latest = s.scalar(select(ThresholdPolicy).where(ThresholdPolicy.org_id == c.org_id, ThresholdPolicy.name == body.name).order_by(ThresholdPolicy.number.desc()))
        row = ThresholdPolicy(id=uid(), org_id=c.org_id, number=(latest.number if latest else 0) + 1, created_by=c.user_id, status="DRAFT", **body.model_dump())
        s.add(row)
        s.flush()
        audit(s, c, "threshold_policy.create", row.id, {"evaluation_id": row.evaluation_id})
        return data(row)

    @app.post(P + "/threshold-policies/{ident}/approve")
    def threshold_approve(ident: str, body: ThresholdApproval, s=Depends(db), c=Depends(ctx)):
        c.permit("admin")
        row = scoped(s, ThresholdPolicy, ident, c, True)
        require(row.status == "DRAFT", 409, "THRESHOLD_STATE", "只有草稿阈值可以审批")
        evaluation = scoped(s, EvaluationRun, row.evaluation_id, c)
        metrics = evaluation.metrics
        failures = []
        for key, target, mode in (("recall_at_20", .98, "min"), ("suggestion_precision", .97, "min"), ("suggestion_precision_ci_lower", .95, "min"), ("ece", .05, "max"), ("hard_conflict_suggestions", 0, "max")):
            value = metrics.get(key)
            if value is None or (value < target if mode == "min" else value > target):
                failures.append({"metric": key, "value": value, "target": target, "mode": mode})
        require(not failures, 409, "THRESHOLD_ADMISSION", "评测结果未达到 v1.4 阈值准入条件", failures)
        base = scoped(s, Policy, body.base_policy_id, c)
        config = {**base.config, "high_threshold": row.thresholds["confirm_min"], "margin": row.thresholds["margin_min"], "threshold_policy_id": row.id, "evaluation_run_id": evaluation.id, "calibration_method": row.calibration_method, "validated": True}
        if config.get("engine") == "lightgbm":
            from packages.matching.policy import policy_hash
            config["bundle_hash"] = policy_hash(config)
        policy = Policy(org_id=c.org_id, name=f"{row.name} v{row.number}", config=config)
        s.add(policy)
        s.flush()
        for active in s.scalars(select(ThresholdPolicy).where(ThresholdPolicy.org_id == c.org_id, ThresholdPolicy.name == row.name, ThresholdPolicy.status == "APPROVED")):
            active.status = "RETIRED"
        row.status, row.effective_from, row.approved_by, row.approval_note, row.matching_policy_id = "APPROVED", now(), c.user_id, body.reason, policy.id
        if body.activate_default:
            s.get(Organization, c.org_id).default_policy_id = policy.id
        audit(s, c, "threshold_policy.approve", row.id, {"matching_policy_id": policy.id, "activate_default": body.activate_default})
        return data(row)

    @app.get(P + "/learning-queue")
    def learning_queue(run_id: str = "", limit: int = Query(50, ge=1, le=100), s=Depends(db), c=Depends(ctx)):
        c.permit("reviewer", "admin", "annotator")
        query = select(Item).where(Item.org_id == c.org_id, Item.status == "PENDING")
        if run_id:
            scoped(s, Run, run_id, c)
            query = query.where(Item.run_id == run_id)
        rows = s.scalars(query.order_by(Item.created_at.desc()).limit(500)).all()
        result = []
        for item in rows:
            source = s.get(Source, item.source_id)
            result.append({**data(item), "source": {"sku": source.sku, "name": source.normalized.get("name")}, **v14_learning.risk(s, c.org_id, item)})
        result.sort(key=lambda item: (-item["risk_score"], item["id"]))
        return {"items": result[:limit], "total_considered": len(result), "ordering": "硬冲突、发布影响、候选缺失、字段缺失、候选分差和排序不确定性"}

    @app.get(P + "/items/{ident}/learning-context")
    def learning_context(ident: str, s=Depends(db), c=Depends(ctx)):
        item = scoped(s, Item, ident, c)
        return {**v14_learning.risk(s, c.org_id, item), "similar_history": v14_learning.similar_history(s, c.org_id, item)}

    @app.get(P + "/labels")
    def labels(status: str = "", cursor: str | None = None, limit: int = Query(50, ge=1, le=100), s=Depends(db), c=Depends(ctx)):
        c.permit("admin", "annotator", "adjudicator")
        query = select(LabelVersion).where(LabelVersion.org_id == c.org_id)
        if status:
            query = query.where(LabelVersion.status == status)
        rows, nxt = page(s, query, LabelVersion, cursor, limit)
        return {"items": [data(row) for row in rows], "next_cursor": nxt}

    @app.put(P + "/labels/{ident}")
    def label_update(ident: str, body: LabelUpdate, s=Depends(db), c=Depends(ctx)):
        c.permit("admin", "adjudicator")
        row = v14_learning.classify_label(scoped(s, LabelVersion, ident, c, True), body.error_type, body.status)
        audit(s, c, "label.classify", row.id, body.model_dump())
        return data(row)
