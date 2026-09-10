from collections import Counter
from sqlalchemy import select,insert,delete
from packages.domain.db import transaction,uid
from packages.domain.models import *
from packages.domain.catalog_changes import IDENTITY_FIELDS
from packages.domain.catalog_plans import input_hash
from packages.domain.errors import require


def process_plan(ident):
    with transaction(write=True) as s:
        p=s.scalar(select(CatalogPlan).where(CatalogPlan.id==ident).with_for_update())
        if not p or p.status in ('READY','SUBMITTED'):return
        member=s.scalar(select(Membership).join(User,User.id==Membership.user_id).where(Membership.org_id==p.org_id,Membership.user_id==p.created_by,Membership.active.is_(True),User.active.is_(True)))
        if not member or 'admin' not in member.roles:p.status='FAILED';p.error='ACTOR_DISABLED';return
        # Generation acts as a fence across retries and duplicate outbox delivery.
        p.version+=1;generation=p.version;p.status='COMPUTING';p.error=None;p.progress=0
        s.execute(delete(CatalogPlanRow).where(CatalogPlanRow.org_id==p.org_id,CatalogPlanRow.plan_id==ident))
        org,old_id,new_id,hashed=p.org_id,p.from_version_id,p.to_version_id,p.input_hash
    try:
        with transaction() as s:
            before=s.get(CatalogVersion,old_id);after=s.get(CatalogVersion,new_id)
            require(input_hash(before,after)==hashed,409,'PLAN_INPUT_CHANGED','输入已变化')
            old=list(s.scalars(select(Product).where(Product.org_id==org,Product.version_id==old_id).order_by(Product.id)))
            new={x.sku:x for x in s.scalars(select(Product).where(Product.org_id==org,Product.version_id==new_id))}
            entries=[]
            for p in old:
                n=new.get(p.sku)
                group='unlinked' if not n else 'identity' if any(p.normalized.get(f)!=n.normalized.get(f) for f in IDENTITY_FIELDS) else 'display' if p.raw!=n.raw else 'unchanged'
                entries.append(dict(id=uid(),org_id=org,plan_id=ident,product_id=p.id,proposed_id=n.id if n else None,group=group))
        for start in range(0,len(entries),500):
            with transaction(write=True) as s:
                p=s.scalar(select(CatalogPlan).where(CatalogPlan.id==ident).with_for_update())
                if p.version!=generation or p.status!='COMPUTING':return
                s.execute(insert(CatalogPlanRow),entries[start:start+500]);p.progress=min(len(entries),start+500)
        with transaction(write=True) as s:
            p=s.scalar(select(CatalogPlan).where(CatalogPlan.id==ident).with_for_update())
            if p.version!=generation:return
            p.status='READY';p.report={'counts':dict(Counter(x['group'] for x in entries)),'total':len(entries),'input_hash':hashed}
            for e in s.scalars(select(Outbox).where(Outbox.kind=='catalog_plan',Outbox.resource_id==ident)):e.completed=True
    except Exception as e:
        with transaction(write=True) as s:
            p=s.scalar(select(CatalogPlan).where(CatalogPlan.id==ident).with_for_update())
            if p.version==generation:p.status='FAILED';p.error=type(e).__name__
        raise
