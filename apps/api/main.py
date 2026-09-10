import io
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File as Upload, Header, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text

from apps.api.schemas import *
from packages.domain import storage
from packages.domain.auth import authenticate, context, issue_session, password_hash, scoped, token_hash, verify_password
from packages.domain.config import ALLOWED_ORIGINS, COOKIE_SECURE, QUEUE_MODE, REDIS_URL, ROOT
from packages.domain.db import initialize, now, transaction, uid
from packages.domain.errors import Problem, require
from packages.domain.ingest import FIELDS, MAX_BYTES, parse_file, preview
from packages.domain.models import *
from packages.domain.services import audit, create_catalog_version, create_revision, create_run, decide, export_snapshot, idempotent, load_rows
from packages.matching.engine import DEFAULT_POLICY
from packages.matching.normalize import digest

log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app):
    initialize()
    yield


app = FastAPI(title="商品数据匹配与核对平台", version="1.0.0", openapi_url="/api/v1/openapi.json", docs_url="/api/v1/docs", lifespan=lifespan)
PREFIX = "/api/v1"


@app.middleware("http")
async def request_context(request, call_next):
    request.state.request_id = uid()
    started = time.perf_counter()
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("origin") and request.headers["origin"] not in ALLOWED_ORIGINS:
        return JSONResponse({"code": "ORIGIN_DENIED", "message": "请求来源未获授权", "request_id": request.state.request_id}, status_code=403)
    try:
        response = await call_next(request)
    except Exception as exc:
        log.error(json.dumps({"event": "request_error", "request_id": request.state.request_id, "path": request.url.path, "error_type": type(exc).__name__}))
        response = JSONResponse({"code": "INTERNAL_ERROR", "message": "服务暂时不可用，请稍后重试", "request_id": request.state.request_id}, status_code=500)
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.url.path.startswith(PREFIX):
        response.headers["Cache-Control"] = "no-store"
    log.info(json.dumps({"event": "request", "request_id": request.state.request_id, "method": request.method, "path": request.url.path, "status": response.status_code, "seconds": round(time.perf_counter()-started, 4)}))
    return response


