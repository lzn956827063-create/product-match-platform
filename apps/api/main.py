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
from packages.domain import telemetry
from packages.domain import storage
from packages.domain.imports import request_job, read_result, parsed_file
from packages.domain.corrections import save_draft, submit_drafts, source_fields
from packages.domain.auth import authenticate, context, issue_session, password_hash, scoped, token_hash, verify_password
from packages.domain.config import ALLOWED_ORIGINS, COOKIE_SECURE, QUEUE_MODE, REDIS_URL, ROOT
from packages.domain.db import initialize, now, transaction, uid
from packages.domain.errors import Problem, require
from packages.domain.ingest import FIELDS, MAX_BYTES, parse_file, preview
from packages.domain.models import *
from packages.domain.services import audit, create_catalog_version, create_revision, create_run, decide, export_snapshot, idempotent, load_rows
from packages.domain.queries import item_page, item_summaries, run_page, export_page, compare_versions
from packages.matching.engine import DEFAULT_POLICY
from packages.matching.normalize import digest

# Inherit Uvicorn's configured INFO handler in both local and container starts.
log = logging.getLogger("uvicorn.error.product_match")


@asynccontextmanager
async def lifespan(app):
    initialize()
    yield


from packages.domain import request_trace

app = FastAPI(default_response_class=request_trace.MeasuredJSONResponse,title="商品数据匹配与核对平台", version="1.3.1", openapi_url="/api/v1/openapi.json", docs_url="/api/v1/docs", lifespan=lifespan)
PREFIX = "/api/v1"


@app.middleware("http")
async def request_context(request, call_next):
    request.state.request_id = uid()
    request_trace.start(request.state.request_id)
    started = time.perf_counter()
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("origin") and request.headers["origin"] not in ALLOWED_ORIGINS:
        return JSONResponse({"code": "ORIGIN_DENIED", "message": "请求来源未获授权", "request_id": request.state.request_id}, status_code=403)
    if request.url.path.startswith(PREFIX) and not request.url.path.startswith(PREFIX+'/auth') and not request.url.path.startswith(PREFIX+'/service/') and request.headers.get('authorization') and request.headers.get('x-organization-id'):
        from starlette.concurrency import run_in_threadpool
        def throttle():
            with transaction(write=True) as s:
                actor=authenticate(s,request.headers['authorization'])
                c=context(s,actor,request.headers['x-organization-id'],request.state.request_id,write=True)
                from packages.domain.quotas import count_request
                count_request(s,c)
        try:await run_in_threadpool(throttle)
        except Problem as exc:return await problem_handler(request,exc)
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
    log.info(json.dumps({"event": "request", "request_id": request.state.request_id, "method": request.method, "path": request.url.path, "status": response.status_code, "seconds": round(time.perf_counter()-started, 4),"response_bytes":response.headers.get("content-length"),**(request_trace.current.get() or {})}))
    route = request.scope.get('route')
    if route and getattr(route, 'path', '').startswith(PREFIX):
        telemetry.observe(route.path, request.method, response.status_code, time.perf_counter()-started)
    return response


