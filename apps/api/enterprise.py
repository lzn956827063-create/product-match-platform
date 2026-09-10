"""Enterprise workflow routes; uses the same dynamic tenant authorization as v1.1."""
import io
import secrets
import time
from typing import Literal
from urllib.parse import quote
from fastapi import Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import Field
from sqlalchemy import func, select, or_
from .schemas import Input, Reason, VersionInput
from packages.domain.auth import Context, scoped
from packages.domain.db import uid, now
from packages.domain.models import *
from packages.domain.errors import require
from packages.domain.services import audit, idempotent
from packages.domain import releases, review_claims, integrations, catalog_changes, annotations, storage
from packages.matching.normalize import digest


class ReleaseInput(Input):
    baseline_run_id: str
    base_release_id: str | None = None
    correction_run_ids: list[str] = Field(default_factory=list,max_length=1000)
    branch_choices: dict[str,dict[str,str]] = Field(default_factory=dict,max_length=10000)
    confirm_scope_change: bool = False
    scope_reason: str = Field(default='',max_length=2000)


class ReleaseUpdate(ReleaseInput,VersionInput):pass
class IdentityInput(Reason):
    stable_key: str = Field(min_length=1,max_length=190)
class ClaimToken(Input):
    claim_token: str = Field(min_length=1,max_length=200)
class Assignment(Reason):
    item_ids: list[str] = Field(min_length=1,max_length=100)
    assignee_id: str | None = None
class WorkflowInput(Input):
    require_claim: bool = False
    claim_seconds: int = Field(default=300,ge=60,le=900)
    queue_limit: int = Field(default=20,ge=1,le=100)
    storage_bytes: int = Field(default=1073741824,ge=1048576,le=107374182400)
    requests_per_minute: int = Field(default=1200,ge=30,le=60000)
class SupplierInput(Input):
    name: str = Field(min_length=1,max_length=200)
    aliases: list[str] = Field(default_factory=list,max_length=100)
class SupplierBind(Reason):
    supplier_id: str
    batch_ids: list[str] = Field(default_factory=list,max_length=100)
    template_ids: list[str] = Field(default_factory=list,max_length=100)
class ChangeInput(Input):
    from_version_id: str
    to_version_id: str
    links: dict[str,str] = Field(default_factory=dict,max_length=50000)
    confirm_identity_links: bool
class ImpactResolution(Reason):
    replacement_item_id: str | None = None
class ServiceInput(Input):
    name: str = Field(min_length=1,max_length=200)
    scopes: list[Literal['releases:read','deliveries:read','receipts:write']] = Field(min_length=1)
    expires_in_days: int = Field(default=30,ge=1,le=365)
class ServiceState(Input):
    active: bool
class IntegrationInput(Input):
    name: str = Field(min_length=1,max_length=200)
    account_id: str
    url: str = Field(min_length=1,max_length=2000)
    active: bool = False
class ReceiptFailure(Input):
    stable_key: str = Field(max_length=200)
    reason: str = Field(min_length=1,max_length=1000)
class Receipt(Input):
    event_id: str
    status: Literal['APPLIED','PARTIAL','REJECTED']
    failed_rows: list[ReceiptFailure] = Field(default_factory=list,max_length=10000)
    message: str = Field(default='',max_length=2000)
class DatasetInput(Input):
    name: str = Field(min_length=1,max_length=200)
    source_type: Literal['authorized','public','simulation']
    evidence: str = Field(min_length=5,max_length=2000)
    entity_grouping_rule: str = Field(min_length=5,max_length=2000)
    split_seed: int = 42
class AnnotationPair(Input):
    item_id: str
    product_id: str
    entity_key: str = Field(min_length=1,max_length=200)
class AnnotationInput(Input):
    label: Literal['MATCH','NO_MATCH','INSUFFICIENT']
    reason: str = Field(min_length=2,max_length=2000)
    seconds: int = Field(ge=1,le=86400)
class ScoreInput(Input):
    evaluation_id: str
class SamplingInput(Input):
    strategy: Literal['random','uncertainty','disagreement']
    budget: int = Field(default=100,ge=1,le=1000)
    seed: int = 42