@app.exception_handler(Problem)
async def problem_handler(request, exc):
    return JSONResponse({"code": exc.code, "message": exc.message, "details": exc.details, "request_id": request.state.request_id}, status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def validation_handler(request, exc):
    return JSONResponse({"code": "VALIDATION_ERROR", "message": "请求字段格式不正确", "details": [{"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()], "request_id": request.state.request_id}, status_code=422)


def db(request: Request):
    with transaction(write=request.method not in ("GET", "HEAD")) as s:
        if request.method == "POST" and request.url.path.endswith("/exports") and s.bind.dialect.name == "postgresql":
            s.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        yield s


def user(request: Request, s=Depends(db)):
    return authenticate(s, request.headers.get("authorization", ""))


def ctx(request: Request, s=Depends(db), actor=Depends(user)):
    return context(s, actor, request.headers.get("x-organization-id", ""), request.state.request_id, write=request.method not in ("GET", "HEAD"))


def data(obj, exclude=()):
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns if c.name not in {"org_id", *exclude}}


def page(s, query, cls, cursor, limit):
    if cursor:
        query = query.where(cls.id > cursor)
    rows = s.scalars(query.order_by(cls.id).limit(limit+1)).all()
    return rows[:limit], (rows[limit-1].id if len(rows) > limit else None)


def cookies(response, refresh):
    response.set_cookie("refresh_token", refresh, httponly=True, secure=COOKIE_SECURE, samesite="strict", max_age=604800, path=PREFIX+"/auth")
    response.set_cookie("csrf_token", secrets.token_urlsafe(24), httponly=False, secure=COOKIE_SECURE, samesite="strict", max_age=604800, path="/")


def csrf(request):
    cookie, header = request.cookies.get("csrf_token"), request.headers.get("x-csrf-token")
    require(cookie and header and secrets.compare_digest(cookie, header), 403, "CSRF_FAILED", "会话校验失败，请重新登录")


@app.post(PREFIX+"/auth/login")
def login(body: Login, request: Request, response: Response):
    key = digest({"ip": request.client.host if request.client else "unknown", "email": body.email.lower()})
    with transaction(write=True) as s:
        attempts = s.scalar(select(func.count()).select_from(LoginAttempt).where(LoginAttempt.key == key, LoginAttempt.at > time.time()-300))
        require(attempts < 10, 429, "RATE_LIMITED", "登录尝试过多，请 5 分钟后重试")
        s.add(LoginAttempt(key=key, at=time.time()))
    with transaction(write=True) as s:
        actor = s.scalar(select(User).where(User.email == body.email.strip().lower()))
        valid = verify_password(body.password, actor.password_hash if actor else "pbkdf2_sha256$310000$dummy$invalid")
        require(actor and actor.active and valid, 401, "INVALID_CREDENTIALS", "账号或密码不正确")
        token, refresh = issue_session(s, actor)
        cookies(response, refresh)
        return {"access_token": token, "user": data(actor, ("password_hash",))}


@app.post(PREFIX+"/auth/refresh")
def refresh(request: Request, response: Response, s=Depends(db)):
    csrf(request)
    session = s.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash(request.cookies.get("refresh_token", ""))).with_for_update())
    require(session and not session.revoked and session.expires_at > time.time(), 401, "UNAUTHENTICATED", "请重新登录")
    actor = s.get(User, session.user_id)
    require(actor and actor.active, 401, "UNAUTHENTICATED", "账号已停用")
    session.revoked = True
    token, new_refresh = issue_session(s, actor)
    cookies(response, new_refresh)
    return {"access_token": token, "user": data(actor, ("password_hash",))}


@app.post(PREFIX+"/auth/logout")
def logout(request: Request, response: Response, s=Depends(db)):
    csrf(request)
    session = s.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash(request.cookies.get("refresh_token", ""))).with_for_update())
    if session:
        session.revoked = True
    response.delete_cookie("refresh_token", path=PREFIX+"/auth")
    response.delete_cookie("csrf_token", path="/")
    return {"ok": True}


@app.get(PREFIX+"/memberships")
def memberships(s=Depends(db), actor=Depends(user)):
    rows = s.execute(select(Membership, Organization).join(Organization, Organization.id == Membership.org_id).where(Membership.user_id == actor.id, Membership.active.is_(True))).all()
    return {"items": [{"org_id": org.id, "name": org.name, "roles": m.roles} for m, org in rows]}


@app.get(PREFIX+"/dashboard")
def dashboard(s=Depends(db), c=Depends(ctx)):
    def counts(cls, field):
        return dict(s.execute(select(field, func.count()).where(cls.org_id == c.org_id).group_by(field)).all())
    return {"runs": counts(Run, Run.status), "suggestions": counts(Item, Item.suggestion), "decisions": counts(Item, Item.status), "catalog_products": s.scalar(select(func.count()).select_from(Product).where(Product.org_id == c.org_id)), "queue_mode": QUEUE_MODE}


@app.post(PREFIX+"/files", status_code=201)
def upload(file: UploadFile = Upload(...), encoding: str = "utf-8", s=Depends(db), c=Depends(ctx)):
    c.permit("operator", "admin")
    content = file.file.read(MAX_BYTES+1)
    parsed = parse_file(content, file.filename or "file", encoding)
    filename = Path((file.filename or "file").replace("\\", "/")).name[:250]
    obj = File(id=uid(), org_id=c.org_id, name=filename, object_key=storage.put(c.org_id, content, Path(filename).suffix[1:]), sha256=digest(content), size=len(content), encoding=encoding, sheets=list(parsed))
    s.add(obj)
    s.flush()
    audit(s, c, "file.upload", obj.id, {"bytes": obj.size})
    return data(obj, ("object_key",))


