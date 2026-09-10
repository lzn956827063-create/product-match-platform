"""Audited impact dispositions, anchored to immutable release evidence."""
from sqlalchemy import select
from .models import *
from .auth import scoped
from .db import now
from .errors import require, Problem
from .services import audit
from ..matching.normalize import digest


def root_source(s,c,source_id):
    seen=set()
    while source_id not in seen:
        seen.add(source_id);source=scoped(s,Source,source_id,c)
        lineage=s.scalar(select(RevisionLineage).where(RevisionLineage.org_id==c.org_id,RevisionLineage.revision_id==source.revision_id))
        link=lineage.source_links.get(source_id) if lineage else None
        if not link:return source_id
        source_id=link['parent_source_id']
    raise Problem(409,'LINEAGE_CYCLE','来源修正关系存在循环')


def valid_review(s,c,item,change):
    run=scoped(s,Run,item.run_id,c)
    event=scoped(s,ReviewEvent,item.current_decision_id or '',c) if item.current_decision_id else None
    require(run.status=='SUCCEEDED' and run.catalog_version_id==change.to_version_id and item.status in ('CONFIRMED','UNMATCHED') and event and event.item_id==item.id and event.action==('confirm' if item.status=='CONFIRMED' else 'unmatched'),409,'REVIEW_REQUIRED','必须使用新标准库下已完成且仍有效的人工审核')
    if event.product_id:
        product=scoped(s,Product,event.product_id,c)
        require(product.version_id==change.to_version_id,409,'REVIEW_REQUIRED','审核目标不属于新标准库')
    return event


def check(s,c,task,body):
    change=scoped(s,CatalogChange,task.change_id,c)
    evidence={'original_release_id':task.release_id,'replacement_item_id':body.get('replacement_item_id'),'replacement_release_id':body.get('replacement_release_id'),'revoke_event_id':body.get('revoke_event_id'),'affected_keys':[],'decision_ids':[]}
    if task.kind=='display':
        require(not any(body.get(k) for k in ('replacement_item_id','replacement_release_id','revoke_event_id')),422,'DISPLAY_NOTICE','展示信息变化仅需确认已知悉')
        evidence['disposition']='NOTICE_ACKNOWLEDGED';return evidence
    if task.item_id:
        require(not body.get('replacement_release_id') and not body.get('revoke_event_id'),422,'RECORD_DISPOSITION','此待办需要对应来源的审核记录')
        old=scoped(s,Item,task.item_id,c);item=scoped(s,Item,body.get('replacement_item_id') or '',c)
        require(root_source(s,c,old.source_id)==root_source(s,c,item.source_id),422,'REPLACEMENT_SOURCE','替代审核必须属于同一来源及其补数链')
        event=valid_review(s,c,item,change);evidence.update(disposition='RECORD_REVIEWED',decision_ids=[event.id]);return evidence
    require(task.release_id and not body.get('replacement_item_id'),422,'RELEASE_DISPOSITION','发布影响必须关联替代发布或原发布撤销事件，单条审核不能关闭整份影响')
    old=scoped(s,BatchRelease,task.release_id,c)
    affected=list(s.scalars(select(ReleaseRow).where(ReleaseRow.org_id==c.org_id,ReleaseRow.release_id==old.id,ReleaseRow.product_id==task.product_id)))
    evidence['affected_keys']=sorted(r.stable_key for r in affected)
    require(bool(affected),409,'IMPACT_SCOPE','原发布没有对应影响范围')
    if body.get('revoke_event_id'):
        require(not body.get('replacement_release_id'),422,'DISPOSITION_AMBIGUOUS','请选择替代发布或撤销一种处置方式')
        event=scoped(s,ReleaseEvent,body['revoke_event_id'],c)
        require(event.release_id==old.id and event.kind=='REVOKED' and old.status=='REVOKED',409,'REVOCATION_REQUIRED','必须使用原发布的有效撤销事件')
        evidence.update(disposition='RELEASE_REVOKED',delivery_release_id=old.id,delivery_event_id=event.id);return evidence
    new=scoped(s,BatchRelease,body.get('replacement_release_id') or '',c)
    require(new.batch_id==old.batch_id and new.number>old.number,422,'REPLACEMENT_BATCH','替代发布必须属于原批次且版本更新')
    require(new.status=='PUBLISHED' and not new.invalidated,409,'REPLACEMENT_NOT_CURRENT','替代发布必须是当前有效的已发布版本')
    artifacts=list(s.scalars(select(ReleaseArtifact).where(ReleaseArtifact.org_id==c.org_id,ReleaseArtifact.release_id==new.id)))
    require(len(artifacts)==3 and all(a.status=='SUCCEEDED' and a.file_hash for a in artifacts),409,'ARTIFACTS_NOT_READY','替代发布文件尚未全部就绪')
    rows={r.stable_key:r for r in s.scalars(select(ReleaseRow).where(ReleaseRow.org_id==c.org_id,ReleaseRow.release_id==new.id,ReleaseRow.stable_key.in_(evidence['affected_keys'])))}
    require(len(rows)==len(affected),409,'IMPACT_COVERAGE','替代发布必须覆盖全部受影响来源，不能只修正其中一条')
    for prior in affected:
        row=rows[prior.stable_key];item=scoped(s,Item,row.item_id or '',c)
        event=valid_review(s,c,item,change)
        require(row.decision_id==event.id and row.source_id==item.source_id and row.status==item.status,409,'RELEASE_REVIEW_STALE','发布引用的审核已变化')
        # Full reimports may use a different root only after an explicit identity decision.
        if root_source(s,c,prior.source_id)!=root_source(s,c,item.source_id):
            identity=s.scalar(select(SourceIdentity).where(SourceIdentity.org_id==c.org_id,SourceIdentity.batch_id==old.batch_id,SourceIdentity.source_id==root_source(s,c,item.source_id),SourceIdentity.stable_key==prior.stable_key))
            require(identity,422,'REPLACEMENT_SOURCE','来源发生变化，需先明确确认稳定来源身份')
        evidence['decision_ids'].append(event.id)
    event=s.scalar(select(ReleaseEvent).where(ReleaseEvent.org_id==c.org_id,ReleaseEvent.release_id==new.id,ReleaseEvent.kind=='ARTIFACTS_READY'))
    require(event,409,'ARTIFACTS_NOT_READY','替代发布就绪事件尚未生成')
    evidence.update(disposition='RELEASE_REPLACED',delivery_release_id=new.id,delivery_event_id=event.id,snapshot_hash=new.snapshot_hash)
    return evidence


