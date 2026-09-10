import time
from functools import lru_cache
from sqlalchemy import func, select

from . import storage
from .auth import scoped
from .db import now, uid
from .errors import require
from .ingest import mapped_rows, parse_file
from .models import *
from ..matching.engine import DEFAULT_POLICY, FEATURE_VERSION, INDEX_VERSION
from ..matching.normalize import RULE_VERSION, compare, digest


@lru_cache(maxsize=1)
def release_hashes():
    from .config import ROOT
    files = {str(p.relative_to(ROOT)): digest(p.read_bytes()) for folder in ("apps/api", "packages", "workers") for p in (ROOT / folder).rglob("*.py")}
    lock = ROOT / "requirements.lock"
    return {"code_sha256": digest(files), "dependency_lock_sha256": digest(lock.read_bytes()) if lock.exists() else None}


def audit(s, ctx, action, resource_id, detail=None):
    s.add(Audit(org_id=ctx.org_id, actor_id=ctx.user_id, action=action, resource_id=resource_id, detail=detail or {}, request_id=ctx.request_id))


def emit(s, ctx, kind, resource_id):
    s.add(Outbox(org_id=ctx.org_id, event_key=f"{kind}:{resource_id}", kind=kind, resource_id=resource_id))


def idempotent(s, ctx, route, key, body, operation):
    require(key and len(key) <= 100, 422, "IDEMPOTENCY_REQUIRED", "请提供有效的 Idempotency-Key")
    hashed = digest(body)
    old = s.scalar(select(Idempotency).where(Idempotency.org_id == ctx.org_id, Idempotency.user_id == ctx.user_id, Idempotency.route == route, Idempotency.key == key))
    if old:
        require(old.request_hash == hashed, 409, "IDEMPOTENCY_CONFLICT", "此幂等键已用于不同请求")
        return old.response
    response = operation()
    s.add(Idempotency(org_id=ctx.org_id, user_id=ctx.user_id, route=route, key=key, request_hash=hashed, response=response))
    s.flush()
    return response


def load_rows(s, ctx, data, catalog=False):
    from .imports import cached_rows
    return cached_rows(s, ctx, data, catalog)


def create_revision(s, ctx, batch, data, fingerprint_context=None):
    file, rows = load_rows(s, ctx, data)
    excludes = set(data.get("exclude_rows", []))
    errors = {r["row_no"] for r in rows if any(i["level"] == "error" for i in r["issues"])}
    require(errors <= excludes, 422, "EXCLUSION_CONFIRMATION_REQUIRED", "请明确确认排除错误行，或修正后重新导入", {"error_rows": sorted(errors)})
    require(excludes <= {r["row_no"] for r in rows}, 422, "INVALID_EXCLUSIONS", "排除范围包含不存在的行")
    valid = len(rows) - len(excludes)
    require(valid > 0, 422, "NO_VALID_ROWS", "没有有效记录，无法创建输入版本")
    content_hash = digest({"file": file.sha256, "config": {k:v for k,v in data.items() if k!="file_id"}, "rule": RULE_VERSION, **({"context":fingerprint_context} if fingerprint_context else {})})
    old = s.scalar(select(Revision).where(Revision.org_id==ctx.org_id,Revision.batch_id==batch.id,Revision.content_hash==content_hash))
    if old:return old
    number = (s.scalar(select(func.max(Revision.number)).where(Revision.org_id == ctx.org_id, Revision.batch_id == batch.id)) or 0) + 1
    revision = Revision(id=uid(), org_id=ctx.org_id, batch_id=batch.id, file_id=file.id, number=number, sheet=data["sheet"], header_row=data["header_row"], mapping=data["mapping"], content_hash=digest({"file": file.sha256, "config": {k:v for k,v in data.items() if k!="file_id"}, "rule": RULE_VERSION, **({"context":fingerprint_context} if fingerprint_context else {})}), status="READY", total=len(rows), valid=valid, excluded=len(excludes), exclusion_rows=sorted(excludes), created_by=ctx.user_id, rule_version=RULE_VERSION)
    s.add(revision)
    s.flush()
    for row in rows:
        s.add(Source(org_id=ctx.org_id, revision_id=revision.id, excluded=row["row_no"] in excludes, **row))
    from .releases import touch_batch
    touch_batch(s, ctx.org_id, batch.id)
    audit(s, ctx, "revision.create", revision.id, {"valid": valid, "excluded_rows": sorted(excludes)})
    s.flush()
    return revision