@app.get(PREFIX+"/files/{ident}/preview")
def file_preview(ident: str, sheet: str, header_row: int = 1, s=Depends(db), c=Depends(ctx)):
    file = scoped(s, File, ident, c)
    return {**preview(parse_file(storage.read(file.object_key), file.name, file.encoding), sheet, header_row), "fields": FIELDS}


@app.post(PREFIX+"/imports/validate")
def validate_import(body: ImportConfig, catalog: bool = False, s=Depends(db), c=Depends(ctx)):
    c.permit("operator", "admin")
    _, rows = load_rows(s, c, body.model_dump(), catalog)
    errors = [r["row_no"] for r in rows if any(i["level"] == "error" for i in r["issues"])]
    return {"total": len(rows), "valid": len(rows)-len(errors), "error_rows": errors, "warning_count": sum(any(i["level"] == "warning" for i in r["issues"]) for r in rows), "rows": sorted(rows, key=lambda r: (not any(i["level"] == "error" for i in r["issues"]), r["row_no"]))[:100]}


@app.post(PREFIX+"/catalogs", status_code=201)
def catalogs_create(body: Named, s=Depends(db), c=Depends(ctx)):
    c.permit("operator", "admin")
    obj = Catalog(org_id=c.org_id, name=body.name)
    s.add(obj)
    s.flush()
    return data(obj)


@app.get(PREFIX+"/catalogs")
def catalogs_list(s=Depends(db), c=Depends(ctx)):
    cats = s.scalars(select(Catalog).where(Catalog.org_id == c.org_id).order_by(Catalog.created_at.desc())).all()
    return {"items": [{**data(cat), "versions": [data(v) for v in s.scalars(select(CatalogVersion).where(CatalogVersion.org_id == c.org_id, CatalogVersion.catalog_id == cat.id).order_by(CatalogVersion.number.desc()))]} for cat in cats]}


@app.post(PREFIX+"/catalogs/{ident}/versions", status_code=202)
def catalog_version_create(ident: str, body: ImportConfig, s=Depends(db), c=Depends(ctx)):
    c.permit("operator", "admin")
    return data(create_catalog_version(s, c, scoped(s, Catalog, ident, c), body.model_dump()))


@app.post(PREFIX+"/catalog-versions/{ident}/publish")
def publish(ident: str, body: VersionInput, s=Depends(db), c=Depends(ctx)):
    c.permit("admin")
    version = scoped(s, CatalogVersion, ident, c, True)
    require(version.lock_version == body.expected_version, 409, "VERSION_CONFLICT", "库版本已更新")
    require(version.status == "DRAFT" and not version.errors, 409, "CATALOG_INVALID", "此版本存在错误或已发布")
    version.status, version.lock_version = "PUBLISHED", version.lock_version+1
    audit(s, c, "catalog.publish", version.id)
    return data(version)


@app.get(PREFIX+"/catalog-versions/{ident}/products")
def products_list(ident: str, q: str = "", cursor: str | None = None, limit: int = Query(50, ge=1, le=100), s=Depends(db), c=Depends(ctx)):
    scoped(s, CatalogVersion, ident, c)
    query = select(Product).where(Product.org_id == c.org_id, Product.version_id == ident)
    if q:
        from sqlalchemy import cast, String, or_
        query = query.where(or_(Product.sku.icontains(q, autoescape=True), cast(Product.normalized, String).icontains(q, autoescape=True)))
    rows, cursor = page(s, query, Product, cursor, limit)
    return {"items": [data(x) for x in rows], "next_cursor": cursor}


@app.post(PREFIX+"/batches", status_code=201)
def batch_create(body: BatchInput, s=Depends(db), c=Depends(ctx)):
    c.permit("operator", "admin")
    batch = Batch(id=uid(), org_id=c.org_id, name=body.name, supplier=body.supplier, created_by=c.user_id)
    s.add(batch)
    s.flush()
    revision = create_revision(s, c, batch, body.model_dump(exclude={"name", "supplier"}))
    return {"batch": data(batch), "revision": data(revision)}


