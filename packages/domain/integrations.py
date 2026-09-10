"""Organization-scoped machine credentials and durable delivery protocol."""
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import time
from urllib.parse import urlsplit
from sqlalchemy import select
from .auth import token_hash
from .config import JWT_SECRET
from .errors import require
from .models import *
from .db import uid

SCOPES={'releases:read','deliveries:read','receipts:write'}


def cipher():
    from cryptography.fernet import Fernet
    key=os.getenv('INTEGRATION_MASTER_KEY') or base64.urlsafe_b64encode(hashlib.sha256(('integration-v1:'+JWT_SECRET).encode()).digest()).decode()
    return Fernet(key.encode())


def issue_credential(account):
    raw=f'pm_sa_{account.id}.{secrets.token_urlsafe(36)}'
    account.token_hash=token_hash(raw)
    return raw


def service_auth(s,bearer,org,scope):
    raw=bearer.removeprefix('Bearer ')
    require(raw.startswith('pm_sa_') and '.' in raw,401,'SERVICE_AUTH','服务账号凭据无效')
    ident=raw[6:].split('.',1)[0]
    account=s.get(ServiceAccount,ident)
    require(account and account.active and account.expires_at>time.time() and hmac.compare_digest(account.token_hash,token_hash(raw)),401,'SERVICE_AUTH','服务账号已停用、过期或凭据无效')
    require(account.org_id==org,404,'NOT_FOUND','资源不存在或无权访问')
    require(scope in account.scopes,403,'SCOPE_DENIED','服务账号未获此权限')
    return account


def validate_url(url,resolve=True):
    u=urlsplit(url)
    allow={x.strip() for x in os.getenv('WEBHOOK_ALLOWED_HOSTS','').split(',') if x.strip()}
    local=os.getenv('WEBHOOK_ALLOW_LOOPBACK','false')=='true' and u.hostname in ('127.0.0.1','localhost','::1')
    require(u.hostname in allow and not u.username and not u.password and not u.fragment and (u.scheme=='https' or (local and u.scheme=='http')),422,'WEBHOOK_TARGET_DENIED','通知地址必须使用 HTTPS 且在管理员配置的出站白名单内')
    port=u.port or (443 if u.scheme=='https' else 80)
    addresses=sorted({x[4][0] for x in socket.getaddrinfo(u.hostname,port,type=socket.SOCK_STREAM)}) if resolve else []
    require(all(ipaddress.ip_address(ip).is_global or (local and ipaddress.ip_address(ip).is_loopback) for ip in addresses),422,'WEBHOOK_TARGET_DENIED','禁止访问内网、链路本地和保留地址')
    return u,addresses


def signature(secret,event_id,timestamp,body):
    return hmac.new(secret.encode(),f'{timestamp}.{event_id}.'.encode()+body,hashlib.sha256).hexdigest()


def verify_signature(secret,event_id,timestamp,body,supplied,now_value=None):
    try:valid_time=abs((now_value if now_value is not None else time.time())-int(timestamp))<=300
    except (TypeError,ValueError):return False
    return valid_time and hmac.compare_digest(signature(secret,event_id,timestamp,body),supplied)


def enqueue_deliveries(s,release,event):
    if event.kind=='ARTIFACTS_READY':
        artifacts=list(s.scalars(select(ReleaseArtifact).where(ReleaseArtifact.org_id==release.org_id,ReleaseArtifact.release_id==release.id)))
        if len(artifacts)!=3 or any(a.status!='SUCCEEDED' for a in artifacts):return
    for integration in s.scalars(select(Integration).where(Integration.org_id==release.org_id,Integration.active.is_(True))):
        existing=s.scalar(select(Delivery.id).where(Delivery.org_id==release.org_id,Delivery.integration_id==integration.id,Delivery.event_id==event.id))
        if existing:continue
        d=Delivery(id=uid(),org_id=release.org_id,release_id=release.id,integration_id=integration.id,event_id=event.id,kind=event.kind)
        s.add(d);s.flush();s.add(Outbox(org_id=release.org_id,event_key='delivery:'+d.id,kind='delivery',resource_id=d.id))


def validate_configuration(s,c,body):
    if body.get('receipt_query_url'):validate_url(body['receipt_query_url'])
    owner=body.get('reconciliation_owner_id')
    if owner:
        member=s.scalar(select(Membership).join(User,User.id==Membership.user_id).where(Membership.org_id==c.org_id,Membership.user_id==owner,Membership.active.is_(True),User.active.is_(True)))
        require(member and 'integration_manager' in member.roles,422,'RECONCILIATION_OWNER','负责人须为本组织有效的集成管理员')


def receipt(s,account,delivery,body):
    integration=s.get(Integration,delivery.integration_id)
    require(integration.org_id==account.org_id and integration.account_id==account.id,403,'RECEIPT_ACCOUNT','此服务账号不能提交该交付的回执')
    require(account.active and account.expires_at>time.time() and 'receipts:write' in account.scopes,403,'RECEIPT_AUTH','服务账号已失去提交回执权限')
    require(body['event_id']==delivery.event_id,409,'EVENT_MISMATCH','回执事件编号不一致')
    require(body['status'] in ('APPLIED','PARTIAL','REJECTED'),422,'RECEIPT_STATUS','不支持此回执状态')
    require(delivery.status not in ('CANCELLED',),409,'DELIVERY_CANCELLED','此通知已取消，请处理新的失效通知')
    release=s.get(BatchRelease,delivery.release_id)
    if delivery.status=='RECONCILE' or delivery.escalation_state=='REPLAY_REQUESTED' or (delivery.receipt_due_at and delivery.receipt_due_at<time.time()):
        require(all(k in body for k in ('applied_release_id','base_release_id','snapshot_hash')),409,'RECEIPT_VERSION_REQUIRED','迟到或重放后的回执须包含实际发布、基线和摘要')
    for key,expected in (('applied_release_id',release.id),('base_release_id',release.base_release_id),('snapshot_hash',release.snapshot_hash)):
        if key in body:require(body[key]==expected,409,'RECEIPT_VERSION','回执所应用的发布、基线或摘要不一致')
    failed=body.get('failed_rows',[])
    require((body['status']=='PARTIAL')==bool(failed),422,'RECEIPT_ROWS','部分失败必须给出来源身份明细；成功不能携带失败记录')
    if failed:
        keys=set(s.scalars(select(ReleaseRow.stable_key).where(ReleaseRow.org_id==account.org_id,ReleaseRow.release_id==delivery.release_id)))
        release=s.get(BatchRelease,delivery.release_id)
        if release.base_release_id:keys.update(s.scalars(select(ReleaseRow.stable_key).where(ReleaseRow.org_id==account.org_id,ReleaseRow.release_id==release.base_release_id)))
        require({x['stable_key'] for x in failed}<=keys,422,'UNKNOWN_RECEIPT_ROW','回执包含不属于此发布版本的来源身份')
    if delivery.status=='APPLIED':
        require(delivery.receipt==body,409,'RECEIPT_FINAL','已成功应用的回执不可改写')
        return delivery
    delivery.receipt=body;delivery.status=body['status'];delivery.lease_until=0;delivery.fence_token+=1
    delivery.receipt_due_at=None;delivery.escalation_state='COMPLETED' if body['status']=='APPLIED' else 'ACTION_REQUIRED'
    s.add(ServiceAccess(org_id=account.org_id,account_id=account.id,action='receipt.'+body['status'],resource_id=delivery.id))
    return delivery
