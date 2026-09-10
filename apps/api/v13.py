from fastapi import Depends,Query,UploadFile,File as UploadParam
from fastapi.responses import Response
from pydantic import Field
from sqlalchemy import select,func
from .schemas import Input,Reason
from packages.domain.models import *
from packages.domain.auth import scoped
from packages.domain.errors import require
from packages.domain import catalog_plans


class PlanInput(Input):
    from_version_id: str
    to_version_id: str
class PlanConfirm(Reason):
    expected_version: int = Field(ge=0)
    group: str | None = None
    row_id: str | None = None
    proposed_id: str | None = None
class PlanSubmit(Reason):
    expected_version: int = Field(ge=0)
    retired_count: int = Field(ge=0)
class TodoAssign(Reason):
    resource_key: str
    assignee_id: str | None = None


def install(app,db,ctx,data,page):
    P='/api/v1'
    @app.post(P+'/catalog-plans',status_code=202)
    def create_plan(body:PlanInput,s=Depends(db),c=Depends(ctx)):
        return data(catalog_plans.create(s,c,body.from_version_id,body.to_version_id))

    @app.get(P+'/catalog-plans')
    def plans(s=Depends(db),c=Depends(ctx)):
        c.permit('admin');return {'items':[data(p) for p in s.scalars(select(CatalogPlan).where(CatalogPlan.org_id==c.org_id).order_by(CatalogPlan.created_at.desc()).limit(50))]}

    @app.get(P+'/catalog-plans/{ident}')
    def get_plan(ident:str,group:str|None=None,cursor:str|None=None,limit:int=Query(20,ge=1,le=100),s=Depends(db),c=Depends(ctx)):
        c.permit('admin');p=scoped(s,CatalogPlan,ident,c)
        q=select(CatalogPlanRow).where(CatalogPlanRow.org_id==c.org_id,CatalogPlanRow.plan_id==ident)
        summary=[{'group':g,'total':n,'confirmed':confirmed} for g,n,confirmed in s.execute(select(CatalogPlanRow.group,func.count(),func.sum(CatalogPlanRow.confirmed.cast(Integer))).where(CatalogPlanRow.org_id==c.org_id,CatalogPlanRow.plan_id==ident).group_by(CatalogPlanRow.group))]
        if group:q=q.where(CatalogPlanRow.group==group)
        rows,nxt=page(s,q,CatalogPlanRow,cursor,limit)
        products={x.id:x for x in s.scalars(select(Product).where(Product.org_id==c.org_id,Product.id.in_({r.product_id for r in rows}|{r.proposed_id for r in rows if r.proposed_id})))}
        retired=s.scalar(select(func.count()).select_from(CatalogPlanRow).where(CatalogPlanRow.org_id==c.org_id,CatalogPlanRow.plan_id==ident,CatalogPlanRow.proposed_id.is_(None)))
        return {**data(p),'groups':summary,'retired_count':retired,'items':[{**data(r),'old_product':data(products[r.product_id]),'proposed_product':data(products[r.proposed_id]) if r.proposed_id else None} for r in rows],'next_cursor':nxt}

    @app.post(P+'/catalog-plans/{ident}/confirm')
    def confirm_plan(ident:str,body:PlanConfirm,s=Depends(db),c=Depends(ctx)):
        p=scoped(s,CatalogPlan,ident,c,True);catalog_plans.confirm(s,c,p,body.model_dump());return data(p)

    @app.post(P+'/catalog-plans/{ident}/submit',status_code=202)
    def submit_plan(ident:str,body:PlanSubmit,s=Depends(db),c=Depends(ctx)):
        return data(catalog_plans.submit(s,c,scoped(s,CatalogPlan,ident,c,True),body.expected_version,body.retired_count,body.reason))

    @app.post(P+'/catalog-plans/{ident}/retry',status_code=202)
    def retry_plan(ident:str,s=Depends(db),c=Depends(ctx)):
        c.permit('admin');p=scoped(s,CatalogPlan,ident,c,True);require(p.status=='FAILED',409,'PLAN_STATE','仅失败任务可重新准备');p.status='QUEUED'
        for e in s.scalars(select(Outbox).where(Outbox.org_id==c.org_id,Outbox.kind=='catalog_plan',Outbox.resource_id==ident)):e.completed=False;e.published_at=None
        return data(p)

    @app.get(P+'/runs/{ident}/corrections/return-template')
    def return_template(ident:str,s=Depends(db),c=Depends(ctx)):
        from packages.domain.correction_returns import template
        return Response(template(s,c,scoped(s,Run,ident,c)),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="correction-return.csv"'})

    @app.post(P+'/runs/{ident}/corrections/return')
    def return_import(ident:str,file:UploadFile=UploadParam(...),s=Depends(db),c=Depends(ctx)):
        from packages.domain.correction_returns import import_return
        return import_return(s,c,scoped(s,Run,ident,c),file.file.read(20*1024*1024+1),file.filename or '')

    @app.get(P+'/workflow-members')
    def workflow_members(s=Depends(db),c=Depends(ctx)):
        c.permit('admin','supervisor','integration_manager')
        return {'items':[{'user_id':u.id,'name':u.name,'roles':m.roles} for m,u in s.execute(select(Membership,User).join(User,User.id==Membership.user_id).where(Membership.org_id==c.org_id,Membership.active.is_(True),User.active.is_(True)))]}

    @app.get(P+'/todos')
    def todos(owner:str|None=None,cursor:str|None=None,limit:int=Query(50,ge=1,le=100),s=Depends(db),c=Depends(ctx)):
        from packages.domain.todos import list_todos
        return list_todos(s,c,owner,cursor,limit)

    @app.post(P+'/todos/assign')
    def assign_todo(body:TodoAssign,s=Depends(db),c=Depends(ctx)):
        from packages.domain.todos import assign
        return assign(s,c,body.model_dump())