@app.get(PREFIX+"/batches")
def batches_list(s=Depends(db), c=Depends(ctx)):
    batches = s.scalars(select(Batch).where(Batch.org_id == c.org_id).order_by(Batch.created_at.desc()).limit(100)).all()
    return {"items": [{**data(b), "revisions": [data(r) for r in s.scalars(select(Revision).where(Revision.org_id == c.org_id, Revision.batch_id == b.id).order_by(Revision.number.desc()))]} for b in batches]}


@app.post(PREFIX+"/batches/{ident}/revisions", status_code=202)
def revision_create(ident: str, body: ImportConfig, s=Depends(db), c=Depends(ctx)):
    c.permit("operator", "admin")
    return data(create_revision(s, c, scoped(s, Batch, ident, c, True), body.model_dump()))


@app.get(PREFIX+"/revisions/{ident}/preview")
def revision_preview(ident: str, cursor: str | None = None, limit: int = Query(50, ge=1, le=100), s=Depends(db), c=Depends(ctx)):
    revision = scoped(s, Revision, ident, c)
    rows, cursor = page(s, select(Source).where(Source.org_id == c.org_id, Source.revision_id == ident), Source, cursor, limit)
    return {"revision": data(revision), "items": [data(r) for r in rows], "next_cursor": cursor}


def run_data(s, c, run):
    revision = scoped(s, Revision, run.revision_id, c)
    batch = scoped(s, Batch, revision.batch_id, c)
    actor = s.get(User, run.created_by)
    groups = dict(s.execute(select(Item.suggestion, func.count()).where(Item.org_id == c.org_id, Item.run_id == run.id).group_by(Item.suggestion)).all())
    decisions = dict(s.execute(select(Item.status, func.count()).where(Item.org_id == c.org_id, Item.run_id == run.id).group_by(Item.status)).all())
    return {**data(run), "batch_name": batch.name, "batch_id": batch.id, "supplier": batch.supplier, "revision_number": revision.number, "creator_name": actor.name, "suggestions": groups, "decisions": decisions}


@app.post(PREFIX+"/runs", status_code=202)
def runs_create(body: RunInput, request: Request, s=Depends(db), c=Depends(ctx)):
    return idempotent(s, c, "runs", request.headers.get("idempotency-key"), body.model_dump(), lambda: create_run(s, c, body.model_dump()))


@app.get(PREFIX+"/runs")
def runs_list(status: str | None = None, q: str = "", cursor: str | None = None, limit: int = Query(50, ge=1, le=100), s=Depends(db), c=Depends(ctx)):
    query = select(Run).where(Run.org_id == c.org_id)
    if status:
        query = query.where(Run.status == status)
    if q:
        query = query.join(Revision, (Revision.id == Run.revision_id) & (Revision.org_id == Run.org_id)).join(Batch, (Batch.id == Revision.batch_id) & (Batch.org_id == Run.org_id)).where(Batch.name.icontains(q, autoescape=True) | Batch.supplier.icontains(q, autoescape=True))
    rows, cursor = page(s, query, Run, cursor, limit)
    return {"items": [run_data(s, c, r) for r in rows], "next_cursor": cursor}


@app.get(PREFIX+"/runs/{ident}")
def runs_detail(ident: str, s=Depends(db), c=Depends(ctx)):
    return run_data(s, c, scoped(s, Run, ident, c))


@app.post(PREFIX+"/runs/{ident}/cancel", status_code=202)
def cancel(ident: str, body: Reason, s=Depends(db), c=Depends(ctx)):
    c.permit("operator", "admin")
    run = scoped(s, Run, ident, c, True)
    require(run.status in ("QUEUED", "RUNNING", "CANCEL_REQUESTED"), 409, "INVALID_TRANSITION", "任务已结束，不能取消")
    run.status, run.cancel_reason = "CANCEL_REQUESTED", body.reason
    audit(s, c, "run.cancel", run.id)
    return {"id": run.id, "status": run.status}