def view(s,c,task):
    result={k:getattr(task,k) for k in ('id','created_at','change_id','product_id','item_id','release_id','kind','status','resolution','version')}
    resolution=task.resolution
    if task.status=='LOCAL_RESOLVED' and resolution.get('delivery_event_id'):
        deliveries=list(s.scalars(select(Delivery).where(Delivery.org_id==c.org_id,Delivery.event_id==resolution['delivery_event_id'])))
        result['downstream']=[{'id':d.id,'status':d.status,'integration_id':d.integration_id} for d in deliveries]
        result['status']='RESOLVED' if deliveries and all(d.status=='APPLIED' for d in deliveries) else 'WAITING_DOWNSTREAM' if deliveries else 'LOCAL_RESOLVED'
        result['next_action']='下游已回执' if result['status']=='RESOLVED' else '查看交付并对账' if deliveries else '平台内处置完成，尚无电子交付回执'
    return result


def resolve(s,c,task,body):
    c.permit('reviewer' if task.item_id or task.kind=='display' else 'publisher')
    request_hash=digest({k:v for k,v in body.items() if k!='expected_version'})
    if task.resolution.get('request_hash')==request_hash:return view(s,c,task)
    require(task.status in ('OPEN','NOTICE'),409,'IMPACT_RESOLVED','此影响已有处置，不可覆盖原依据')
    require(body.get('expected_version',0)==task.version,409,'VERSION_CONFLICT','影响待办已变化，请刷新')
    evidence=check(s,c,task,body)
    task.resolution={**({'legacy_resolution':task.resolution['legacy_resolution']} if task.resolution.get('legacy_resolution') else {}),**body,**evidence,'actor_id':c.user_id,'at':now(),'request_hash':request_hash}
    task.status='RESOLVED' if task.item_id or task.kind=='display' else 'LOCAL_RESOLVED';task.version+=1
    audit(s,c,'impact.resolve',task.id,task.resolution)
    return view(s,c,task)