@app.exception_handler(Problem)
async def problem_handler(request, exc):
    return JSONResponse({"code": exc.code, "message": exc.message, "details": exc.details, "request_id": request.state.request_id}, status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def validation_handler(request, exc):
    return JSONResponse({"code": "VALIDATION_ERROR", "message": "请求字段格式不正确", "details": [{"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()], "request_id": request.state.request_id}, status_code=422)


def db(request: Request):
    with transaction(write=request.method not in ("GET", "HEAD") or request.url.path.startswith(PREFIX+"/service/")) as s:
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


@app.post(PREFIX+"/files", status_code=202)
def upload(file: UploadFile = Upload(...), encoding: str = "utf-8", s=Depends(db), c=Depends(ctx)):
    c.permit("operator", "admin")
    content = file.file.read(MAX_BYTES+1)
    filename = Path((file.filename or "file").replace("\\", "/")).name[:250]
    require(len(content)<=MAX_BYTES, 413, "FILE_TOO_LARGE", "文件不能超过 20MB")
    require(Path(filename).suffix.lower() in (".csv", ".xlsx"), 422, "INVALID_FILE", "仅支持 CSV 和 XLSX")
    require(encoding in ("utf-8", "gb18030"), 422, "INVALID_ENCODING", "请选择有效编码")
    obj = File(id=uid(), org_id=c.org_id, name=filename, object_key=storage.quota_put(c.org_id, content, Path(filename).suffix[1:], s=s), sha256=digest(content), size=len(content), encoding=encoding, sheets=[])
    s.add(obj);s.flush()
    job = request_job(s, c, obj, "parse")
    audit(s, c, "file.upload", obj.id, {"bytes": obj.size})
    return {**data(obj, ("object_key",)), "job_id": job.id, "status": job.status}


@app.get(PREFIX+"/files/{ident}/preview")
def file_preview(ident: str, sheet: str, header_row: int = 1, s=Depends(db), c=Depends(ctx)):
    file = scoped(s, File, ident, c)
    return {**preview(parsed_file(s, c, file, allow_sync=True), sheet, header_row), "fields": FIELDS}


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
def catalogs_list(q: str = "", cursor: str | None = None, limit: int = Query(50, ge=1, le=100), s=Depends(db), c=Depends(ctx)):
    cats, cursor = page(s, select(Catalog).where(Catalog.org_id == c.org_id, Catalog.name.icontains(q, autoescape=True)), Catalog, cursor, limit)
    versions = {}
    for v in s.scalars(select(CatalogVersion).where(CatalogVersion.org_id == c.org_id, CatalogVersion.catalog_id.in_([x.id for x in cats])).order_by(CatalogVersion.number.desc())):
        versions.setdefault(v.catalog_id, []).append(data(v))
    active={x.catalog_id:x.version_id for x in s.scalars(select(CatalogActivation).where(CatalogActivation.org_id==c.org_id))}
    return {"items": [{**data(cat), "active_version_id":active.get(cat.id), "versions": versions.get(cat.id, [])} for cat in cats], "next_cursor": cursor}


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
def products_list(ident: str, q: str = "", mode: str = "auto", cursor: str | None = None, limit: int = Query(50, ge=1, le=100), s=Depends(db), c=Depends(ctx)):
    scoped(s, CatalogVersion, ident, c)
    query = select(Product).where(Product.org_id == c.org_id, Product.version_id == ident)
    require(mode in ('auto','exact','prefix','fuzzy'),422,'SEARCH_MODE','不支持此搜索方式')
    used=mode
    if q:
        from sqlalchemy import or_
        q=q.strip()[:300]
        if mode=='auto':
            used='exact' if s.scalar(select(Product.id).where(Product.org_id==c.org_id,Product.version_id==ident,Product.sku==q).limit(1)) else 'fuzzy'
        if used=='exact':query=query.where(Product.sku==q)
        elif used=='prefix':query=query.where(Product.search_model>=q.casefold(),Product.search_model<q.casefold()+'\U0010ffff')
        else:query=query.where(or_(Product.sku.icontains(q,autoescape=True),Product.search_name.contains(q.casefold(),autoescape=True),Product.search_model.contains(q.casefold(),autoescape=True)))
    rows, cursor = page(s, query, Product, cursor, limit)
    return {"items": [data(x) for x in rows], "next_cursor": cursor,"search_mode":used}


@app.post(PREFIX+"/batches", status_code=201)
def batch_create(body: BatchInput, request: Request, s=Depends(db), c=Depends(ctx)):
    c.permit("operator", "admin")
    def operation():
        batch = Batch(id=uid(), org_id=c.org_id, name=body.name, supplier=body.supplier, created_by=c.user_id)
        s.add(batch)
        s.flush()
        revision = create_revision(s, c, batch, body.model_dump(exclude={"name", "supplier"}))
        return {"batch": data(batch), "revision": data(revision)}
    return idempotent(s, c, "batches", request.headers.get("idempotency-key") or digest(body.model_dump()), body.model_dump(), operation)


@app.get(PREFIX+"/batches")
def batches_list(q: str = "", cursor: str | None = None, limit: int = Query(50, ge=1, le=100), s=Depends(db), c=Depends(ctx)):
    batches, cursor = page(s, select(Batch).where(Batch.org_id == c.org_id, Batch.name.icontains(q, autoescape=True) | Batch.supplier.icontains(q, autoescape=True)), Batch, cursor, limit)
    revisions = {}
    for r in s.scalars(select(Revision).where(Revision.org_id == c.org_id, Revision.batch_id.in_([b.id for b in batches])).order_by(Revision.number.desc())):
        revisions.setdefault(r.batch_id, []).append(data(r))
    return {"items": [{**data(b), "revisions": revisions.get(b.id, [])} for b in batches], "next_cursor": cursor}


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
    return run_page(s, c, [run])[0]


@app.post(PREFIX+"/runs", status_code=202)
def runs_create(body: RunInput, request: Request, s=Depends(db), c=Depends(ctx)):
    return idempotent(s, c, "runs", request.headers.get("idempotency-key"), body.model_dump(), lambda: create_run(s, c, body.model_dump()))


@app.get(PREFIX+"/runs")
def runs_list(status: str | None = None, q: str = "", cursor: str | None = None, limit: int = Query(50, ge=1, le=100), batch_id: str | None = None, s=Depends(db), c=Depends(ctx)):
    query = select(Run).where(Run.org_id == c.org_id)
    if batch_id:
        query = query.where(Run.revision_id.in_(select(Revision.id).where(Revision.org_id==c.org_id, Revision.batch_id==batch_id)))
    if status:
        query = query.where(Run.status == status)
    if q:
        query = query.join(Revision, (Revision.id == Run.revision_id) & (Revision.org_id == Run.org_id)).join(Batch, (Batch.id == Revision.batch_id) & (Batch.org_id == Run.org_id)).where(Batch.name.icontains(q, autoescape=True) | Batch.supplier.icontains(q, autoescape=True))
    rows, cursor = page(s, query, Run, cursor, limit)
    return {"items": run_page(s, c, rows), "next_cursor": cursor}


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
def compare_runs(ident: str, other_id: str, cursor: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100), s=Depends(db), c=Depends(ctx)):
    left, right = scoped(s, Run, ident, c), scoped(s, Run, other_id, c)
    require(scoped(s, Revision, left.revision_id, c).batch_id == scoped(s, Revision, right.revision_id, c).batch_id, 422, "INCOMPARABLE_RUNS", "请选择同一批次的运行版本")
    return compare_versions(s, c, left, right, cursor, limit)


def item_data(s, c, item):
    return item_page(s, c, [item])[0]


@app.get(PREFIX+"/runs/{ident}/items")
def items_list(ident: str, suggestion: str | None = None, status: str | None = None, q: str = "", cursor: str | None = None, limit: int = Query(50, ge=1, le=100), s=Depends(db), c=Depends(ctx), details: bool = True):
    scoped(s, Run, ident, c)
    query = select(Item).where(Item.org_id == c.org_id, Item.run_id == ident)
    if suggestion:
        query = query.where(Item.suggestion == suggestion)
    if status:
        query = query.where(Item.status == status)
    if q:
        from sqlalchemy import cast, String
        query = query.join(Source, (Source.id == Item.source_id) & (Source.org_id == Item.org_id)).where(Source.sku.icontains(q, autoescape=True) | Source.normalized["name"].as_string().icontains(q, autoescape=True))
    rows, cursor = page(s, query, Item, cursor, limit)
    return {"items": (item_page if details else item_summaries)(s, c, rows), "next_cursor": cursor}


@app.get(PREFIX+"/items/{ident}/candidates")
def candidates(ident: str, s=Depends(db), c=Depends(ctx)):
    item = scoped(s, Item, ident, c)
    return item_data(s, c, item)


@app.get(PREFIX+"/items/{ident}/history")
def history(ident: str, cursor: int | None = None, limit: int = Query(50, ge=1, le=100), s=Depends(db), c=Depends(ctx)):
    item = scoped(s, Item, ident, c)
    query = select(ReviewEvent, User.name).join(User, User.id == ReviewEvent.actor_id).where(ReviewEvent.org_id == c.org_id, ReviewEvent.item_id == item.id)
    if cursor is not None:
        query = query.where(ReviewEvent.seq < cursor)
    events = s.execute(query.order_by(ReviewEvent.seq.desc()).limit(limit+1)).all()
    previous = s.execute(select(Item, Run).join(Run, (Run.id == Item.run_id) & (Run.org_id == Item.org_id)).where(Item.org_id == c.org_id, Item.source_id == item.source_id, Item.run_id != item.run_id).order_by(Run.created_at.desc()).limit(10)).all()
    return {"items": [{**data(e), "actor_name": name} for e, name in events[:limit]], "next_cursor": events[limit-1][0].seq if len(events)>limit else None, "previous_runs": [{"run_id": r.id, "status": i.status, "suggestion": i.suggestion} for i, r in previous]}


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
    return {"items": export_page(s, c, rows), "next_cursor": cursor}


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
        from packages.matching.policy import admission, validate_bundle
        evaluation = scoped(s, PolicyEvaluation, body.evaluation_id or "", c)
        require(digest(evaluation.report) == evaluation.report_hash, 409, "REPORT_HASH_MISMATCH", "评测报告校验失败")
        try:
            gate = admission(evaluation.report, evaluation.config)
        except (ValueError, KeyError, OSError) as exc:
            raise Problem(409, "BUNDLE_INVALID", "完整策略制品校验失败") from exc
        require(gate['eligible'], 409, "POLICY_NOT_READY", "此完整策略未通过手机业务准入", gate)
        require(body.high_threshold == evaluation.config['high_threshold'] and body.margin == evaluation.config['margin'], 409, "POLICY_REPORT_MISMATCH", "阈值或分差变化后需要重新评测")
        config = {**evaluation.config, "validated": True, "admission": {**gate, "evaluation_id": evaluation.id, "report_hash": evaluation.report_hash}}

    p = Policy(org_id=c.org_id, name=body.name, config=config)
    s.add(p)
    s.flush()
    audit(s, c, "policy.create", p.id)
    return data(p)


@app.get(PREFIX+"/members")
def members(q: str = "", cursor: str | None = None, limit: int = Query(50, ge=1, le=100), s=Depends(db), c=Depends(ctx)):
    c.permit("admin")
    query = select(Membership, User).join(User, User.id == Membership.user_id).where(Membership.org_id == c.org_id, User.name.icontains(q, autoescape=True) | User.email.icontains(q, autoescape=True))
    if cursor:
        query = query.where(Membership.id > cursor)
    rows = s.execute(query.order_by(Membership.id).limit(limit+1)).all()
    return {"items": [{**data(m), "name": u.name, "email": u.email} for m, u in rows[:limit]], "next_cursor": rows[limit-1][0].id if len(rows)>limit else None}


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
    names = dict(s.execute(select(Audit.id, User.name).join(User, User.id == Audit.actor_id).where(Audit.org_id == c.org_id, Audit.id.in_([a.id for a in rows]))).all())
    return {"items": [{**data(a), "actor_name": names[a.id]} for a in rows], "next_cursor": cursor}


@app.get(PREFIX+"/live")
def liveness():
    return {"status":"alive"}


@app.get(PREFIX+"/internal/metrics")
def internal_metrics(request: Request):
    from packages.domain.monitoring import authorized,render
    require(authorized(request.headers.get('authorization','')),403,'METRICS_AUTH','监控身份无效')
    return Response(render(),media_type='text/plain')


@app.get(PREFIX+"/health")
def health():
    with transaction() as s:
        s.execute(text("SELECT 1"))
    return {"status": "ok", "version": "1.3.1"}


@app.get(PREFIX+"/readiness")
def readiness():
    try:
        with transaction() as s:s.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse({"status":"unavailable","database":"unavailable","operations":[]},status_code=503)
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
    ready = storage_state == "ok" and queue != "waiting_for_redis"
    result = {"database": "ok", "storage": storage_state, "queue": queue, "status": "ready" if ready else "unavailable"}
    return JSONResponse(result, status_code=200 if ready else 503)


@app.get(PREFIX+"/metrics")
def metrics(s=Depends(db), c=Depends(ctx)):
    c.permit("admin")
    values = s.execute(select(Run.status, func.count()).where(Run.org_id == c.org_id).group_by(Run.status)).all()
    lines = [f'product_match_runs{{status="{status}"}} {count}' for status, count in values]
    expired = s.scalar(select(func.count()).select_from(Chunk).where(Chunk.org_id == c.org_id, Chunk.status == "RUNNING", Chunk.lease_until < time.time()))
    lines.append(f"product_match_expired_leases {expired}")
    oldest = s.scalar(select(func.min(Outbox.created_at)).where(Outbox.org_id==c.org_id, Outbox.completed.is_(False)))
    from datetime import datetime
    age = max(0,time.time()-datetime.fromisoformat(oldest).timestamp()) if oldest else 0
    lines.append(f"product_match_outbox_oldest_seconds {age}")
    retries = s.scalar(select(func.coalesce(func.sum(Chunk.attempts-1),0)).where(Chunk.org_id==c.org_id,Chunk.attempts>1))
    lines.append(f"product_match_chunk_retries {retries}")
    for status,count in s.execute(select(ImportJob.status,func.count()).where(ImportJob.org_id==c.org_id).group_by(ImportJob.status)):
        lines.append(f'product_match_imports{{status="{status}"}} {count}')
    cleanups = s.scalar(select(func.count()).select_from(ArtifactDeletion).where(ArtifactDeletion.org_id==c.org_id,ArtifactDeletion.deleted_at.is_(None)))
    lines.append(f"product_match_cleanup_pending {cleanups}")
    lines.extend(telemetry.render())
    return Response("\n".join(lines)+"\n", media_type="text/plain")


@app.post(PREFIX+"/import-jobs", status_code=202)
def import_validate_job(body: ImportConfig, catalog: bool = False, s=Depends(db), c=Depends(ctx)):
    c.permit('operator', 'admin')
    file = scoped(s, File, body.file_id, c)
    parsed_file(s, c, file)
    return data(request_job(s, c, file, 'validate', {**body.model_dump(exclude={'exclude_rows'}), 'catalog': catalog}), ('result_key',))


@app.get(PREFIX+"/import-jobs/{ident}")
def import_status(ident: str, s=Depends(db), c=Depends(ctx)):
    return data(scoped(s, ImportJob, ident, c), ('result_key',))


@app.post(PREFIX+"/import-jobs/{ident}/retry", status_code=202)
def import_retry(ident: str, s=Depends(db), c=Depends(ctx)):
    c.permit('operator', 'admin')
    job=scoped(s, ImportJob, ident, c, True)
    require(job.status=='FAILED', 409, 'INVALID_TRANSITION', '只有失败的导入可以重试')
    job.status, job.error, job.progress, job.lease_until = 'UPLOADED', None, 0, 0
    event=s.scalar(select(Outbox).where(Outbox.org_id==c.org_id, Outbox.event_key=='import:'+ident))
    event.completed, event.published_at=False, None
    return data(job, ('result_key',))


@app.get(PREFIX+"/import-jobs/{ident}/rows")
def import_rows(ident: str, cursor: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100), issues_only: bool = False, download: bool = False, s=Depends(db), c=Depends(ctx)):
    job=scoped(s, ImportJob, ident, c)
    require(job.kind=='validate' and job.status=='READY',409,'IMPORT_NOT_READY','校验尚未完成')
    rows=read_result(job)
    if issues_only: rows=[r for r in rows if r['issues']]
    rows.sort(key=lambda r:(not any(i['level']=='error' for i in r['issues']),r['row_no']))
    if download:
        import csv
        from workers.jobs import safe_text
        buf=io.StringIO(newline='');writer=csv.writer(buf);writer.writerow(['原始行号','来源编号','商品名称','问题原因'])
        writer.writerows([[r['row_no'],safe_text(r['sku']),safe_text(r['normalized']['name']),safe_text('；'.join(i['message'] for i in r['issues']))] for r in rows])
        return Response(buf.getvalue().encode('utf-8-sig'),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="import-issues.csv"'})
    return {'items':rows[cursor:cursor+limit], 'total':len(rows), 'next_cursor':str(cursor+limit) if cursor+limit<len(rows) else None}


@app.get(PREFIX+"/mapping-templates")
def template_list(supplier: str = '', cursor: str | None = None, limit: int = Query(50, ge=1, le=100), s=Depends(db), c=Depends(ctx)):
    query=select(MappingTemplate).where(MappingTemplate.org_id==c.org_id)
    if supplier: query=query.where(MappingTemplate.supplier.icontains(supplier,autoescape=True))
    rows,cursor=page(s,query,MappingTemplate,cursor,limit)
    return {'items':[data(r) for r in rows],'next_cursor':cursor}


@app.post(PREFIX+"/mapping-templates", status_code=201)
def template_save(body: TemplateInput, s=Depends(db), c=Depends(ctx)):
    c.permit('operator','admin')
    require(body.mapping and all(k in FIELDS and v in body.headers for k,v in body.mapping.items()) and len(set(body.mapping.values()))==len(body.mapping),422,'INVALID_MAPPING','映射包含未知列或重复列')
    query=select(MappingTemplate).where(MappingTemplate.org_id==c.org_id,MappingTemplate.supplier==body.supplier,MappingTemplate.name==body.name)
    latest=s.scalar(query.order_by(MappingTemplate.number.desc()))
    if latest and latest.mapping==body.mapping and latest.headers==body.headers: return data(latest)
    row=MappingTemplate(org_id=c.org_id,**body.model_dump(),number=(latest.number if latest else 0)+1,created_by=c.user_id)
    s.add(row);s.flush();audit(s,c,'mapping_template.create',row.id)
    return data(row)


@app.get(PREFIX+"/runs/{ident}/problems")
def problems(ident: str, cursor: str | None = None, limit: int = Query(50, ge=1, le=100), download: bool = False, s=Depends(db), c=Depends(ctx)):
    run=scoped(s,Run,ident,c)
    query=select(Item).where(Item.org_id==c.org_id,Item.run_id==ident,(Item.status=='NEEDS_INFO') | Item.suggestion.in_(['CONFLICT','REVIEW','NO_CANDIDATE']))
    if download:
        import csv
        from workers.jobs import safe_text
        revision=scoped(s,Revision,run.revision_id,c)
        rows=s.execute(select(Item,Source).join(Source,(Source.org_id==Item.org_id)&(Source.id==Item.source_id)).where(Item.org_id==c.org_id,Item.run_id==ident,(Item.status=='NEEDS_INFO') | Item.suggestion.in_(['CONFLICT','REVIEW','NO_CANDIDATE'])).order_by(Source.row_no))
        buf=io.StringIO(newline='');writer=csv.writer(buf);writer.writerow(['source_id','原始行号',*FIELDS,'问题原因','资料来源'])
        for item,src in rows:
            values=source_fields(src,revision)
            writer.writerow([src.id,src.row_no,*[safe_text(values.get(k,'')) for k in FIELDS],safe_text('；'.join(i['message'] for i in src.issues) or item.suggestion),''])
        return Response(buf.getvalue().encode('utf-8-sig'),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="corrections.csv"'})
    total=s.scalar(select(func.count()).select_from(query.subquery()))
    rows,cursor=page(s,query,Item,cursor,limit)
    return {'items':item_page(s,c,rows),'next_cursor':cursor,'total':total}


@app.get(PREFIX+"/runs/{ident}/corrections")
def drafts_list(ident: str, s=Depends(db), c=Depends(ctx)):
    scoped(s,Run,ident,c);c.permit('operator','admin')
    rows=s.scalars(select(CorrectionDraft).where(CorrectionDraft.org_id==c.org_id,CorrectionDraft.run_id==ident,CorrectionDraft.actor_id==c.user_id).order_by(CorrectionDraft.updated_at.desc()))
    return {'items':[data(r) for r in rows]}


@app.put(PREFIX+"/runs/{ident}/corrections/{source_id}")
def draft_save(ident: str, source_id: str, body: CorrectionInput, s=Depends(db), c=Depends(ctx)):
    draft,preview=save_draft(s,c,scoped(s,Run,ident,c),scoped(s,Source,source_id,c),body.fields,body.evidence,body.expected_version)
    return {**data(draft),'preview':preview}


@app.post(PREFIX+"/runs/{ident}/corrections/submit", status_code=202)
def draft_submit(ident: str, body: CorrectionSubmit, request: Request, s=Depends(db), c=Depends(ctx)):
    run=scoped(s,Run,ident,c)
    return idempotent(s,c,'correction-submit:'+ident,request.headers.get('idempotency-key'),body.model_dump(),lambda:submit_drafts(s,c,run,body.draft_ids))


@app.get(PREFIX+"/policy-evaluations")
def policy_evaluations(s=Depends(db), c=Depends(ctx)):
    c.permit('admin')
    return {'items':[data(e) for e in s.scalars(select(PolicyEvaluation).where(PolicyEvaluation.org_id==c.org_id).order_by(PolicyEvaluation.created_at.desc()))]}


@app.post(PREFIX+"/runs/{ident}/shadow", status_code=202)
def shadow_create(ident: str, body: ShadowInput, request: Request, s=Depends(db), c=Depends(ctx)):
    c.permit('admin');run=scoped(s,Run,ident,c)
    require(run.status=='SUCCEEDED',409,'RUN_NOT_READY','请选择成功运行')
    evaluation=scoped(s,PolicyEvaluation,body.evaluation_id,c)
    from packages.matching.policy import validate_bundle
    try:validate_bundle(evaluation.config)
    except (ValueError,KeyError,OSError) as exc:raise Problem(409,'BUNDLE_INVALID','策略制品校验失败') from exc
    def operation():
        row=ShadowRun(id=uid(),org_id=c.org_id,run_id=ident,evaluation_id=evaluation.id)
        s.add(row);s.flush()
        s.add(Outbox(org_id=c.org_id,event_key='shadow:'+row.id,kind='shadow',resource_id=row.id))
        return data(row)
    return idempotent(s,c,'shadow:'+ident,request.headers.get('idempotency-key'),body.model_dump(),operation)


@app.get(PREFIX+"/shadow-runs/{ident}")
def shadow_status(ident: str,s=Depends(db),c=Depends(ctx)):
    c.permit('admin');return data(scoped(s,ShadowRun,ident,c))


@app.post(PREFIX+"/usage-events",status_code=201)
def usage(body: UsageInput,s=Depends(db),c=Depends(ctx)):
    run=scoped(s,Run,body.run_id,c)
    if body.item_id:require(scoped(s,Item,body.item_id,c).run_id==run.id,404,'NOT_FOUND','记录不存在')
    row=UsageEvent(org_id=c.org_id,actor_id=c.user_id,**body.model_dump());s.add(row)
    return {'ok':True}


@app.get(PREFIX+"/usage-events")
def usage_report(run_id: str, s=Depends(db), c=Depends(ctx)):
    c.permit('admin');scoped(s,Run,run_id,c)
    from statistics import median
    events=s.scalars(select(UsageEvent).where(UsageEvent.org_id==c.org_id,UsageEvent.run_id==run_id)).all()
    times=[e.duration_ms for e in events if e.event=='review' and e.duration_ms is not None]
    return {'events':len(events),'review_median_ms':median(times) if times else None,'search_count':sum(e.event=='search' for e in events),'correction_count':sum(e.event=='correction' for e in events),'participants':len({e.actor_id for e in events}),'scope':'操作遥测；误确认及人工对照须独立核验'}


@app.get(PREFIX+"/operations")
def operations(s=Depends(db), c=Depends(ctx)):
    c.permit('admin')
    from datetime import datetime
    pending=s.scalar(select(func.count()).select_from(Outbox).where(Outbox.org_id==c.org_id,Outbox.completed.is_(False)))
    oldest=s.scalar(select(func.min(Outbox.created_at)).where(Outbox.org_id==c.org_id,Outbox.completed.is_(False)))
    age=max(0,time.time()-datetime.fromisoformat(oldest).timestamp()) if oldest else 0
    expired=s.scalar(select(func.count()).select_from(Chunk).where(Chunk.org_id==c.org_id,Chunk.status=='RUNNING',Chunk.lease_until<time.time()))
    no_heartbeat=s.scalar(select(func.count()).select_from(Chunk).where(Chunk.org_id==c.org_id,Chunk.status=='RUNNING',Chunk.lease_until<time.time()-180))
    cleanup=s.scalar(select(func.count()).select_from(ArtifactDeletion).where(ArtifactDeletion.org_id==c.org_id,ArtifactDeletion.deleted_at.is_(None)))
    errors=[{'reason':reason,'count':count} for reason,count in s.execute(select(ImportJob.error,func.count()).where(ImportJob.org_id==c.org_id,ImportJob.status=='FAILED').group_by(ImportJob.error))]
    timings={k:0 for k in ('queue_seconds','index_seconds','matching_seconds','persistence_prepare_seconds','lease_recoveries')}
    for values in s.scalars(select(Run.timings).where(Run.org_id==c.org_id)):
        for k in timings:timings[k]+=values.get(k,0)
    alerts=[]
    if age>300:alerts.append('后台最老未完成事件超过 5 分钟，请检查调度器及依赖状态')
    if no_heartbeat:alerts.append('存在 5 分钟未续租的任务，请检查 Worker')
    if cleanup:alerts.append('存在待清理的到期文件，请查看清理队列和存储状态')
    return {'outbox_pending':pending,'oldest_outbox_seconds':age,'expired_leases':expired,'cleanup_pending':cleanup,'imports':dict(s.execute(select(ImportJob.status,func.count()).where(ImportJob.org_id==c.org_id).group_by(ImportJob.status)).all()),'import_errors':errors,'timings':timings,'alerts':alerts}


from apps.api.enterprise import install as install_enterprise
install_enterprise(app, db, ctx, data, page)
from apps.api.v13 import install as install_v13
install_v13(app, db, ctx, data, page)

import os
static = Path(os.getenv("WEB_DIST", ROOT / "apps/web/dist"))
if static.exists():
    app.mount("/", StaticFiles(directory=static, html=True), name="web")