@app.post(PREFIX+"/runs/{ident}/retry", status_code=202)
def retry(ident: str, request: Request, s=Depends(db), c=Depends(ctx)):
    old = scoped(s, Run, ident, c)
    require(old.status in ("FAILED", "CANCELLED"), 409, "INVALID_TRANSITION", "仅失败或取消的任务可以重试")
    body = {"revision_id": old.revision_id, "catalog_version_id": old.catalog_version_id, "policy_id": old.policy_id}
    return idempotent(s, c, f"retry:{ident}", request.headers.get("idempotency-key"), body, lambda: create_run(s, c, body, old.id))


@app.get(PREFIX+"/runs/{ident}/compare/{other_id}")
def compare_runs(ident: str, other_id: str, s=Depends(db), c=Depends(ctx)):
    left, right = scoped(s, Run, ident, c), scoped(s, Run, other_id, c)
    require(scoped(s, Revision, left.revision_id, c).batch_id == scoped(s, Revision, right.revision_id, c).batch_id, 422, "INCOMPARABLE_RUNS", "请选择同一批次的运行版本")
    def indexed(run):
        result = {}
        for item, source in s.execute(select(Item, Source).join(Source, (Source.id == Item.source_id) & (Source.org_id == Item.org_id)).where(Item.org_id == c.org_id, Item.run_id == run.id)):
            best = s.scalar(select(Candidate).where(Candidate.org_id == c.org_id, Candidate.item_id == item.id, Candidate.rank == 1))
            product = scoped(s, Product, best.product_id, c) if best else None
            # Original row identity is explicit; edited/reordered files need manual comparison.
            result[(source.row_no, source.sku)] = {"suggestion": item.suggestion, "status": item.status, "target_sku": product.sku if product else None, "source_hash": digest(source.raw)}
        return result
    a, b = indexed(left), indexed(right)
    keys = sorted(set(a) | set(b))
    return {"changes": [{"row_no": k[0], "sku": k[1], "before": a.get(k), "after": b.get(k)} for k in keys if a.get(k) != b.get(k)], "manifest_before": left.manifest, "manifest_after": right.manifest, "identity": "原始行号与来源编号；重排记录显示为新增或移除"}


def item_data(s, c, item):
    source = scoped(s, Source, item.source_id, c)
    candidates = s.scalars(select(Candidate).where(Candidate.org_id == c.org_id, Candidate.item_id == item.id).order_by(Candidate.rank).limit(5)).all()
    event = scoped(s, ReviewEvent, item.current_decision_id, c) if item.current_decision_id else None
    return {**data(item), "source": data(source), "candidates": [{**data(cand), "product": data(scoped(s, Product, cand.product_id, c))} for cand in candidates], "decision": data(event) if event else None}


@app.get(PREFIX+"/runs/{ident}/items")
def items_list(ident: str, suggestion: str | None = None, status: str | None = None, q: str = "", cursor: str | None = None, limit: int = Query(50, ge=1, le=100), s=Depends(db), c=Depends(ctx)):
    scoped(s, Run, ident, c)
    query = select(Item).where(Item.org_id == c.org_id, Item.run_id == ident)
    if suggestion:
        query = query.where(Item.suggestion == suggestion)
    if status:
        query = query.where(Item.status == status)
    if q:
        from sqlalchemy import cast, String
        query = query.join(Source, (Source.id == Item.source_id) & (Source.org_id == Item.org_id)).where(Source.sku.icontains(q, autoescape=True) | cast(Source.normalized, String).icontains(q, autoescape=True))
    rows, cursor = page(s, query, Item, cursor, limit)
    return {"items": [item_data(s, c, x) for x in rows], "next_cursor": cursor}


@app.get(PREFIX+"/items/{ident}/candidates")
def candidates(ident: str, s=Depends(db), c=Depends(ctx)):
    item = scoped(s, Item, ident, c)
    return item_data(s, c, item)


