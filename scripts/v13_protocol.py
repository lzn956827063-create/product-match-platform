"""Actual local TCP protocol test: interrupted delta, receipt timeout and old-version order."""
import os,json,time,subprocess,sys,csv,io,sqlite3
from pathlib import Path
from tests.conftest import env,TEST_DIR
from tests.test_v13 import impact_fixture,publish_new
from packages.domain.db import transaction,uid
from packages.domain.models import *
from packages.domain import integrations
from packages.domain.releases import release_event
from workers.deliveries import process_delivery,scan_receipts
from sqlalchemy import select
import httpx


def main(folder,output):
    folder=Path(folder).resolve();folder.mkdir(parents=True,exist_ok=True);generator=env.__wrapped__();fixture=next(generator);processes=[];logs=[]
    try:
        client,h,run,new,old,task,old_bytes=impact_fixture(fixture);org=h['admin']['X-Organization-ID'];os.environ['WEBHOOK_ALLOWED_HOSTS']='127.0.0.1';os.environ['WEBHOOK_ALLOW_LOOPBACK']='true'
        with transaction(write=True) as s:
            account=ServiceAccount(id=uid(),org_id=org,name='TCP 协议隔离验收',scopes=list(integrations.SCOPES),expires_at=time.time()+3600);credential=integrations.issue_credential(account);s.add(account);s.flush();secret='isolated-v13-protocol-secret'
            integration=Integration(id=uid(),org_id=org,account_id=account.id,name='TCP 模拟采购端',url='http://127.0.0.1:18769/notify',active=True,secret_cipher=integrations.cipher().encrypt(secret.encode()).decode(),receipt_timeout_seconds=30);s.add(integration);s.flush();integrations.enqueue_deliveries(s,s.get(BatchRelease,old['id']),s.scalar(select(ReleaseEvent).where(ReleaseEvent.release_id==old['id'],ReleaseEvent.kind=='ARTIFACTS_READY')))
            old_delivery=s.scalar(select(Delivery).where(Delivery.release_id==old['id']));old_id=old_delivery.id
        config={'api_url':'http://127.0.0.1:18768/api/v1','org_id':org,'credential':credential,'signing_secret':secret,'delta_page_size':2};config_path=folder/'receiver-config.json';config_path.write_text(json.dumps(config));config_path.chmod(0o600);state=folder/'receiver.db'
        for name,command in [('api',['uvicorn','apps.api.main:app','--host','127.0.0.1','--port','18768','--no-access-log']),('receiver',['scripts.mock_receiver','--config',str(config_path),'--state',str(state),'--port','18769'])]:
            log=(folder/(name+'.log')).open('w');logs.append(log);processes.append(subprocess.Popen([sys.executable,'-m',*command],stdout=log,stderr=log,env=os.environ.copy()))
        deadline=time.time()+20
        while time.time()<deadline:
            try:
                if httpx.get('http://127.0.0.1:18768/api/v1/live').status_code==200 and httpx.get('http://127.0.0.1:18769/').status_code==501:break
            except httpx.HTTPError:pass
            time.sleep(.2)
        def wait_applied(ident):
            deadline=time.time()+15
            while time.time()<deadline:
                with transaction() as s:
                    if s.get(Delivery,ident).status=='APPLIED':return
                time.sleep(.2)
            raise AssertionError('receipt missing')
        process_delivery(old_id);wait_applied(old_id)
        replacement=publish_new(client,h,new,old)
        with transaction() as s:new_id=s.scalar(select(Delivery.id).where(Delivery.release_id==replacement['id']))
        config['interrupt_after_page']=0;config_path.write_text(json.dumps(config));process_delivery(new_id)
        with sqlite3.connect(state) as s:
            baseline=s.execute('SELECT release_id FROM batches').fetchone()[0];assert baseline==old['id'];assert s.execute('SELECT count(*) FROM delta_stage').fetchone()[0]==2
        with transaction(write=True) as s:d=s.get(Delivery,new_id);assert d.status=='RETRY';d.next_retry=0
        del config['interrupt_after_page'];config['hold_receipts']=True;config_path.write_text(json.dumps(config));process_delivery(new_id)
        with transaction() as s:d=s.get(Delivery,new_id);assert d.status=='RECEIVED';due=d.receipt_due_at
        scan_receipts(due+1)
        with transaction() as s:assert s.get(Delivery,new_id).status=='RECONCILE'
        full=client.get('/api/v1/releases/'+replacement['id']+'/artifacts/full/download',headers=h['admin']).content;truth={r['stable_key']:r for r in csv.DictReader(io.StringIO(full.decode('utf-8-sig')))}
        with sqlite3.connect(state) as s:
            actual={k:json.loads(v) for k,v in s.execute('SELECT stable_key,row_data FROM mappings')};assert actual==truth
        with transaction(write=True) as s:s.get(Integration,integration.id).receipt_query_url='http://127.0.0.1:18769/receipt-query'
        scan_receipts(due+2);wait_applied(new_id)
        config['hold_receipts']=False;config_path.write_text(json.dumps(config));wait_applied(new_id)
        with transaction(write=True) as s:
            r=s.get(BatchRelease,old['id']);event=release_event(s,r,'ARTIFACTS_READY',None,{'scope':'controlled late old notification'});integrations.enqueue_deliveries(s,r,event);late=s.scalar(select(Delivery.id).where(Delivery.event_id==event.id))
        process_delivery(late);time.sleep(2.5)
        with sqlite3.connect(state) as s:assert s.execute('SELECT release_id FROM batches').fetchone()[0]==replacement['id'];assert {k:json.loads(v) for k,v in s.execute('SELECT stable_key,row_data FROM mappings')}==truth
        with transaction() as s:assert s.get(Delivery,late).status=='REJECTED'
        assert client.get('/api/v1/releases/'+old['id']+'/artifacts/full/download',headers=h['admin']).content==old_bytes
        result={'passed':True,'scope':'actual local HTTP and SQLite procurement simulator, not real ERP','checks':['full initial sync','interrupted delta pages leave current baseline intact','retry resumes identical staged pages','complete delta atomically equals full CSV including invalidations','2xx without receipt becomes RECONCILE at deadline scan','signed status query obtains a version-bound late receipt','duplicate bound receipt preserves APPLIED','new release followed by unseen older event rejects rollback','old artifact bytes preserved'],'full_rows':len(truth),'ports':[18768,18769]}
        Path(output).write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))
    finally:
        for p in processes:p.terminate()
        for p in processes:
            try:p.wait(timeout=10)
            except subprocess.TimeoutExpired:p.kill()
        for log in logs:log.close()
        generator.close()

if __name__=='__main__':main(sys.argv[1],sys.argv[2])
