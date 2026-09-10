"""Read-only union of business states; assignment never grants business permissions."""
import time
from datetime import datetime
from sqlalchemy import select,union_all,literal,func,or_
from .models import *
from .auth import scoped
from .errors import require
from .services import audit
from .db import uid
from .impacts import view


def queries(c):
    def project(cls,kind,reason,next_action,route):
        return select((literal(kind+':')+cls.id).label('key'),literal(kind).label('kind'),cls.id.label('id'),cls.created_at.label('created_at'),reason.label('reason'),literal(next_action).label('next_action'),literal(route).label('route')).where(cls.org_id==c.org_id)
    rows=[project(ImpactTask,'impact',ImpactTask.kind,'核对全部影响范围并选择处置依据','catalog').where(ImpactTask.status.in_(['OPEN','NOTICE','LOCAL_RESOLVED'])),project(Item,'correction',literal('来源资料不完整'),'打开来源记录并填写补数草稿','workbench').where(Item.status=='NEEDS_INFO')]
    if set(c.roles)&{'operator','publisher','admin'}:rows.append(project(BatchRelease,'release',literal('发布校验有阻断或文件未就绪'),'查看校验问题，完成后重新校验或重试文件','release').where(or_((BatchRelease.status=='DRAFT')&(BatchRelease.validation['blocker_count'].as_integer()>0),(BatchRelease.status=='PUBLISHED')&BatchRelease.id.in_(select(ReleaseArtifact.release_id).where(ReleaseArtifact.org_id==c.org_id,ReleaseArtifact.status!='SUCCEEDED')))))
    if set(c.roles)&{'integration_manager','publisher','admin'}:rows.append(project(Delivery,'delivery',Delivery.status,'核对接收端状态，记录对账或重放','integration').where(Delivery.status.in_(['RECONCILE','PARTIAL','REJECTED','MANUAL'])))
    return union_all(*rows).subquery()


def list_todos(s,c,owner=None,cursor=None,limit=50):
    canonical=queries(c);q=select(canonical,TodoAssignment.assignee_id).outerjoin(TodoAssignment,(TodoAssignment.org_id==c.org_id)&(TodoAssignment.resource_key==canonical.c.key))
    if owner:q=q.where(TodoAssignment.assignee_id==(c.user_id if owner=='me' else owner))
    if cursor:q=q.where(canonical.c.key>cursor)
    rows=s.execute(q.order_by(canonical.c.key).limit(limit+1)).mappings().all();nxt=rows[limit-1]['key'] if len(rows)>limit else None;result=[]
    for row in rows[:limit]:
        r=dict(row)
        if r['kind']=='impact':
            impact=view(s,c,scoped(s,ImpactTask,r['id'],c))
            if impact['status']=='RESOLVED':continue
            r['status']=impact['status'];r['object_label']='发布影响' if impact['release_id'] else '记录复核'
            r['reason']={'identity':'标准商品身份或编码变化','retired':'标准商品停用','display':'标准商品展示信息变化'}.get(impact['kind'],impact['kind'])
        if r['kind']=='correction':r['run_id']=scoped(s,Item,r['id'],c).run_id
        r['waiting_seconds']=max(0,time.time()-datetime.fromisoformat(r['created_at']).timestamp())
        result.append(r)
    names={u.id:u.name for u in s.scalars(select(User).where(User.id.in_({x['assignee_id'] for x in result if x['assignee_id']})))}
    for r in result:r['assignee_name']=names.get(r['assignee_id']);r['owner_note']='尚未分派' if not r['assignee_id'] else '转交不改变操作权限'
    return {'items':result,'next_cursor':nxt}


def assign(s,c,body):
    c.permit('supervisor','admin');q=queries(c)
    require(s.execute(select(q.c.key).where(q.c.key==body['resource_key'])).first(),404,'NOT_FOUND','待办不存在、已完成或无权查看')
    if body['assignee_id']:
        member=s.scalar(select(Membership).join(User,User.id==Membership.user_id).where(Membership.org_id==c.org_id,Membership.user_id==body['assignee_id'],Membership.active.is_(True),User.active.is_(True)))
        needed={'correction':{'operator','admin'},'release':{'publisher','operator'},'delivery':{'integration_manager'},'impact':{'reviewer','publisher'}}[body['resource_key'].split(':')[0]]
        require(member and set(member.roles)&needed,422,'ASSIGNEE_ROLE','接收人须具备对应业务操作权限')
    row=s.scalar(select(TodoAssignment).where(TodoAssignment.org_id==c.org_id,TodoAssignment.resource_key==body['resource_key']))
    if not row:row=TodoAssignment(id=uid(),org_id=c.org_id,resource_key=body['resource_key'],actor_id=c.user_id,reason=body['reason']);s.add(row)
    row.assignee_id=body['assignee_id'];row.actor_id=c.user_id;row.reason=body['reason'];audit(s,c,'todo.assign',row.id,body);return {'assigned':True}