@app.get(PREFIX+"/items/{ident}/history")
def history(ident: str, s=Depends(db), c=Depends(ctx)):
    item = scoped(s, Item, ident, c)
    events = s.scalars(select(ReviewEvent).where(ReviewEvent.org_id == c.org_id, ReviewEvent.item_id == item.id).order_by(ReviewEvent.seq.desc())).all()
    source = scoped(s, Source, item.source_id, c)
    previous = s.execute(select(Item, Run).join(Run, (Run.id == Item.run_id) & (Run.org_id == Item.org_id)).where(Item.org_id == c.org_id, Item.source_id == source.id, Item.run_id != item.run_id).order_by(Run.created_at.desc()).limit(10)).all()
    return {"items": [{**data(e), "actor_name": s.get(User, e.actor_id).name} for e in events], "previous_runs": [{"run_id": r.id, "status": i.status, "suggestion": i.suggestion} for i, r in previous]}


@app.post(PREFIX+"/items/{ident}/decisions", status_code=201)
def decision_create(ident: str, body: Decision, s=Depends(db), c=Depends(ctx)):
    return decide(s, c, ident, body.model_dump())


@app.post(PREFIX+"/runs/{ident}/bulk-decisions")
def bulk_decisions(ident: str, body: Bulk, request: Request):
    require(len({i.item_id for i in body.items}) == len(body.items), 422, "DUPLICATE_ITEMS", "批量条目不能重复")
    batch_key = request.headers.get("idempotency-key")
    require(batch_key and len(batch_key) <= 100, 422, "IDEMPOTENCY_REQUIRED", "请提供有效的 Idempotency-Key")
    route, hashed = f"bulk:{ident}", digest(body.model_dump())
    def authorized(s):
        actor = authenticate(s, request.headers.get("authorization", ""))
        c = context(s, actor, request.headers.get("x-organization-id", ""), request.state.request_id, write=True)
        c.permit("reviewer")
        scoped(s, Run, ident, c)
        return c
    with transaction(write=True) as s:
        c = authorized(s)
        batch = s.scalar(select(Idempotency).where(Idempotency.org_id == c.org_id, Idempotency.user_id == c.user_id, Idempotency.route == route, Idempotency.key == batch_key))
        if batch:
            require(batch.request_hash == hashed, 409, "IDEMPOTENCY_CONFLICT", "此幂等键已用于不同请求")
            if "results" in batch.response:
                return batch.response
        else:
            s.add(Idempotency(org_id=c.org_id, user_id=c.user_id, route=route, key=batch_key, request_hash=hashed, response={"processing": True}))
    results = []
    # Each row commits separately. Per-row receipts make a crash halfway replay-safe.
    for entry in body.items:
        with transaction(write=True) as s:
            c = authorized(s)
            def perform_row():
                try:
                    with s.begin_nested():
                        item = scoped(s, Item, entry.item_id, c)
                        require(item.run_id == ident, 404, "NOT_FOUND", "资源不存在或无权访问")
                        response = decide(s, c, item.id, entry.model_dump(exclude={"item_id"}), True)
                    return {"item_id": entry.item_id, "ok": True, **response}
                except Problem as e:
                    return {"item_id": entry.item_id, "ok": False, "code": e.code, "message": e.message, "details": e.details}
            result = idempotent(s, c, f"{route}:{entry.item_id}", batch_key, entry.model_dump(), perform_row)
            results.append(result)
    result = {"results": results, "succeeded": sum(r["ok"] for r in results), "failed": sum(not r["ok"] for r in results)}
    with transaction(write=True) as s:
        c = authorized(s)
        batch = s.scalar(select(Idempotency).where(Idempotency.org_id == c.org_id, Idempotency.user_id == c.user_id, Idempotency.route == route, Idempotency.key == batch_key))
        batch.response = result
    return result


@app.post(PREFIX+"/runs/{ident}/exports", status_code=202)
def export_create(ident: str, body: ExportInput, request: Request, s=Depends(db), c=Depends(ctx)):
    return idempotent(s, c, f"exports:{ident}", request.headers.get("idempotency-key"), body.model_dump(), lambda: export_snapshot(s, c, ident, body.model_dump()))


