import csv
import io
from collections import Counter
from sqlalchemy import delete, insert, select
from packages.domain.auth import Context
from packages.domain.db import transaction, uid
from packages.domain.errors import Problem, require
from packages.domain.models import *
from packages.domain import storage
from packages.domain.releases import resolve, result_version, batch_state, delta_rows, frozen_rows, release_event
from packages.matching.normalize import digest


def complete(s,kind,ident):
    for e in s.scalars(select(Outbox).where(Outbox.kind==kind,Outbox.resource_id==ident)): e.completed=True


def process_validation(ident):
    with transaction() as s:
        release=s.get(BatchRelease,ident)
        if not release or release.status!='VALIDATING':return
        org,batch,generation=release.org_id,release.batch_id,release.version
        token=result_version(s,org,batch)
        member=s.scalar(select(Membership).join(User,User.id==Membership.user_id).where(Membership.org_id==org,Membership.user_id==release.created_by,Membership.active.is_(True),User.active.is_(True)))
        try:
            require(member and set(member.roles)&{'publisher','operator'},403,'ACTOR_DISABLED','草稿创建者已停权或失去权限')
            rows,blockers,meta=resolve(s,Context(org,release.created_by,member.roles),batch,release.config)
            base=s.get(BatchRelease,release.base_release_id) if release.base_release_id else None
            old=frozen_rows(s,base) if base else []
            delta=delta_rows(old,rows)
            added=[x['stable_key'] for x in delta if x['previous'] is None]
            removed=[x['stable_key'] for x in delta if x['current'] is None]
            if base and (added or removed) and not (release.config.get('confirm_scope_change') and release.config.get('scope_reason','').strip()):
                blockers.append({'problems':[{'code':'SCOPE_CONFIRMATION_REQUIRED','message':'完整来源范围有新增或删除，需明确确认','added':added,'removed':removed}]})
            # A reused identifier across complete files requires an explicit identity decision.
            old_keys={r['stable_key']:r for r in old}
            identity_fields=('brand','model','ram','storage','color','region','pack_count')
            explicit={x.source_id for x in s.scalars(select(SourceIdentity).where(SourceIdentity.org_id==org,SourceIdentity.batch_id==batch))}
            for row in rows:
                prior=old_keys.get(row['stable_key'])
                if prior and prior['root_source_id']!=row['root_source_id'] and row['root_source_id'] not in explicit and any(prior['source_fields'].get(f)!=row['source_fields'].get(f) for f in identity_fields):
                    blockers.append({'root_source_id':row['root_source_id'],'sku':row['source_sku'],'problems':[{'code':'IDENTITY_REUSE_REVIEW','message':'新完整文件中同一编号的身份规格变化，请人工确认编号沿用或重新分配身份'}]})
            rows.sort(key=lambda x:x['stable_key'])
            meta.update({'total':len(rows),'mapped':sum(r['status']=='CONFIRMED' for r in rows),'delta_count':len(delta),'added':len(added),'removed':len(removed),'base_release_id':release.base_release_id})
        except Problem as e:
            rows=[];blockers=[{'problems':[{'code':e.code,'message':e.message}]}];meta={}
    with transaction(write=True) as s:
        s.scalar(select(Organization).where(Organization.id==org).with_for_update())
        r=s.get(BatchRelease,ident)
        if r.status!='VALIDATING' or r.version!=generation:return
        if batch_state(s,org,batch).version!=token:
            blockers.append({'problems':[{'code':'RESULT_CHANGED','message':'校验过程中业务结果已变化，请重新校验'}]})
        s.execute(delete(ReleaseRow).where(ReleaseRow.org_id==org,ReleaseRow.release_id==ident))
        r.validation={'blockers':blockers,'blocker_count':len(blockers),**meta}
        r.manifest={**meta,'result_version':token,'catalog_versions':sorted({x['catalog_version_id'] for x in rows}),'policy_versions':sorted({x['policy_id'] for x in rows}),'config':r.config}
        r.status='DRAFT' if blockers else 'READY';r.result_version=token
        if not blockers:
            r.snapshot_hash=digest(rows)
            entries=[dict(id=uid(),org_id=org,release_id=ident,stable_key=x['stable_key'],source_id=x['source_id'],item_id=x['item_id'],decision_id=x['decision_id'],product_id=x['product_id'],status=x['status'],data=x) for x in rows]
            for start in range(0,len(entries),500):s.execute(insert(ReleaseRow),entries[start:start+500])
        release_event(s,r,'VALIDATED',None,{'status':r.status,'blocker_count':len(blockers),'result_version':token})
        complete(s,'release_validate',ident)


