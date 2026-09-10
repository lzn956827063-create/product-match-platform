"""Server-side, paged identity proposals. Unseen rows are never silently retired."""
from sqlalchemy import select,func,update
from .models import *
from .db import uid
from .auth import scoped
from .errors import require
from .services import audit
from .catalog_changes import create_change
from ..matching.normalize import digest


def input_hash(before,after):return digest([before.id,before.content_hash,after.id,after.content_hash])


def create(s,c,from_id,to_id):
    c.permit('admin');before=scoped(s,CatalogVersion,from_id,c);after=scoped(s,CatalogVersion,to_id,c)
    require(before.catalog_id==after.catalog_id and after.number>before.number and before.status==after.status=='PUBLISHED',422,'CATALOG_PAIR','请选择同一商品库中先后发布的两个版本')
    p=CatalogPlan(id=uid(),org_id=c.org_id,from_version_id=from_id,to_version_id=to_id,input_hash=input_hash(before,after),created_by=c.user_id)
    s.add(p);s.flush();s.add(Outbox(org_id=c.org_id,event_key='catalog_plan:'+p.id,kind='catalog_plan',resource_id=p.id));return p


def confirm(s,c,plan,body):
    require(bool(body.get('row_id'))!=bool(body.get('group')),422,'PLAN_CONFIRM_SCOPE','请选择一条对应或一个分组进行确认')
    c.permit('admin');require(plan.status=='READY' and plan.version==body['expected_version'],409,'PLAN_CHANGED','建议已变化或尚未就绪，请刷新')
    query=select(CatalogPlanRow).where(CatalogPlanRow.org_id==c.org_id,CatalogPlanRow.plan_id==plan.id)
    if body.get('row_id'):
        row=scoped(s,CatalogPlanRow,body['row_id'],c)
        require(row.plan_id==plan.id,404,'NOT_FOUND','对应行不存在')
        proposed=body.get('proposed_id')
        if proposed:require(scoped(s,Product,proposed,c).version_id==plan.to_version_id,422,'INVALID_IDENTITY_LINKS','目标商品不属于新版本')
        row.proposed_id=proposed;row.confirmed=True;row.reason=body['reason'];row.actor_id=c.user_id
    else:
        group=body.get('group');require(group in ('unchanged','display','identity','unlinked'),422,'PLAN_GROUP','请选择确认分组')
        s.execute(update(CatalogPlanRow).where(CatalogPlanRow.org_id==c.org_id,CatalogPlanRow.plan_id==plan.id,CatalogPlanRow.group==group).values(confirmed=True,reason=body['reason'],actor_id=c.user_id))
    plan.version+=1;audit(s,c,'catalog.plan.confirm',plan.id,body)


def submit(s,c,plan,version,retired_count,reason):
    c.permit('admin');require(plan.status in ('READY','SUBMITTED') and plan.version==version,409,'PLAN_CHANGED','建议已变化或尚未就绪，请刷新')
    before=scoped(s,CatalogVersion,plan.from_version_id,c);after=scoped(s,CatalogVersion,plan.to_version_id,c)
    require(plan.input_hash==input_hash(before,after),409,'PLAN_INPUT_CHANGED','商品库内容已变化，请重新准备')
    rows=list(s.scalars(select(CatalogPlanRow).where(CatalogPlanRow.org_id==c.org_id,CatalogPlanRow.plan_id==plan.id)))
    require(len(rows)==before.row_count and all(r.confirmed for r in rows),409,'UNCONFIRMED_IDENTITIES','请确认所有分组；未加载的记录不会自动停用')
    require(retired_count==sum(r.proposed_id is None for r in rows),409,'RETIREMENT_COUNT','停用数量已变化，请核对后明确填写')
    links={r.product_id:r.proposed_id for r in rows if r.proposed_id}
    change=create_change(s,c,before.id,after.id,links);plan.status='SUBMITTED';audit(s,c,'catalog.plan.submit',plan.id,{'retired_count':retired_count,'reason':reason,'change_id':change.id});return change