@app.get(PREFIX+"/exports")
def exports_list(cursor: str | None = None, limit: int = Query(50, ge=1, le=100), s=Depends(db), c=Depends(ctx)):
    rows, cursor = page(s, select(Export).where(Export.org_id == c.org_id), Export, cursor, limit)
    result = []
    for e in rows:
        changed = s.scalar(select(func.count()).select_from(ExportRow).join(Item, (Item.id == ExportRow.item_id) & (Item.org_id == ExportRow.org_id)).where(ExportRow.org_id == c.org_id, ExportRow.export_id == e.id, Item.version != ExportRow.item_version))
        result.append({**data(e, ("object_key",)), "stale": bool(changed), "changed_decisions": changed, "creator_name": s.get(User, e.created_by).name})
    return {"items": result, "next_cursor": cursor}


@app.get(PREFIX+"/exports/{ident}/download")
def download(ident: str, s=Depends(db), c=Depends(ctx)):
    e = scoped(s, Export, ident, c)
    require(e.status == "SUCCEEDED" and e.object_key, 409, "EXPORT_NOT_READY", "导出文件尚未生成")
    require(e.expires_at > time.time(), 410, "EXPORT_EXPIRED", "文件已到期，请生成新导出")
    content = storage.read(e.object_key)
    require(digest(content) == e.file_hash, 409, "FILE_HASH_MISMATCH", "导出文件校验失败")
    filename = f"商品匹配_{e.kind}_{e.id[:8]}.{e.format}"
    return StreamingResponse(io.BytesIO(content), media_type="text/csv; charset=utf-8" if e.format == "csv" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename)})


@app.get(PREFIX+"/settings")
def settings(s=Depends(db), c=Depends(ctx)):
    return data(s.get(Organization, c.org_id))


@app.put(PREFIX+"/settings")
def settings_update(body: SettingsInput, s=Depends(db), c=Depends(ctx)):
    c.permit("admin")
    scoped(s, Policy, body.default_policy_id, c)
    org = s.get(Organization, c.org_id)
    org.dual_review, org.default_policy_id = body.dual_review, body.default_policy_id
    audit(s, c, "settings.update", org.id, body.model_dump())
    return data(org)


@app.get(PREFIX+"/policies")
def policies(s=Depends(db), c=Depends(ctx)):
    return {"items": [data(p) for p in s.scalars(select(Policy).where(Policy.org_id == c.org_id).order_by(Policy.created_at.desc()))]}


@app.get(PREFIX+"/models")
def models(s=Depends(db), c=Depends(ctx)):
    c.permit("admin")
    return {"items": [data(m, ("artifact_path",)) for m in s.scalars(select(ModelVersion))]}


@app.post(PREFIX+"/policies", status_code=201)
def policy_create(body: PolicyInput, s=Depends(db), c=Depends(ctx)):
    c.permit("admin")
    config = {**DEFAULT_POLICY, "engine": body.engine, "high_threshold": body.high_threshold, "margin": body.margin}
    if body.engine == "lightgbm":
        model = s.get(ModelVersion, body.model_id)
        require(model and model.status == "AVAILABLE", 409, "MODEL_NOT_READY", "模型尚未通过准入评测")
        require(digest(Path(model.artifact_path).read_bytes()) == model.artifact_hash, 409, "MODEL_HASH_MISMATCH", "模型文件校验失败")
        config.update({"model_path": model.artifact_path, "model_hash": model.artifact_hash, "feature_schema": model.schema_version, "calibration": model.metrics.get("calibration"), "validated": True})
    p = Policy(org_id=c.org_id, name=body.name, config=config)
    s.add(p)
    s.flush()
    audit(s, c, "policy.create", p.id)
    return data(p)


@app.get(PREFIX+"/members")
def members(s=Depends(db), c=Depends(ctx)):
    c.permit("admin")
    rows = s.execute(select(Membership, User).join(User, User.id == Membership.user_id).where(Membership.org_id == c.org_id)).all()
    return {"items": [{**data(m), "name": u.name, "email": u.email} for m, u in rows]}


