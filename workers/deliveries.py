import http.client
import json
import random
import socket
import ssl
import time
from sqlalchemy import select
from packages.domain.db import transaction
from packages.domain.models import *
from packages.domain.integrations import cipher,validate_url,signature
from packages.domain.errors import require


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self,host,port,address):
        super().__init__(host,port,timeout=10,context=ssl.create_default_context());self.address=address
    def connect(self):
        raw=socket.create_connection((self.address,self.port),timeout=self.timeout)
        self.sock=self._context.wrap_socket(raw,server_hostname=self.host)


def send_notification(url,body,headers):
    u,addresses=validate_url(url)
    require(addresses,422,'WEBHOOK_DNS','通知地址无法解析')
    port=u.port or (443 if u.scheme=='https' else 80)
    conn=PinnedHTTPS(u.hostname,port,addresses[0]) if u.scheme=='https' else http.client.HTTPConnection(addresses[0],port,timeout=10)
    try:
        conn.request('POST',(u.path or '/') + ('?'+u.query if u.query else ''),body=body,headers={**headers,'Host':u.netloc})
        response=conn.getresponse();status=response.status;response.read(65536)
        return status
    finally:conn.close()


def process_delivery(ident):
    from packages.domain.config import DATA_DIR
    if (DATA_DIR/'delivery-restore-hold.json').exists():return
    with transaction(write=True) as s:
        d=s.get(Delivery,ident)
        if not d:return
        s.scalar(select(Organization).where(Organization.id==d.org_id).with_for_update())
        s.refresh(d)
        if d.status in ('APPLIED','PARTIAL','REJECTED','MANUAL','CANCELLED','RECEIVED','RECONCILE'):
            for e in s.scalars(select(Outbox).where(Outbox.kind=='delivery',Outbox.resource_id==ident)):e.completed=True
            return
        if d.lease_until>time.time() or d.next_retry>time.time():return
        integration=s.get(Integration,d.integration_id);account=s.get(ServiceAccount,integration.account_id);release=s.get(BatchRelease,d.release_id)
        if not integration.active or not account.active or account.expires_at<=time.time():d.status,d.last_error='MANUAL','INTEGRATION_DISABLED';return
        if d.kind=='ARTIFACTS_READY' and (release.invalidated or release.status=='REVOKED'):
            d.status='CANCELLED';return
        d.status,d.lease_until,d.fence_token,d.attempts='SENDING',time.time()+30,d.fence_token+1,d.attempts+1
        token,attempt=d.fence_token,d.attempts
        payload={'event_id':d.event_id,'delivery_id':d.id,'release_id':release.id,'kind':d.kind,'base_release_id':release.base_release_id,'snapshot_hash':release.snapshot_hash,'number':release.number,'org_id':d.org_id,'fetch_path':f'/api/v1/service/releases/{release.id}'}
        body=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode();stamp=str(int(time.time()));secret=cipher().decrypt(integration.secret_cipher.encode()).decode()
        url=integration.url;headers={'Content-Type':'application/json','X-Event-ID':d.event_id,'X-Timestamp':stamp,'X-Signature':signature(secret,d.event_id,stamp,body),'X-Attempt-ID':f'{d.id}:{attempt}'}
    code=None;error=None
    try:
        code=send_notification(url,body,headers)
        if not 200<=code<300:error=f'HTTP_{code}'
    except Exception as e:error=type(e).__name__
    with transaction(write=True) as s:
        org_id=s.scalar(select(Delivery.org_id).where(Delivery.id==ident))
        s.scalar(select(Organization).where(Organization.id==org_id).with_for_update())
        d=s.scalar(select(Delivery).where(Delivery.id==ident).with_for_update())
        if not s.scalar(select(DeliveryAttempt.id).where(DeliveryAttempt.org_id==d.org_id,DeliveryAttempt.delivery_id==ident,DeliveryAttempt.number==attempt)):
            s.add(DeliveryAttempt(org_id=d.org_id,delivery_id=ident,number=attempt,status_code=code,error=error))
        if d.fence_token!=token:return  # A receipt, replay or newer lease fenced this attempt.
        d.lease_until=0;d.last_error=error
        if error:
            d.status='MANUAL' if attempt>=8 else 'RETRY';d.next_retry=time.time()+min(3600,2**attempt*5)+random.uniform(0,5)
        else:
            d.status='RECEIVED'  # Only an authenticated receipt can mark APPLIED.
            d.received_at=time.time();d.receipt_due_at=d.received_at+s.get(Integration,d.integration_id).receipt_timeout_seconds;d.escalation_state='AWAITING_RECEIPT'
        if d.status in ('RECEIVED','MANUAL'):
            for e in s.scalars(select(Outbox).where(Outbox.kind=='delivery',Outbox.resource_id==ident)):e.completed=True