def create_catalog_version(s, ctx, catalog, data):
    file, rows = load_rows(s, ctx, data, True)
    errors = [{"row_no": r["row_no"], "issues": r["issues"]} for r in rows if any(i["level"] == "error" for i in r["issues"])]
    require(len(rows) > 0, 422, "NO_VALID_ROWS", "标准库为空")
    content_hash = digest({"file": file.sha256, "config": {k:v for k,v in data.items() if k!="file_id"}, "rule": RULE_VERSION})
    old = s.scalar(select(CatalogVersion).where(CatalogVersion.org_id==ctx.org_id,CatalogVersion.catalog_id==catalog.id,CatalogVersion.content_hash==content_hash))
    if old:return old
    number = (s.scalar(select(func.max(CatalogVersion.number)).where(CatalogVersion.org_id == ctx.org_id, CatalogVersion.catalog_id == catalog.id)) or 0) + 1
    version = CatalogVersion(id=uid(), org_id=ctx.org_id, catalog_id=catalog.id, file_id=file.id, number=number, status="INVALID" if errors else "DRAFT", content_hash=digest({"file": file.sha256, "config": {k:v for k,v in data.items() if k!="file_id"}, "rule": RULE_VERSION}), row_count=len(rows), errors=errors, mapping=data)
    s.add(version)
    s.flush()
    if not errors:
        for row in rows:
            s.add(Product(org_id=ctx.org_id, version_id=version.id, sku=row["sku"], raw=row["raw"], normalized=row["normalized"], fingerprint=digest(row["normalized"]), search_name=str(row["normalized"].get("name") or "").casefold(), search_model=str(row["normalized"].get("model") or "").casefold()))
    audit(s, ctx, "catalog.version.create", version.id, {"errors": len(errors)})
    s.flush()
    return version