def csv_content(rows,columns):
    buf=io.StringIO(newline='');writer=csv.DictWriter(buf,fieldnames=columns,extrasaction='ignore');writer.writeheader()
    for row in rows:
        safe={k:('' if row.get(k) is None else str(row.get(k))) for k in columns}
        # Neutralize spreadsheet formula injection, including tab/CR prefixes.
        writer.writerow({k: "'"+v if v.lstrip().startswith(('=','+','-','@')) or v.startswith(('\t','\r')) else v for k,v in safe.items()})
    return buf.getvalue().encode('utf-8-sig')


def process_files(ident):
    with transaction() as s:
        release=s.get(BatchRelease,ident)
        if not release or release.status not in ('PUBLISHED','SUPERSEDED','REVOKED'):return
        org=release.org_id
        pending=[(a.id,a.kind) for a in s.scalars(select(ReleaseArtifact).where(ReleaseArtifact.org_id==org,ReleaseArtifact.release_id==ident,ReleaseArtifact.status!='SUCCEEDED'))]
        from packages.domain.materialized import delta as cached_delta,persist_delta
        cached=cached_delta(s,release)
        rows=frozen_rows(s,release) if pending or not cached else []
        if pending or not cached:require(digest(rows)==release.snapshot_hash,409,'SNAPSHOT_HASH','发布快照校验失败')
        if not cached:
            base=s.get(BatchRelease,release.base_release_id) if release.base_release_id else None
            delta=delta_rows(frozen_rows(s,base) if base else [],rows)
        elif any(kind=='delta' for _,kind in pending):
            delta=list(s.scalars(select(ReleaseDeltaRow.data).where(ReleaseDeltaRow.org_id==org,ReleaseDeltaRow.delta_id==cached.id).order_by(ReleaseDeltaRow.number)))
        else:delta=[]
    if not cached:
        with transaction(write=True) as s:
            s.scalar(select(Organization).where(Organization.id==org).with_for_update())
            persist_delta(s,s.get(BatchRelease,ident),delta)
    for artifact_id,kind in pending:
        try:
            if kind=='delta':
                output=[{'op':d['op'],'stable_key':d['stable_key'],'status':(d['current'] or {}).get('status'),'target_sku':(d['current'] or {}).get('target_sku'),'previous_target_sku':(d['previous'] or {}).get('target_sku'),'decision_id':(d['current'] or {}).get('decision_id')} for d in delta]
                columns=['op','stable_key','status','target_sku','previous_target_sku','decision_id']
            else:
                output=[r for r in rows if kind=='full' or r['status']=='CONFIRMED']
                columns=['stable_key','source_sku','source_name','status','target_sku','revision_id','run_id','decision_id','decision_actor_id','decision_at','decision_reason','catalog_version_id','policy_id']
            content=csv_content(output,columns);object_key=storage.quota_put(org,content,'csv');hashed=digest(content)
            require(digest(storage.read(object_key))==hashed,409,'ARTIFACT_HASH','文件存储校验失败')
            with transaction(write=True) as s:
                a=s.get(ReleaseArtifact,artifact_id)
                if a.status!='SUCCEEDED':a.status,a.object_key,a.file_hash,a.error,a.count='SUCCEEDED',object_key,hashed,None,len(output)
        except Exception as e:
            with transaction(write=True) as s:
                a=s.get(ReleaseArtifact,artifact_id)
                if a.status!='SUCCEEDED':a.status,a.error='FAILED',type(e).__name__
    with transaction(write=True) as s:
        s.scalar(select(Organization).where(Organization.id==org).with_for_update())
        r=s.get(BatchRelease,ident)
        all_ready=all(a.status=='SUCCEEDED' for a in s.scalars(select(ReleaseArtifact).where(ReleaseArtifact.org_id==org,ReleaseArtifact.release_id==ident)))
        if all_ready:
            event=s.scalar(select(ReleaseEvent).where(ReleaseEvent.org_id==org,ReleaseEvent.release_id==ident,ReleaseEvent.kind=='ARTIFACTS_READY'))
            if not event:
                event=release_event(s,r,'ARTIFACTS_READY',None,{'snapshot_hash':r.snapshot_hash})
                if not r.invalidated and r.status!='REVOKED':
                    from packages.domain.integrations import enqueue_deliveries
                    enqueue_deliveries(s,r,event)
            complete(s,'release_files',ident)