@app.post(PREFIX+"/members", status_code=201)
def member_create(body: MemberInput, s=Depends(db), c=Depends(ctx)):
    c.permit("admin")
    # Never reveal or attach existing global users via unverified email.
    require(not s.scalar(select(User).where(User.email == body.email.strip().lower())), 409, "ACCOUNT_UNAVAILABLE", "此账号无法创建，请使用新账号")
    u = User(id=uid(), email=body.email.strip().lower(), name=body.name, password_hash=password_hash(body.password))
    s.add(u)
    s.flush()
    m = Membership(org_id=c.org_id, user_id=u.id, roles=body.roles)
    s.add(m)
    s.flush()
    audit(s, c, "member.create", m.id)
    return data(m)


@app.put(PREFIX+"/members/{ident}")
def member_update(ident: str, body: MemberUpdate, s=Depends(db), c=Depends(ctx)):
    c.permit("admin")
    m = scoped(s, Membership, ident, c, True)
    if "admin" in m.roles and (not body.active or "admin" not in body.roles):
        others = s.scalars(select(Membership).where(Membership.org_id == c.org_id, Membership.active.is_(True), Membership.id != m.id)).all()
        require(any("admin" in x.roles for x in others), 409, "LAST_ADMIN", "组织至少保留一名有效管理员")
    m.roles, m.active = body.roles, body.active
    audit(s, c, "member.update", m.id, body.model_dump())
    return data(m)


@app.get(PREFIX+"/audit")
def audit_list(cursor: str | None = None, limit: int = Query(50, ge=1, le=100), s=Depends(db), c=Depends(ctx)):
    c.permit("admin")
    rows, cursor = page(s, select(Audit).where(Audit.org_id == c.org_id), Audit, cursor, limit)
    return {"items": [{**data(a), "actor_name": s.get(User, a.actor_id).name} for a in rows], "next_cursor": cursor}


@app.get(PREFIX+"/health")
def health():
    with transaction() as s:
        s.execute(text("SELECT 1"))
    return {"status": "ok", "version": "1.0.0"}


@app.get(PREFIX+"/readiness")
def readiness():
    with transaction() as s:
        s.execute(text("SELECT 1"))
    queue = "database"
    if QUEUE_MODE == "celery":
        try:
            import redis
            redis.Redis.from_url(REDIS_URL, socket_connect_timeout=1, socket_timeout=1).ping()
            queue = "online"
        except Exception:
            queue = "waiting_for_redis"
    from packages.domain.config import STORAGE_BACKEND, DATA_DIR
    import os
    try:
        if STORAGE_BACKEND == "s3":
            storage.client().head_bucket(Bucket=os.getenv("S3_BUCKET", "product-match"))
        else:
            require(os.access(DATA_DIR, os.R_OK | os.W_OK), 503, "STORAGE_UNAVAILABLE", "存储不可用")
        storage_state = "ok"
    except Exception:
        storage_state = "unavailable"
    result = {"database": "ok", "storage": storage_state, "queue": queue, "status": "unavailable" if storage_state != "ok" else "degraded" if queue == "waiting_for_redis" else "ready"}
    return JSONResponse(result, status_code=503 if storage_state != "ok" else 200)


@app.get(PREFIX+"/metrics")
def metrics(s=Depends(db), c=Depends(ctx)):
    c.permit("admin")
    values = s.execute(select(Run.status, func.count()).where(Run.org_id == c.org_id).group_by(Run.status)).all()
    lines = [f'product_match_runs{{status="{status}"}} {count}' for status, count in values]
    expired = s.scalar(select(func.count()).select_from(Chunk).where(Chunk.org_id == c.org_id, Chunk.status == "RUNNING", Chunk.lease_until < time.time()))
    lines.append(f"product_match_expired_leases {expired}")
    return Response("\n".join(lines)+"\n", media_type="text/plain")


static = ROOT / "apps/web/dist"
if static.exists():
    app.mount("/", StaticFiles(directory=static, html=True), name="web")