def query_receipt(url,payload,secret):
    """Signed, allowlisted server-to-server status query; no redirects or credentials in URL."""
    u,addresses=validate_url(url);port=u.port or (443 if u.scheme=='https' else 80)
    conn=PinnedHTTPS(u.hostname,port,addresses[0]) if u.scheme=='https' else http.client.HTTPConnection(addresses[0],port,timeout=10)
    body=json.dumps(payload,sort_keys=True,separators=(',',':')).encode();stamp=str(int(time.time()))
    try:
        conn.request('POST',(u.path or '/')+('?'+u.query if u.query else ''),body,{'Content-Type':'application/json','Host':u.netloc,'X-Event-ID':payload['event_id'],'X-Timestamp':stamp,'X-Signature':signature(secret,payload['event_id'],stamp,body)})
        response=conn.getresponse();raw=response.read(2_000_001)
        require(response.status==200 and len(raw)<=2_000_000,409,'QUERY_FAILED','对端暂未提供可验证回执')
        result=json.loads(raw)
        require(all(k in result for k in ('applied_release_id','base_release_id','snapshot_hash')),409,'QUERY_UNBOUND','查询结果缺少发布版本依据')
        from apps.api.enterprise import Receipt
        return Receipt.model_validate(result).model_dump(exclude_unset=True)
    finally:conn.close()


def scan_receipts(at=None):
    """One bounded pass every 30 seconds; overdue is reconciliation, never redispatch."""
    from packages.domain.config import DATA_DIR
    from packages.domain.integrations import receipt
    if (DATA_DIR/'delivery-restore-hold.json').exists():return 0
    current=time.time() if at is None else at
    with transaction(write=True) as s:
        rows=list(s.scalars(select(Delivery).where(Delivery.status=='RECEIVED',Delivery.receipt_due_at<=current).order_by(Delivery.receipt_due_at).limit(100).with_for_update(skip_locked=True)))
        for d in rows:d.status='RECONCILE';d.escalation_state='ACTION_REQUIRED';d.last_error='RECEIPT_TIMEOUT'
        count=len(rows)
        candidates=list(s.scalars(select(Delivery).where(Delivery.status=='RECONCILE',((Delivery.last_query_at.is_(None))|(Delivery.last_query_at<current-300))).order_by(Delivery.receipt_due_at).limit(100).with_for_update(skip_locked=True)))
        queries=[]
        for d in candidates:
            i=s.get(Integration,d.integration_id);a=s.get(ServiceAccount,i.account_id)
            if not i.receipt_query_url or not i.active or not a.active or a.expires_at<=current:continue
            d.last_query_at=current
            queries.append((d.id,d.fence_token,i.receipt_query_url,{'event_id':d.event_id,'delivery_id':d.id},cipher().decrypt(i.secret_cipher.encode()).decode()))
    for ident,token,url,payload,secret in queries:
        try:
            result=query_receipt(url,payload,secret)
            with transaction(write=True) as s:
                org_id=s.scalar(select(Delivery.org_id).where(Delivery.id==ident));s.scalar(select(Organization).where(Organization.id==org_id).with_for_update())
                d=s.scalar(select(Delivery).where(Delivery.id==ident).with_for_update())
                if d.fence_token!=token or d.status!='RECONCILE':continue
                i=s.get(Integration,d.integration_id)
                if i.active:receipt(s,s.get(ServiceAccount,i.account_id),d,result)
        except Exception as e:
            with transaction(write=True) as s:
                d=s.scalar(select(Delivery).where(Delivery.id==ident).with_for_update())
                if d.fence_token==token and d.status=='RECONCILE':d.last_error='QUERY_'+type(e).__name__
    return count
