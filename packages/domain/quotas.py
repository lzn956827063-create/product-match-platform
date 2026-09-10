import time
from sqlalchemy import select,func,delete
from .models import *
from .errors import require
from .review_claims import settings


def queue_available(s,org):
    cfg=settings(s,org);limit=cfg.queue_limit if cfg else 20
    count=s.scalar(select(func.count()).select_from(Run).where(Run.org_id==org,Run.status.in_(['QUEUED','RUNNING','CANCEL_REQUESTED'])))
    require(count<limit,429,'QUEUE_QUOTA','组织待处理匹配任务已达上限，请等待任务完成或取消排队任务',{'used':count,'limit':limit})


def reserve(s,org,key,amount):
    s.scalar(select(Organization).where(Organization.id==org).with_for_update())
    old=s.scalar(select(QuotaReservation).where(QuotaReservation.org_id==org,QuotaReservation.resource_key==key).with_for_update())
    if old and not old.released:
        require(old.amount==amount,409,'RESERVATION_MISMATCH','对象预占与文件大小不一致');return old
    cfg=settings(s,org);limit=cfg.storage_bytes if cfg else 1073741824
    total=s.scalar(select(func.coalesce(func.sum(QuotaReservation.amount),0)).where(QuotaReservation.org_id==org,QuotaReservation.kind=='storage',QuotaReservation.released.is_(False)))
    tracked=select(QuotaReservation.resource_key).where(QuotaReservation.org_id==org,QuotaReservation.kind=='storage')
    legacy=s.scalar(select(func.coalesce(func.sum(File.size),0)).where(File.org_id==org,~File.object_key.in_(tracked)))
    require(total+legacy+amount<=limit,413,'STORAGE_QUOTA','组织存储配额不足，请清理到期文件或联系管理员',{'used':total+legacy,'requested':amount,'limit':limit})
    if old:old.released=False;old.amount=amount
    else:old=QuotaReservation(org_id=org,resource_key=key,kind='storage',amount=amount);s.add(old)
    s.flush();return old


def release_object(s,org,key):
    old=s.scalar(select(QuotaReservation).where(QuotaReservation.org_id==org,QuotaReservation.resource_key==key))
    if old:old.released=True


def count_request(s,c):
    # Persisted per-member windows survive multiple API instances; business errors
    # still consume a request because this transaction commits independently.
    s.scalar(select(Organization).where(Organization.id==c.org_id).with_for_update())
    cfg=settings(s,c.org_id);limit=cfg.requests_per_minute if cfg else 1200
    window=int(time.time()//60)
    b=s.scalar(select(RateBucket).where(RateBucket.org_id==c.org_id,RateBucket.principal==c.user_id,RateBucket.window==window).with_for_update())
    require(not b or b.count<limit,429,'RATE_LIMITED','请求过于频繁，请在下一分钟重试',{'limit':limit,'retry_after':60-int(time.time()%60)})
    if not b:b=RateBucket(org_id=c.org_id,principal=c.user_id,window=window,count=0);s.add(b)
    b.count+=1
    s.execute(delete(RateBucket).where(RateBucket.org_id==c.org_id,RateBucket.window<window-2))
