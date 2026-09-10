from collections import Counter
from sqlalchemy import select,insert,delete
from time import perf_counter
from packages.matching.normalize import digest
from packages.domain.errors import require
from packages.domain.db import transaction,uid
from packages.domain.models import *
from packages.domain.catalog_changes import IDENTITY_FIELDS
from workers.catalog_identity_batches import bind_change
from packages.domain.auth import Context


def _process_change(ident):
    started=perf_counter()
    with transaction() as s:
        job=s.get(CatalogChange,ident)
        if not job or job.status=='SUCCEEDED':return
        before={p.id:p for p in s.scalars(select(Product).where(Product.org_id==job.org_id,Product.version_id==job.from_version_id))}
        after={p.id:p for p in s.scalars(select(Product).where(Product.org_id==job.org_id,Product.version_id==job.to_version_id))}
        changes=[]
        for old in before.values():
            new=after.get(job.links.get(old.id))
            fields=[f for f in IDENTITY_FIELDS if new and old.normalized.get(f)!=new.normalized.get(f)]
            kind='retired' if not new else 'identity' if fields or old.sku!=new.sku else 'display' if old.raw!=new.raw else None
            if kind:changes.append({'kind':kind,'old_product_id':old.id,'new_product_id':new.id if new else None,'old_sku':old.sku,'new_sku':new.sku if new else None,'fields':fields})
        linked_targets=set(job.links.values())
        for new in after.values():
            if new.id not in linked_targets:changes.append({'kind':'added','old_product_id':None,'new_product_id':new.id,'new_sku':new.sku})
        expected=digest([s.get(CatalogVersion,job.from_version_id).content_hash,s.get(CatalogVersion,job.to_version_id).content_hash,job.links])
        require(not job.input_hash or job.input_hash==expected,409,'CHANGE_INPUT_CHANGED','版本内容已变化')
        computed=perf_counter()-started
        member=s.scalar(select(Membership).join(User,User.id==Membership.user_id).where(Membership.org_id==job.org_id,Membership.user_id==job.created_by,Membership.active.is_(True),User.active.is_(True)))
        require(member and 'admin' in member.roles,403,'ACTOR_DISABLED','请求人已无管理权限')
    bind_change(ident,expected)
    with transaction(write=True) as s:
        job=s.get(CatalogChange,ident)
        if not job or job.status=='SUCCEEDED':return
        s.scalar(select(Organization).where(Organization.id==job.org_id).with_for_update())
        s.refresh(job)
        if job.status=='SUCCEEDED':return
        member=s.scalar(select(Membership).join(User,User.id==Membership.user_id).where(Membership.org_id==job.org_id,Membership.user_id==job.created_by,Membership.active.is_(True),User.active.is_(True)))
        if not member or 'admin' not in member.roles:job.status,job.error='FAILED','ACTOR_DISABLED';return
        require(digest([s.get(CatalogVersion,job.from_version_id).content_hash,s.get(CatalogVersion,job.to_version_id).content_hash,job.links])==expected,409,'CHANGE_INPUT_CHANGED','版本内容已变化')
        changed={x['old_product_id']:x['kind'] for x in changes if x['old_product_id']}
        existing={i.resource_key for i in s.scalars(select(ImpactTask).where(ImpactTask.org_id==job.org_id,ImpactTask.change_id==ident))}
        impacts=[]
        for m in s.execute(select(Mapping.item_id,Mapping.product_id).where(Mapping.org_id==job.org_id,Mapping.product_id.in_(changed),Mapping.valid_to.is_(None))):impacts.append((f'item:{m.item_id}',m.product_id,m.item_id,None))
        for row in s.execute(select(ReleaseRow.release_id,ReleaseRow.product_id).distinct().join(BatchRelease,(BatchRelease.org_id==ReleaseRow.org_id)&(BatchRelease.id==ReleaseRow.release_id)).where(ReleaseRow.org_id==job.org_id,ReleaseRow.product_id.in_(changed),BatchRelease.status.in_(['PUBLISHED','SUPERSEDED']))):impacts.append((f'release:{row.release_id}:{row.product_id}',row.product_id,None,row.release_id))
        for key,product,item,release in impacts:
            if key not in existing:
                s.add(ImpactTask(org_id=job.org_id,change_id=ident,resource_key=key,product_id=product,item_id=item,release_id=release,kind=changed[product],status='NOTICE' if changed[product]=='display' else 'OPEN'));existing.add(key)
        s.execute(delete(CatalogChangeRow).where(CatalogChangeRow.org_id==job.org_id,CatalogChangeRow.change_id==ident))
        for start in range(0,len(changes),500):
            s.execute(insert(CatalogChangeRow),[dict(id=uid(),org_id=job.org_id,change_id=ident,number=n,data=x) for n,x in enumerate(changes[start:start+500],start)])
        job.report={'counts':dict(Counter(x['kind'] for x in changes)),'total_changes':len(changes),'timings':{'compute_seconds':computed,'commit_prepare_seconds':perf_counter()-started-computed},'affected_items':len({i[2] for i in impacts if i[2]}),'affected_releases':len({i[3] for i in impacts if i[3]}),'identity_links_confirmed':len(job.links)}
        job.status='SUCCEEDED';job.error=None
        for e in s.scalars(select(Outbox).where(Outbox.kind=='catalog_change',Outbox.resource_id==ident)):e.completed=True


def process_change(ident):
    try:return _process_change(ident)
    except Exception as e:
        with transaction(write=True) as s:
            job=s.get(CatalogChange,ident)
            if job and job.status!='SUCCEEDED':job.status='FAILED';job.error=getattr(e,'code',type(e).__name__)
        raise
