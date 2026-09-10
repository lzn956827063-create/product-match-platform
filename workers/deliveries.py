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
        if d.status in ('APPLIED','PARTIAL','REJECTED','MANUAL','CANCELLED','RECEIVED'):
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
        d=s.get(Delivery,ident)
        if not s.scalar(select(DeliveryAttempt.id).where(DeliveryAttempt.org_id==d.org_id,DeliveryAttempt.delivery_id==ident,DeliveryAttempt.number==attempt)):
            s.add(DeliveryAttempt(org_id=d.org_id,delivery_id=ident,number=attempt,status_code=code,error=error))
        if d.fence_token!=token:return  # A receipt, replay or newer lease fenced this attempt.
        d.lease_until=0;d.last_error=error
        if error:
            d.status='MANUAL' if attempt>=8 else 'RETRY';d.next_retry=time.time()+min(3600,2**attempt*5)+random.uniform(0,5)
        else:d.status='RECEIVED'  # Only an authenticated receipt can mark APPLIED.
        if d.status in ('RECEIVED','MANUAL'):
            for e in s.scalars(select(Outbox).where(Outbox.kind=='delivery',Outbox.resource_id==ident)):e.completed=True