def install(app,db,ctx,data,page):
    P='/api/v1'
    def release_data(s,r):
        v={**r.validation};v['blockers']=v.get('blockers',[])[:100]
        return {**data(r),'validation':v,'artifacts':[data(a,('object_key',)) for a in sorted(s.scalars(select(ReleaseArtifact).where(ReleaseArtifact.org_id==r.org_id,ReleaseArtifact.release_id==r.id)),key=lambda a:{'full':0,'mapped':1,'delta':2}[a.kind])]}

    @app.get(P+'/releases')
    def list_releases(batch_id: str | None=None,cursor: str|None=None,limit:int=Query(50,ge=1,le=100),s=Depends(db),c=Depends(ctx)):
        q=select(BatchRelease).where(BatchRelease.org_id==c.org_id)
        if batch_id:q=q.where(BatchRelease.batch_id==batch_id)
        rows,next_cursor=page(s,q,BatchRelease,cursor,limit)
        return {'items':[release_data(s,r) for r in rows],'next_cursor':next_cursor}

    @app.post(P+'/batches/{ident}/releases',status_code=201)
    def create_release(ident:str,body:ReleaseInput,request:Request,s=Depends(db),c=Depends(ctx)):
        return idempotent(s,c,'releases:'+ident,request.headers.get('idempotency-key'),body.model_dump(),lambda:release_data(s,releases.create_release(s,c,ident,body.model_dump())))

    @app.get(P+'/releases/{ident}')
    def get_release(ident:str,s=Depends(db),c=Depends(ctx)):
        return release_data(s,scoped(s,BatchRelease,ident,c))

    @app.put(P+'/releases/{ident}')
    def edit_release(ident:str,body:ReleaseUpdate,s=Depends(db),c=Depends(ctx)):
        c.permit('publisher','operator');r=scoped(s,BatchRelease,ident,c,True)
        require(r.status in ('DRAFT','READY') and r.version==body.expected_version,409,'VERSION_CONFLICT','草稿版本已变化或不允许修改')
        require(r.baseline_run_id==body.baseline_run_id and r.base_release_id==body.base_release_id,422,'BASELINE_FROZEN','更换完整基线或增量基线请新建草稿')
        r.config=body.model_dump(exclude={'expected_version'});r.status='DRAFT';r.version+=1;r.snapshot_hash=None;r.validation={}
        releases.release_event(s,r,'EDITED',c.user_id,{'version':r.version});return release_data(s,r)

    @app.post(P+'/releases/{ident}/validate',status_code=202)
    def validate_release(ident:str,s=Depends(db),c=Depends(ctx)):
        r=scoped(s,BatchRelease,ident,c,True);releases.queue_validation(s,c,r);return release_data(s,r)

    @app.post(P+'/releases/{ident}/publish')
    def publish_release(ident:str,body:VersionInput,request:Request,s=Depends(db),c=Depends(ctx)):
        c.permit('publisher')
        return idempotent(s,c,'release.publish:'+ident,request.headers.get('idempotency-key'),body.model_dump(),lambda:release_data(s,releases.publish_release(s,c,scoped(s,BatchRelease,ident,c,True),body.expected_version)))

    @app.post(P+'/releases/{ident}/revoke')
    def revoke_release(ident:str,body:Reason,s=Depends(db),c=Depends(ctx)):
        c.permit('publisher');r=scoped(s,BatchRelease,ident,c,True)
        require(r.status in ('PUBLISHED','SUPERSEDED'),409,'RELEASE_STATE','此版本不能撤销')
        r.status='REVOKED';r.version+=1
        event=releases.release_event(s,r,'REVOKED',c.user_id,body.model_dump());integrations.enqueue_deliveries(s,r,event);audit(s,c,'release.revoke',r.id,body.model_dump());return release_data(s,r)

    @app.post(P+'/releases/{ident}/retry-files',status_code=202)
    def retry_files(ident:str,s=Depends(db),c=Depends(ctx)):
        c.permit('publisher');r=scoped(s,BatchRelease,ident,c)
        require(r.published_at,409,'RELEASE_STATE','尚未发布')
        for e in s.scalars(select(Outbox).where(Outbox.org_id==c.org_id,Outbox.kind=='release_files',Outbox.resource_id==ident)):e.completed,e.published_at=False,None
        return {'queued':True}

    @app.get(P+'/releases/{ident}/rows')
    def release_rows(ident:str,cursor:str|None=None,limit:int=Query(50,ge=1,le=100),s=Depends(db),c=Depends(ctx)):
        scoped(s,BatchRelease,ident,c)
        rows,nxt=page(s,select(ReleaseRow).where(ReleaseRow.org_id==c.org_id,ReleaseRow.release_id==ident),ReleaseRow,cursor,limit)
        return {'items':[r.data for r in rows],'next_cursor':nxt}

    @app.get(P+'/releases/{ident}/blockers')
    def blockers(ident:str,offset:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=100),s=Depends(db),c=Depends(ctx)):
        r=scoped(s,BatchRelease,ident,c);items=r.validation.get('blockers',[])
        return {'items':items[offset:offset+limit],'total':len(items),'next_offset':offset+limit if offset+limit<len(items) else None}

    @app.get(P+'/releases/{ident}/events')
    def release_events(ident:str,cursor:str|None=None,limit:int=Query(50,ge=1,le=100),s=Depends(db),c=Depends(ctx)):
        scoped(s,BatchRelease,ident,c);rows,nxt=page(s,select(ReleaseEvent).where(ReleaseEvent.org_id==c.org_id,ReleaseEvent.release_id==ident),ReleaseEvent,cursor,limit)
        return {'items':[data(r) for r in rows],'next_cursor':nxt}

    def artifact_download(s,c,ident,kind):
        r=scoped(s,BatchRelease,ident,c)
        a=s.scalar(select(ReleaseArtifact).where(ReleaseArtifact.org_id==c.org_id,ReleaseArtifact.release_id==ident,ReleaseArtifact.kind==kind))
        require(a and a.status=='SUCCEEDED',409,'ARTIFACT_NOT_READY','文件尚未就绪，请稍后重试')
        content=storage.read(a.object_key);require(digest(content)==a.file_hash,409,'FILE_HASH_MISMATCH','文件校验失败')
        return StreamingResponse(io.BytesIO(content),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(f'批次发布_v{r.number}_{kind}.csv'),'X-File-SHA256':a.file_hash,'X-Snapshot-SHA256':r.snapshot_hash,'X-Release-Status':r.status,'X-Release-Invalidated':str(r.invalidated).lower()})

    @app.get(P+'/releases/{ident}/artifacts/{kind}/download')
    def download_release(ident:str,kind:Literal['full','mapped','delta'],s=Depends(db),c=Depends(ctx)):return artifact_download(s,c,ident,kind)

    @app.get(P+'/sources/{ident}/trace')
    def source_trace(ident:str,s=Depends(db),c=Depends(ctx)):
        source=scoped(s,Source,ident,c);revision=scoped(s,Revision,source.revision_id,c)
        lineage=s.scalar(select(RevisionLineage).where(RevisionLineage.org_id==c.org_id,RevisionLineage.revision_id==revision.id))
        link=lineage.source_links.get(ident,{}) if lineage else {}
        actor=s.get(User,link.get('actor_id')) if link.get('actor_id') else None
        return {'source':data(source),'revision_number':revision.number,'correction':link,'actor_name':actor.name if actor else None,'runs':[data(r,('manifest',)) for r in s.scalars(select(Run).where(Run.org_id==c.org_id,Run.revision_id==revision.id))]}

    @app.put(P+'/sources/{ident}/identity')
    def source_identity(ident:str,body:IdentityInput,s=Depends(db),c=Depends(ctx)):
        c.permit('operator','publisher');source=scoped(s,Source,ident,c);rev=scoped(s,Revision,source.revision_id,c)
        require(not s.scalar(select(RevisionLineage.id).where(RevisionLineage.org_id==c.org_id,RevisionLineage.revision_id==rev.id)),422,'ROOT_IDENTITY_ONLY','补数记录沿用父记录身份，请在完整输入记录上消歧')
        obj=s.scalar(select(SourceIdentity).where(SourceIdentity.org_id==c.org_id,SourceIdentity.source_id==ident))
        if not obj:obj=SourceIdentity(org_id=c.org_id,batch_id=rev.batch_id,source_id=ident);s.add(obj)
        obj.stable_key,obj.reason,obj.actor_id=body.stable_key,body.reason,c.user_id
        releases.touch_batch(s,c.org_id,rev.batch_id);audit(s,c,'source.identity',ident,body.model_dump());s.flush();return data(obj)

    @app.get(P+'/workflow-settings')
    def workflow_settings(s=Depends(db),c=Depends(ctx)):
        obj=review_claims.settings(s,c.org_id);return data(obj) if obj else WorkflowInput().model_dump()

    @app.put(P+'/workflow-settings')
    def workflow_update(body:WorkflowInput,s=Depends(db),c=Depends(ctx)):
        c.permit('admin');obj=review_claims.settings(s,c.org_id)
        if not obj:obj=WorkflowSettings(org_id=c.org_id);s.add(obj)
        for k,v in body.model_dump().items():setattr(obj,k,v)
        audit(s,c,'workflow.update',c.org_id,body.model_dump());s.flush();return data(obj)

    @app.get(P+'/reviewers')
    def reviewers(s=Depends(db),c=Depends(ctx)):
        c.permit('reviewer','supervisor','admin','publisher')
        return {'items':[{'id':u.id,'name':u.name,'roles':m.roles} for m,u in s.execute(select(Membership,User).join(User,User.id==Membership.user_id).where(Membership.org_id==c.org_id,Membership.active.is_(True),User.active.is_(True))) if 'reviewer' in m.roles]}

    @app.get(P+'/review-queue')
    def queue(mine:bool=True,run_id:str|None=None,batch_id:str|None=None,supplier_id:str|None=None,suggestion:str|None=None,cursor:str|None=None,limit:int=Query(50,ge=1,le=100),s=Depends(db),c=Depends(ctx)):
        c.permit('reviewer','supervisor','admin','publisher')
        q=select(Item).where(Item.org_id==c.org_id,Item.status.in_(['PENDING','NEEDS_INFO','REVOKED'])).join(Run,(Run.org_id==Item.org_id)&(Run.id==Item.run_id)).where(Run.status=='SUCCEEDED').outerjoin(ReviewAssignment,(ReviewAssignment.org_id==Item.org_id)&(ReviewAssignment.item_id==Item.id))
        if mine:q=q.where(or_(ReviewAssignment.assignee_id==c.user_id,ReviewAssignment.assignee_id.is_(None)))
        if run_id:q=q.where(Item.run_id==run_id)
        if batch_id or supplier_id:
            q=q.join(Revision,(Revision.org_id==Run.org_id)&(Revision.id==Run.revision_id))
            if batch_id:q=q.where(Revision.batch_id==batch_id)
            if supplier_id:q=q.where(Revision.batch_id.in_(select(BatchSupplier.batch_id).where(BatchSupplier.org_id==c.org_id,BatchSupplier.supplier_id==supplier_id)))
        if suggestion:q=q.where(Item.suggestion==suggestion)
        items,nxt=page(s,q,Item,cursor,limit)
        sources={x.id:x for x in s.scalars(select(Source).where(Source.org_id==c.org_id,Source.id.in_([i.source_id for i in items])))}
        assignments={a.item_id:a for a in s.scalars(select(ReviewAssignment).where(ReviewAssignment.org_id==c.org_id,ReviewAssignment.item_id.in_([i.id for i in items])))}
        claims={a.item_id:a for a in s.scalars(select(ReviewClaim).where(ReviewClaim.org_id==c.org_id,ReviewClaim.item_id.in_([i.id for i in items])))}
        names={u.id:u.name for u in s.scalars(select(User).where(User.id.in_({a.assignee_id for a in assignments.values()}|{a.holder_id for a in claims.values()}|{a.assigned_by for a in assignments.values()})))}
        from datetime import datetime
        result=[]
        for i in items:
            a,claim=assignments.get(i.id),claims.get(i.id)
            result.append({**data(i),'source':data(sources[i.source_id]),'assignment':{**data(a),'assignee_name':names.get(a.assignee_id),'assigned_by_name':names.get(a.assigned_by)} if a else None,'claim':{**data(claim,('token_hash',)),'holder_name':names.get(claim.holder_id),'expired':claim.lease_until<=time.time()} if claim else None,'waiting_seconds':max(0,time.time()-datetime.fromisoformat(i.created_at).timestamp())})
        return {'items':result,'next_cursor':nxt}

    @app.post(P+'/items/{ident}/claim')
    def claim_item(ident:str,s=Depends(db),c=Depends(ctx)):return review_claims.claim(s,c,scoped(s,Item,ident,c,True))

    @app.post(P+'/items/{ident}/claim/resume')
    def resume_claim(ident:str,s=Depends(db),c=Depends(ctx)):
        c.permit('reviewer');item=scoped(s,Item,ident,c,True)
        held=s.scalar(select(ReviewClaim).where(ReviewClaim.org_id==c.org_id,ReviewClaim.item_id==ident).with_for_update())
        require(held and held.holder_id==c.user_id and held.status=='CLAIMED',409,'CLAIM_OWNER','只能恢复自己仍持有的领取')
        held.status,held.lease_until,held.token_hash='RELEASED',0,None
        review_claims.event(s,c,item,'RESUMED',{'reason':'重新打开工作台，旧浏览器令牌作废'})
        return review_claims.claim(s,c,item)

    @app.post(P+'/items/{ident}/claim/renew')
    def renew_claim(ident:str,body:ClaimToken,s=Depends(db),c=Depends(ctx)):
        c.permit('reviewer');item=scoped(s,Item,ident,c,True);review_claims.eligible(s,c,item);held=review_claims.verify(s,c,item,body.claim_token);settings=review_claims.settings(s,c.org_id);held.lease_until=time.time()+(settings.claim_seconds if settings else 300);return {'lease_until':held.lease_until}

    @app.post(P+'/items/{ident}/claim/release')
    def release_claim(ident:str,body:ClaimToken,s=Depends(db),c=Depends(ctx)):
        c.permit('reviewer');review_claims.release(s,c,scoped(s,Item,ident,c,True),body.claim_token);return {'released':True}

    @app.post(P+'/review-assignments')
    def assignments(body:Assignment,s=Depends(db),c=Depends(ctx)):
        c.permit('supervisor')
        for ident in set(body.item_ids):review_claims.assign(s,c,scoped(s,Item,ident,c,True),body.assignee_id,body.reason)
        return {'assigned':len(set(body.item_ids))}

    @app.get(P+'/items/{ident}/coordination')
    def coordination(ident:str,s=Depends(db),c=Depends(ctx)):
        scoped(s,Item,ident,c)
        held=s.scalar(select(ReviewClaim).where(ReviewClaim.org_id==c.org_id,ReviewClaim.item_id==ident));assignment=s.scalar(select(ReviewAssignment).where(ReviewAssignment.org_id==c.org_id,ReviewAssignment.item_id==ident))
        events=list(s.scalars(select(CoordinationEvent).where(CoordinationEvent.org_id==c.org_id,CoordinationEvent.item_id==ident).order_by(CoordinationEvent.created_at.desc()).limit(100)))
        return {'claim':data(held,('token_hash',)) if held else None,'assignment':data(assignment) if assignment else None,'events':[data(e) for e in events]}

    @app.get(P+'/suppliers')
    def suppliers(q:str='',cursor:str|None=None,limit:int=Query(50,ge=1,le=100),s=Depends(db),c=Depends(ctx)):
        rows,nxt=page(s,select(Supplier).where(Supplier.org_id==c.org_id,Supplier.name.icontains(q,autoescape=True)),Supplier,cursor,limit);return {'items':[data(r) for r in rows],'next_cursor':nxt}

    @app.post(P+'/suppliers',status_code=201)
    def supplier_create(body:SupplierInput,s=Depends(db),c=Depends(ctx)):
        c.permit('admin');obj=Supplier(org_id=c.org_id,**body.model_dump());s.add(obj);s.flush();audit(s,c,'supplier.create',obj.id,body.model_dump());return data(obj)

    @app.post(P+'/supplier-bindings')
    def supplier_bind(body:SupplierBind,s=Depends(db),c=Depends(ctx)):
        c.permit('admin');scoped(s,Supplier,body.supplier_id,c)
        for ident in set(body.batch_ids):
            scoped(s,Batch,ident,c)
            old=s.scalar(select(BatchSupplier).where(BatchSupplier.org_id==c.org_id,BatchSupplier.batch_id==ident))
            if old:old.supplier_id,old.reason,old.actor_id=body.supplier_id,body.reason,c.user_id
            else:s.add(BatchSupplier(org_id=c.org_id,batch_id=ident,supplier_id=body.supplier_id,actor_id=c.user_id,reason=body.reason))
            audit(s,c,'supplier.bind_batch',ident,body.model_dump());releases.touch_batch(s,c.org_id,ident)
        for ident in set(body.template_ids):
            scoped(s,MappingTemplate,ident,c)
            old=s.scalar(select(TemplateSupplier).where(TemplateSupplier.org_id==c.org_id,TemplateSupplier.template_id==ident))
            if old:old.supplier_id=body.supplier_id
            else:s.add(TemplateSupplier(org_id=c.org_id,template_id=ident,supplier_id=body.supplier_id))
            audit(s,c,'supplier.bind_template',ident,body.model_dump())
        return {'bound':len(set(body.batch_ids))+len(set(body.template_ids))}

    @app.get(P+'/suppliers/{ident}/quality')
    def supplier_quality(ident:str,since:str='',until:str='9999',s=Depends(db),c=Depends(ctx)):
        scoped(s,Supplier,ident,c)
        rows=list(s.scalars(select(Revision).where(Revision.org_id==c.org_id,Revision.batch_id.in_(select(BatchSupplier.batch_id).where(BatchSupplier.org_id==c.org_id,BatchSupplier.supplier_id==ident)),Revision.created_at>=since,Revision.created_at<=until).order_by(Revision.created_at).limit(100)))
        from packages.domain.quality import revision_quality
        metrics=[]
        for r in rows:
            report=revision_quality(s,c,r);report['issue_count']=len(report.pop('issues'));metrics.append(report)
        return {'supplier_id':ident,'period':{'since':since or None,'until':until if until!='9999' else None},'items':metrics,'limit':100,'note':'各输入版本分别展示，重复内容已在导入环节去重；小样本不排名。'}

    @app.get(P+'/revisions/{ident}/quality')
    def quality_detail(ident:str,offset:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=100),s=Depends(db),c=Depends(ctx)):
        from packages.domain.quality import revision_quality
        r=revision_quality(s,c,scoped(s,Revision,ident,c));issues=r.pop('issues');return {**r,'issues':issues[offset:offset+limit],'total_issues':len(issues),'next_offset':offset+limit if offset+limit<len(issues) else None}

    @app.get(P+'/catalog-versions/{ident}/identity-index')
    def identity_index(ident:str,s=Depends(db),c=Depends(ctx)):
        c.permit('admin');scoped(s,CatalogVersion,ident,c)
        rows=s.scalars(select(Product).where(Product.org_id==c.org_id,Product.version_id==ident).order_by(Product.sku).limit(50001)).all()
        require(len(rows)<=50000,422,'CATALOG_TRIAL_LIMIT','当前身份对应界面支持最多 5 万件标准商品')
        return {'items':[{'id':p.id,'sku':p.sku,'normalized':{k:p.normalized.get(k) for k in ('name',*catalog_changes.IDENTITY_FIELDS)}} for p in rows]}

    @app.post(P+'/catalog-changes',status_code=202)
    def change_create(body:ChangeInput,s=Depends(db),c=Depends(ctx)):
        require(body.confirm_identity_links,422,'IDENTITY_CONFIRMATION','请明确确认商品身份对应；未对应的旧商品会视为停用')
        return data(catalog_changes.create_change(s,c,body.from_version_id,body.to_version_id,body.links))

    @app.get(P+'/catalog-changes')
    def changes(s=Depends(db),c=Depends(ctx)):
        return {'items':[{**data(x),'report':{k:v for k,v in x.report.items() if k!='changes'},'links':{}} for x in s.scalars(select(CatalogChange).where(CatalogChange.org_id==c.org_id).order_by(CatalogChange.created_at.desc()).limit(50))]}

    @app.get(P+'/catalog-changes/{ident}')
    def change_detail(ident:str,offset:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=100),s=Depends(db),c=Depends(ctx)):
        x=scoped(s,CatalogChange,ident,c);report={**x.report};values=report.pop('changes',[]);return {**data(x,('links',)),'report':report,'changes':values[offset:offset+limit],'total':len(values),'next_offset':offset+limit if offset+limit<len(values) else None}

    @app.post(P+'/catalog-changes/{ident}/activate')
    def change_activate(ident:str,s=Depends(db),c=Depends(ctx)):
        catalog_changes.activate(s,c,scoped(s,CatalogChange,ident,c,True));return {'activated':True}

    @app.get(P+'/impact-tasks')
    def impacts(change_id:str|None=None,cursor:str|None=None,limit:int=Query(50,ge=1,le=100),s=Depends(db),c=Depends(ctx)):
        q=select(ImpactTask).where(ImpactTask.org_id==c.org_id)
        if change_id:q=q.where(ImpactTask.change_id==change_id)
        rows,nxt=page(s,q,ImpactTask,cursor,limit);return {'items':[data(r) for r in rows],'next_cursor':nxt}

    @app.post(P+'/impact-tasks/{ident}/resolve')
    def impact_resolve(ident:str,body:ImpactResolution,s=Depends(db),c=Depends(ctx)):
        c.permit('reviewer');task=scoped(s,ImpactTask,ident,c,True)
        require(task.status in ('OPEN','NOTICE'),409,'IMPACT_RESOLVED','此影响已处理')
        if task.kind!='display':
            replacement=scoped(s,Item,body.replacement_item_id or '',c);change=scoped(s,CatalogChange,task.change_id,c);run=scoped(s,Run,replacement.run_id,c)
            require(run.catalog_version_id==change.to_version_id and replacement.status in ('CONFIRMED','UNMATCHED'),409,'REVIEW_REQUIRED','请选择新标准库版本下已审核的对应记录')
            if task.item_id:
                old=scoped(s,Item,task.item_id,c);old_run=scoped(s,Run,old.run_id,c)
                require(run.revision_id==old_run.revision_id and replacement.source_id==old.source_id,422,'REPLACEMENT_SOURCE','替代审核必须属于同一输入来源；补数影响请在新的批次发布中处理')
        task.status='RESOLVED';task.resolution={**body.model_dump(),'actor_id':c.user_id,'at':now()};audit(s,c,'impact.resolve',task.id,task.resolution);return data(task)

    @app.get(P+'/service-accounts')
    def accounts(s=Depends(db),c=Depends(ctx)):
        c.permit('integration_manager','admin');return {'items':[data(x,('token_hash',)) for x in s.scalars(select(ServiceAccount).where(ServiceAccount.org_id==c.org_id))]}

    @app.post(P+'/service-accounts',status_code=201)
    def account_create(body:ServiceInput,s=Depends(db),c=Depends(ctx)):
        c.permit('integration_manager');a=ServiceAccount(id=uid(),org_id=c.org_id,name=body.name,scopes=body.scopes,expires_at=time.time()+body.expires_in_days*86400);token=integrations.issue_credential(a);s.add(a);s.flush();audit(s,c,'service_account.create',a.id,{'scopes':a.scopes});return {**data(a,('token_hash',)),'credential':token}

    @app.post(P+'/service-accounts/{ident}/rotate')
    def rotate(ident:str,s=Depends(db),c=Depends(ctx)):
        c.permit('integration_manager');a=scoped(s,ServiceAccount,ident,c,True);token=integrations.issue_credential(a);audit(s,c,'service_account.rotate',a.id);return {'credential':token,'expires_at':a.expires_at}

    @app.put(P+'/service-accounts/{ident}')
    def account_state(ident:str,body:ServiceState,s=Depends(db),c=Depends(ctx)):
        c.permit('integration_manager');a=scoped(s,ServiceAccount,ident,c,True);a.active=body.active;audit(s,c,'service_account.state',a.id,body.model_dump());return data(a,('token_hash',))

    @app.get(P+'/integrations')
    def integration_list(s=Depends(db),c=Depends(ctx)):
        c.permit('integration_manager','admin');return {'items':[data(x,('secret_cipher',)) for x in s.scalars(select(Integration).where(Integration.org_id==c.org_id))]}

    @app.post(P+'/integrations',status_code=201)
    def integration_create(body:IntegrationInput,s=Depends(db),c=Depends(ctx)):
        c.permit('integration_manager');a=scoped(s,ServiceAccount,body.account_id,c);require(set(a.scopes)>=integrations.SCOPES,422,'INTEGRATION_SCOPES','交付账号需具备读取发布、读取交付和提交回执权限');integrations.validate_url(body.url)
        secret=secrets.token_urlsafe(32);i=Integration(org_id=c.org_id,**body.model_dump(),secret_cipher=integrations.cipher().encrypt(secret.encode()).decode());s.add(i);s.flush();audit(s,c,'integration.create',i.id,{'active':i.active});return {**data(i,('secret_cipher',)),'signing_secret':secret}

    @app.put(P+'/integrations/{ident}')
    def integration_state(ident:str,body:ServiceState,s=Depends(db),c=Depends(ctx)):
        c.permit('integration_manager');i=scoped(s,Integration,ident,c,True)
        if body.active:integrations.validate_url(i.url)
        i.active=body.active;audit(s,c,'integration.state',i.id,body.model_dump());return data(i,('secret_cipher',))

    @app.get(P+'/deliveries')
    def delivery_list(cursor:str|None=None,limit:int=Query(50,ge=1,le=100),s=Depends(db),c=Depends(ctx)):
        c.permit('integration_manager','publisher','admin');rows,nxt=page(s,select(Delivery).where(Delivery.org_id==c.org_id),Delivery,cursor,limit);return {'items':[data(r) for r in rows],'next_cursor':nxt}

    @app.get(P+'/deliveries/{ident}/attempts')
    def attempts(ident:str,s=Depends(db),c=Depends(ctx)):
        c.permit('integration_manager','publisher','admin');scoped(s,Delivery,ident,c);return {'items':[data(x) for x in s.scalars(select(DeliveryAttempt).where(DeliveryAttempt.org_id==c.org_id,DeliveryAttempt.delivery_id==ident).order_by(DeliveryAttempt.number))]}

    @app.post(P+'/deliveries/{ident}/replay')
    def replay(ident:str,body:Reason,s=Depends(db),c=Depends(ctx)):
        c.permit('integration_manager');d=scoped(s,Delivery,ident,c,True);require(d.status in ('MANUAL','RECEIVED','PARTIAL','REJECTED','RETRY'),409,'DELIVERY_REPLAY','此状态不允许重放')
        d.status,d.next_retry,d.lease_until,d.fence_token='QUEUED',0,0,d.fence_token+1
        for e in s.scalars(select(Outbox).where(Outbox.org_id==c.org_id,Outbox.kind=='delivery',Outbox.resource_id==ident)):e.completed,e.published_at=False,None
        audit(s,c,'delivery.replay',ident,{**body.model_dump(),'event_id':d.event_id});return data(d)

    def machine(request,s,scope):
        account=integrations.service_auth(s,request.headers.get('authorization',''),request.headers.get('x-organization-id',''),scope)
        from packages.domain.quotas import count_request
        count_request(s,Context(account.org_id,account.id,[]))
        return account
    def machine_context(a):return Context(a.org_id,a.id,[])

    @app.get(P+'/service/releases')
    def service_releases(request:Request,cursor:str|None=None,limit:int=Query(50,ge=1,le=100),s=Depends(db)):
        a=machine(request,s,'releases:read');rows,nxt=page(s,select(BatchRelease).where(BatchRelease.org_id==a.org_id,BatchRelease.published_at.is_not(None)),BatchRelease,cursor,limit)
        s.add(ServiceAccess(org_id=a.org_id,account_id=a.id,action='releases.list',resource_id=a.id));return {'items':[release_data(s,r) for r in rows],'next_cursor':nxt}

    @app.get(P+'/service/releases/{ident}')
    def service_release(ident:str,request:Request,s=Depends(db)):
        a=machine(request,s,'releases:read');r=scoped(s,BatchRelease,ident,machine_context(a));require(r.published_at,404,'NOT_FOUND','版本尚未发布');s.add(ServiceAccess(org_id=a.org_id,account_id=a.id,action='release.read',resource_id=ident));return release_data(s,r)

    @app.get(P+'/service/releases/{ident}/artifacts/{kind}/download')
    def service_download(ident:str,kind:Literal['full','mapped','delta'],request:Request,s=Depends(db)):
        a=machine(request,s,'releases:read');s.add(ServiceAccess(org_id=a.org_id,account_id=a.id,action='release.download',resource_id=ident));return artifact_download(s,machine_context(a),ident,kind)

    @app.get(P+'/service/releases/{ident}/delta')
    def service_delta(ident:str,base_release_id:str,request:Request,offset:int=Query(0,ge=0),limit:int=Query(100,ge=1,le=100),s=Depends(db)):
        a=machine(request,s,'releases:read');r=scoped(s,BatchRelease,ident,machine_context(a));require(r.published_at,404,'NOT_FOUND','版本尚未发布')
        require(r.base_release_id==base_release_id,409,'FULL_SYNC_REQUIRED','接收方基线不一致，请获取全量发布')
        base=scoped(s,BatchRelease,base_release_id,machine_context(a));rows=releases.delta_rows(releases.frozen_rows(s,base),releases.frozen_rows(s,r));s.add(ServiceAccess(org_id=a.org_id,account_id=a.id,action='release.delta',resource_id=ident));return {'items':rows[offset:offset+limit],'total':len(rows),'next_offset':offset+limit if offset+limit<len(rows) else None}

    @app.get(P+'/service/deliveries/{ident}')
    def service_delivery(ident:str,request:Request,s=Depends(db)):
        a=machine(request,s,'deliveries:read');d=scoped(s,Delivery,ident,machine_context(a));i=scoped(s,Integration,d.integration_id,machine_context(a));require(i.account_id==a.id,404,'NOT_FOUND','资源不存在');s.add(ServiceAccess(org_id=a.org_id,account_id=a.id,action='delivery.read',resource_id=ident));return data(d)

    @app.post(P+'/service/deliveries/{ident}/receipt')
    def delivery_receipt(ident:str,body:Receipt,request:Request,s=Depends(db)):
        a=machine(request,s,'receipts:write');s.scalar(select(Organization).where(Organization.id==a.org_id).with_for_update());d=scoped(s,Delivery,ident,machine_context(a),True);return data(integrations.receipt(s,a,d,body.model_dump()))

    @app.get(P+'/datasets')
    def datasets(s=Depends(db),c=Depends(ctx)):
        c.permit('admin','annotator','adjudicator');return {'items':[data(x,('manifest',)) for x in s.scalars(select(DatasetVersion).where(DatasetVersion.org_id==c.org_id).order_by(DatasetVersion.created_at.desc()).limit(100))]}

    @app.post(P+'/datasets',status_code=201)
    def create_dataset(body:DatasetInput,s=Depends(db),c=Depends(ctx)):
        c.permit('admin');d=DatasetVersion(org_id=c.org_id,name=body.name,provenance={**body.model_dump(exclude={'name'}),'attested_by':c.user_id,'attested_at':now(),'authorization_verified_by_platform':False});s.add(d);s.flush();audit(s,c,'dataset.create',d.id);return data(d)

    @app.post(P+'/datasets/{ident}/pairs',status_code=201)
    def add_pair(ident:str,body:AnnotationPair,s=Depends(db),c=Depends(ctx)):
        return data(annotations.add_task(s,c,scoped(s,DatasetVersion,ident,c,True),**body.model_dump()))

    @app.get(P+'/datasets/{ident}/tasks')
    def annotation_tasks(ident:str,cursor:str|None=None,limit:int=Query(50,ge=1,le=100),s=Depends(db),c=Depends(ctx)):
        c.permit('admin','annotator','adjudicator');scoped(s,DatasetVersion,ident,c)
        rows,nxt=page(s,select(AnnotationTask).where(AnnotationTask.org_id==c.org_id,AnnotationTask.dataset_id==ident),AnnotationTask,cursor,limit)
        mine={x.task_id:x for x in s.scalars(select(AnnotationDecision).where(AnnotationDecision.org_id==c.org_id,AnnotationDecision.actor_id==c.user_id,AnnotationDecision.task_id.in_([r.id for r in rows])))}
        result=[]
        for r in rows:
            snapshot={k:v for k,v in r.snapshot.items() if k in ('source','source_raw','target','display_mode')}
            result.append({**data(r,('snapshot','label')),'snapshot':snapshot,'mine':data(mine[r.id]) if r.id in mine else None,'label':r.label if r.status=='RESOLVED' else None})
        return {'items':result,'next_cursor':nxt}

    @app.post(P+'/annotation-tasks/{ident}/decisions')
    def annotate(ident:str,body:AnnotationInput,s=Depends(db),c=Depends(ctx)):
        t=scoped(s,AnnotationTask,ident,c,True);annotations.annotate(s,c,t,body.model_dump());return {'status':t.status}

    @app.get(P+'/annotation-tasks/{ident}/dispute')
    def dispute(ident:str,s=Depends(db),c=Depends(ctx)):
        c.permit('adjudicator');t=scoped(s,AnnotationTask,ident,c);require(t.status=='DISPUTED',409,'NO_DISPUTE','此条目没有待裁决分歧');decisions=list(s.scalars(select(AnnotationDecision).where(AnnotationDecision.org_id==c.org_id,AnnotationDecision.task_id==ident)));require(all(x.actor_id!=c.user_id for x in decisions),403,'INDEPENDENT_ADJUDICATOR','裁决者不能是该记录的标注者');return {'items':[data(d) for d in decisions]}

    @app.post(P+'/annotation-tasks/{ident}/adjudicate')
    def adjudicate(ident:str,body:AnnotationInput,s=Depends(db),c=Depends(ctx)):
        t=scoped(s,AnnotationTask,ident,c,True);annotations.annotate(s,c,t,body.model_dump(),True);return {'status':t.status,'label':t.label}

    @app.post(P+'/datasets/{ident}/freeze')
    def freeze(ident:str,s=Depends(db),c=Depends(ctx)):
        return data(annotations.freeze(s,c,scoped(s,DatasetVersion,ident,c,True)))

    @app.get(P+'/datasets/{ident}/manifest')
    def dataset_manifest(ident:str,s=Depends(db),c=Depends(ctx)):
        c.permit('admin');d=scoped(s,DatasetVersion,ident,c);require(d.status=='FROZEN',409,'DATASET_NOT_FROZEN','尚未冻结');require(digest(d.manifest)==d.content_hash,409,'DATASET_HASH','清单校验失败');return {'manifest':d.manifest,'content_hash':d.content_hash}

    @app.post(P+'/datasets/{ident}/score-training',status_code=202)
    def score_training(ident:str,body:ScoreInput,s=Depends(db),c=Depends(ctx)):
        c.permit('admin');d=scoped(s,DatasetVersion,ident,c,True);e=scoped(s,PolicyEvaluation,body.evaluation_id,c)
        require(d.status=='DRAFT' and not s.scalar(select(SamplingRound.id).where(SamplingRound.org_id==c.org_id,SamplingRound.dataset_id==ident)),409,'SAMPLING_FROZEN','开始选样后不能改写冻结评分，请建立新的实验数据集')
        require(d.provenance.get('scoring',{}).get('status')!='QUEUED',409,'SCORING_BUSY','训练池评分正在排队')
        from packages.matching.policy import validate_bundle
        validate_bundle(e.config)
        d.provenance={**d.provenance,'scoring':{'evaluation_id':e.id,'status':'QUEUED','requested_by':c.user_id}}
        s.add(Outbox(org_id=c.org_id,event_key='annotation_score:'+ident+':'+uid(),kind='annotation_score',resource_id=ident));return {'status':'QUEUED'}

    @app.post(P+'/datasets/{ident}/sample')
    def sample(ident:str,body:SamplingInput,request:Request,s=Depends(db),c=Depends(ctx)):
        d=scoped(s,DatasetVersion,ident,c,True);return idempotent(s,c,'sample:'+ident,request.headers.get('idempotency-key'),body.model_dump(),lambda:data(annotations.sample(s,c,d,**body.model_dump())))

    @app.get(P+'/datasets/{ident}/rounds')
    def rounds(ident:str,s=Depends(db),c=Depends(ctx)):
        c.permit('admin');scoped(s,DatasetVersion,ident,c);return {'items':[data(r) for r in s.scalars(select(SamplingRound).where(SamplingRound.org_id==c.org_id,SamplingRound.dataset_id==ident).order_by(SamplingRound.created_at))]}