def create_run(s, ctx, data, retry_of=None):
    ctx.permit("admin", "operator")
    from .quotas import queue_available
    queue_available(s,ctx.org_id)
    revision = scoped(s, Revision, data["revision_id"], ctx)
    catalog = scoped(s, CatalogVersion, data["catalog_version_id"], ctx)
    org = s.get(Organization, ctx.org_id)
    policy = scoped(s, Policy, data.get("policy_id") or org.default_policy_id, ctx)
    require(revision.status == "READY" and catalog.status == "PUBLISHED", 409, "INPUT_NOT_READY", "请选择就绪的输入版本和已发布的标准库")
    if policy.config.get('engine') == 'lightgbm':
        from packages.matching.policy import validate_bundle
        try:validate_bundle(policy.config)
        except (ValueError,KeyError,OSError) as exc:
            from .errors import Problem
            raise Problem(409, 'BUNDLE_INVALID', '完整策略制品校验失败') from exc
        require(policy.config.get('validated'),409,'POLICY_NOT_READY','学习策略尚未通过准入')
    require(policy.config["rule_version"] == RULE_VERSION and revision.rule_version == RULE_VERSION, 409, "RULE_VERSION_UNAVAILABLE", "当前执行程序不支持此规则版本")
    file = scoped(s, File, revision.file_id, ctx)
    import os
    manifest = {"input_hash": revision.content_hash, "input_file_sha256": file.sha256, "mapping": revision.mapping, "rule_version": RULE_VERSION, "category": "phone", "required_fields": ["brand", "model", "ram", "storage", "color", "region", "pack_count"], "catalog_version_id": catalog.id, "catalog_hash": catalog.content_hash, "index_version": INDEX_VERSION, "feature_schema": FEATURE_VERSION, "policy_id": policy.id, "policy": policy.config, "dual_review": org.dual_review, "code_version": os.getenv("CODE_VERSION", "1.3.0"), "batch_creator": scoped(s, Batch, revision.batch_id, ctx).created_by, "revision_creator": revision.created_by}
    lineage = s.scalar(select(RevisionLineage).where(RevisionLineage.org_id==ctx.org_id, RevisionLineage.revision_id==revision.id))
    if lineage:
        manifest.update({'correction_scope':lineage.mode, 'parent_run_id':lineage.parent_run_id})
    manifest.update(release_hashes())
    run = Run(id=uid(), org_id=ctx.org_id, revision_id=revision.id, catalog_version_id=catalog.id, policy_id=policy.id, created_by=ctx.user_id, retry_of=retry_of, manifest=manifest, total=revision.valid, excluded=revision.excluded)
    s.add(run)
    s.flush()
    sources = s.scalars(select(Source.id).where(Source.org_id == ctx.org_id, Source.revision_id == revision.id, Source.excluded.is_(False)).order_by(Source.row_no)).all()
    # SQLite has a single writer. Bound new result transactions so interactive
    # work can acquire the writer lock between matching chunks.
    chunk_size = 50 if s.get_bind().dialect.name == 'sqlite' else 200
    run.manifest = {**run.manifest, 'chunk_size': chunk_size}
    for i in range(0, len(sources), chunk_size):
        s.add(Chunk(org_id=ctx.org_id, run_id=run.id, number=i // chunk_size, source_ids=sources[i:i+chunk_size]))
    emit(s, ctx, "match", run.id)
    from .releases import touch_batch
    touch_batch(s, ctx.org_id, revision.batch_id)
    audit(s, ctx, "run.create", run.id)
    s.flush()
    return {"id": run.id, "status": run.status}


def decide(s, ctx, item_id, data, bulk=False):
    ctx.permit("reviewer")
    item = scoped(s, Item, item_id, ctx, True)
    run = scoped(s, Run, item.run_id, ctx, True)
    require(run.status == "SUCCEEDED", 409, "RUN_NOT_READY", "只有成功运行可以审核")
    submitters = {run.created_by, run.manifest["batch_creator"], run.manifest["revision_creator"]}
    require(not run.manifest["dual_review"] or ctx.user_id not in submitters, 403, "SELF_REVIEW_DENIED", "双人复核已开启，提交者不能审核自己的批次")
    require(item.version == data["expected_version"], 409, "VERSION_CONFLICT", "记录已更新，请刷新后处理", {"version": item.version, "status": item.status})
    from .review_claims import verify
    claim = verify(s, ctx, item, data.get("claim_token"), required=False)
    action, reason, product_id = data["action"], data.get("reason", "").strip(), data.get("product_id")
    require(action in ("confirm", "unmatched", "needs_info", "revoke"), 422, "INVALID_ACTION", "不支持此审核操作")
    if action != "confirm" or item.status != "PENDING":
        require(bool(reason), 422, "REASON_REQUIRED", "此操作必须填写原因")
    if bulk:
        require(action == "confirm" and item.suggestion == "RECOMMENDED" and item.status == "PENDING", 409, "BULK_NOT_ALLOWED", "批量确认仅适用于未处理的推荐匹配")
    selection = "none"
    if action == "confirm":
        require(bool(product_id), 422, "PRODUCT_REQUIRED", "请选择标准商品")
        product = scoped(s, Product, product_id, ctx)
        require(product.version_id == run.catalog_version_id, 404, "NOT_FOUND", "资源不存在或无权访问")
        source = scoped(s, Source, item.source_id, ctx)
        _, conflicts, missing = compare(source.normalized, product.normalized)
        require(not conflicts, 409, "SPEC_CONFLICT", "存在未解决的关键字段冲突，请修正输入后新建运行", {"conflicts": conflicts})
        require(not missing, 409, "INSUFFICIENT_INFO", "关键字段不完整，请补充资料后重新运行", {"missing": missing})
        candidate = s.scalar(select(Candidate).where(Candidate.org_id == ctx.org_id, Candidate.item_id == item.id, Candidate.product_id == product_id))
        selection = "candidate" if candidate else "manual_search"
        if bulk:
            require(candidate and candidate.rank == 1, 409, "BULK_NOT_ALLOWED", "批量确认必须选择首选推荐商品")
    else:
        product_id = None
    if action == "revoke":
        require(item.status not in ("PENDING", "REVOKED"), 409, "INVALID_TRANSITION", "当前没有可撤销的决定")
    event = ReviewEvent(id=uid(), org_id=ctx.org_id, item_id=item.id, seq=item.version+1, action=action, product_id=product_id, reason=reason, actor_id=ctx.user_id, selection_source=selection)
    s.add(event)
    s.flush()
    current = s.scalar(select(Mapping).where(Mapping.org_id == ctx.org_id, Mapping.item_id == item.id, Mapping.valid_to.is_(None)))
    if current:
        current.valid_to = now()
        s.flush()
    if action == "confirm":
        s.add(Mapping(org_id=ctx.org_id, item_id=item.id, decision_id=event.id, source_id=item.source_id, product_id=product_id))
    item.status = {"confirm": "CONFIRMED", "unmatched": "UNMATCHED", "needs_info": "NEEDS_INFO", "revoke": "REVOKED"}[action]
    item.current_decision_id, item.version = event.id, item.version + 1
    if claim:
        from .review_claims import event as claim_event
        claim.status, claim.lease_until, claim.token_hash = "COMPLETED", 0, None
        claim_event(s, ctx, item, "COMPLETED", {"decision_id":event.id})
    from .releases import touch_batch, invalidate_for_item
    revision = scoped(s, Revision, run.revision_id, ctx)
    touch_batch(s, ctx.org_id, revision.batch_id)
    invalidate_for_item(s, ctx, item.id, event.id)
    audit(s, ctx, "review." + action, item.id, {"event_id": event.id, "version": item.version})
    s.flush()
    return {"decision_id": event.id, "item_version": item.version, "status": item.status}


STATUS_LABELS = {"PENDING": "未处理", "CONFIRMED": "已确认", "UNMATCHED": "当前未匹配", "NEEDS_INFO": "待补充", "REVOKED": "已撤销"}


def export_snapshot(s, ctx, run_id, data):
    run = scoped(s, Run, run_id, ctx, True)
    require(run.status == "SUCCEEDED", 409, "RUN_NOT_READY", "只有成功运行可以正式导出")
    require(data["kind"] in ("confirmed", "all") and data["format"] in ("csv", "xlsx"), 422, "INVALID_EXPORT", "导出类型或格式无效")
    revision = scoped(s, Revision, run.revision_id, ctx)
    file = scoped(s, File, revision.file_id, ctx)
    query = select(Item).where(Item.org_id == ctx.org_id, Item.run_id == run.id).order_by(Item.id)
    if data["kind"] == "confirmed":
        query = query.where(Item.status == "CONFIRMED")
    if data.get("status"):
        query = query.where(Item.status == data["status"])
    if data.get("suggestion"):
        query = query.where(Item.suggestion == data["suggestion"])
    joined = query.add_columns(Source, ReviewEvent, Product, User.name).join(Source, (Source.org_id==Item.org_id)&(Source.id==Item.source_id)).outerjoin(ReviewEvent,(ReviewEvent.org_id==Item.org_id)&(ReviewEvent.id==Item.current_decision_id)).outerjoin(Product,(Product.org_id==ReviewEvent.org_id)&(Product.id==ReviewEvent.product_id)).outerjoin(User,User.id==ReviewEvent.actor_id)
    records = s.execute(joined).all()
    export = Export(id=uid(), org_id=ctx.org_id, run_id=run.id, kind=data["kind"], format=data["format"], count=len(records), filters=data, snapshot_hash="", created_by=ctx.user_id, expires_at=time.time()+30*86400)
    s.add(export)
    s.flush()
    snapshots = []
    for i, (item, source, event, product, actor_name) in enumerate(records, 1):
        differences = []
        if product:
            evidence, _, _ = compare(source.normalized, product.normalized)
            differences = [e["text"] for e in evidence if e["state"] != "same"]
            if source.normalized.get("price") != product.normalized.get("price"):
                differences.append(f"报价 {source.normalized.get('price') or '缺失'} / 标准价格 {product.normalized.get('price') or '缺失'}")
        row = {"来源文件": file.name, "工作表": revision.sheet, "原始行号": str(source.row_no), "来源编号": source.sku, "编号类型": "系统记录号" if source.generated_sku else "来源编号", "原始名称": source.normalized["name"], "标准编号": product.sku if product else "", "标准名称": product.normalized["name"] if product else "", "差异说明": "；".join(differences), "审核状态": STATUS_LABELS[item.status], "审核原因": event.reason if event else "", "审核人": actor_name or "", "审核时间": event.created_at if event else "", "运行编号": run.id, "决定编号": event.id if event else ""}
        if data.get("include_raw"):
            row.update({"原始列_" + k: str(v) for k, v in source.raw.items()})
        snapshots.append(row)
        s.add(ExportRow(org_id=ctx.org_id, export_id=export.id, item_id=item.id, item_version=item.version, row_no=i, data=row))
    export.snapshot_hash = digest(snapshots)
    emit(s, ctx, "export", export.id)
    audit(s, ctx, "export.create", export.id, {"count": len(records), "snapshot_hash": export.snapshot_hash})
    s.flush()
    return {"id": export.id, "status": export.status, "count": export.count, "snapshot_hash": export.snapshot_hash}
