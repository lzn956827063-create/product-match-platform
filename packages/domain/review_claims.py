import secrets
import time
from sqlalchemy import select
from .auth import scoped, token_hash
from .db import now, uid
from .errors import require
from .models import *


def settings(s,org):
    return s.scalar(select(WorkflowSettings).where(WorkflowSettings.org_id==org))


def eligible(s,c,item,user_id=None):
    user_id=user_id or c.user_id
    m=s.scalar(select(Membership).join(User,User.id==Membership.user_id).where(Membership.org_id==c.org_id,Membership.user_id==user_id,Membership.active.is_(True),User.active.is_(True)))
    require(m and 'reviewer' in m.roles,403,'REVIEWER_REQUIRED','目标成员需要有效审核员权限')
    run=scoped(s,Run,item.run_id,c)
    require(run.status=='SUCCEEDED',409,'RUN_NOT_READY','运行尚未成功')
    require(not run.manifest['dual_review'] or user_id not in {run.created_by,run.manifest['batch_creator'],run.manifest['revision_creator']},403,'SELF_REVIEW_DENIED','提交者不能审核自己的批次')


def event(s,c,item,kind,detail=None):
    s.add(CoordinationEvent(org_id=c.org_id,item_id=item.id,actor_id=c.user_id,kind=kind,detail=detail or {}))


def claim(s,c,item):
    c.permit('reviewer');eligible(s,c,item)
    assignment=s.scalar(select(ReviewAssignment).where(ReviewAssignment.org_id==c.org_id,ReviewAssignment.item_id==item.id))
    require(not assignment or not assignment.assignee_id or assignment.assignee_id==c.user_id,409,'ASSIGNED_TO_OTHER','此条记录已分派给其他审核员')
    current=s.scalar(select(ReviewClaim).where(ReviewClaim.org_id==c.org_id,ReviewClaim.item_id==item.id).with_for_update())
    require(not current or current.status!='CLAIMED' or current.lease_until<=time.time(),409,'ALREADY_CLAIMED','此条记录已被领取；请等待释放或联系主管')
    if current and current.status=='CLAIMED':event(s,c,item,'EXPIRED',{'previous_holder':current.holder_id})
    if not current:
        current=ReviewClaim(id=uid(),org_id=c.org_id,item_id=item.id);s.add(current)
    token=secrets.token_urlsafe(32);setting=settings(s,c.org_id)
    current.holder_id,current.token_hash,current.lease_until,current.status=c.user_id,token_hash(token),time.time()+(setting.claim_seconds if setting else 300),'CLAIMED'
    event(s,c,item,'CLAIMED');s.flush()
    return {'claim_token':token,'lease_until':current.lease_until,'item_id':item.id}


def verify(s,c,item,token,required=True):
    current=s.scalar(select(ReviewClaim).where(ReviewClaim.org_id==c.org_id,ReviewClaim.item_id==item.id).with_for_update())
    setting=settings(s,c.org_id)
    enforce=required or (setting and setting.require_claim) or (current and current.status=='CLAIMED')
    assignment=s.scalar(select(ReviewAssignment).where(ReviewAssignment.org_id==c.org_id,ReviewAssignment.item_id==item.id))
    if assignment and assignment.assignee_id:
        require(assignment.assignee_id==c.user_id,409,'ASSIGNED_TO_OTHER','此条记录已分派给其他审核员')
        enforce=True
    if enforce or token:
        require(current and current.status=='CLAIMED' and current.holder_id==c.user_id and current.lease_until>time.time() and secrets.compare_digest(current.token_hash or '',token_hash(token or '')),409,'CLAIM_LOST','领取已到期、已转交或令牌失效，请重新领取')
        return current
    return None


def release(s,c,item,token,kind='RELEASED'):
    current=verify(s,c,item,token)
    current.status,current.lease_until,current.token_hash=kind,0,None
    event(s,c,item,kind)


def assign(s,c,item,assignee,reason):
    c.permit('supervisor');require(reason.strip(),422,'REASON_REQUIRED','分派、退回或转交需要说明原因')
    if assignee:eligible(s,c,item,assignee)
    current=s.scalar(select(ReviewAssignment).where(ReviewAssignment.org_id==c.org_id,ReviewAssignment.item_id==item.id))
    previous=current.assignee_id if current else None
    if not current:
        current=ReviewAssignment(org_id=c.org_id,item_id=item.id,assigned_by=c.user_id,reason=reason);s.add(current)
    current.assignee_id,current.assigned_by,current.reason,current.updated_at=assignee,c.user_id,reason,now()
    held=s.scalar(select(ReviewClaim).where(ReviewClaim.org_id==c.org_id,ReviewClaim.item_id==item.id))
    if held:held.status,held.lease_until,held.token_hash='RELEASED',0,None
    event(s,c,item,'TRANSFERRED' if previous else 'ASSIGNED' if assignee else 'RETURNED',{'from':previous,'to':assignee,'reason':reason})
