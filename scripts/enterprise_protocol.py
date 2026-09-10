"""Actual local HTTP integration drill against an isolated seeded development instance.
No real ERP, no live business data. Credentials only in the private work directory.
"""
import argparse,csv,hashlib,io,json,os,sqlite3,subprocess,sys,time
from pathlib import Path
from uuid import uuid4
import httpx
from packages.domain.integrations import signature


def main(base,folder,output,node):
    folder=Path(folder).resolve();folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    api=httpx.Client(base_url=base+'/api/v1',timeout=30);headers={}
    def request(method,path,role='admin',body=None):
        r=api.request(method,path,headers={**headers.get(role,{}),'Idempotency-Key':str(uuid4())},json=body)
        if r.status_code>=400:raise RuntimeError(f'{method} {path}: {r.status_code} {r.text[:400]}')
        return r
    def get(path,role='admin'):return request('GET',path,role).json()
    def post(path,body=None,role='admin'):return request('POST',path,role,body or {}).json()
    def until(fn,condition,seconds=120):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            result=fn()
            if condition(result):return result
            time.sleep(.5)
        raise RuntimeError('Timed out waiting for '+repr(result))
    for role in ('admin','operator','reviewer'):
        r=post('/auth/login',{'email':role+'@demo.local','password':'Demo2026!match'},role);headers[role]={'Authorization':'Bearer '+r['access_token']};headers[role]['X-Organization-ID']=get('/memberships',role)['items'][0]['org_id']
    original=next(x for x in get('/batches')['items'] if x['revisions'])
    revision=next(x for x in original['revisions'] if not x.get('source_links'))
    config={k:revision[k] for k in ('file_id','sheet','header_row','mapping')};config['exclude_rows']=revision['exclusion_rows']
    batch=post('/batches',{**config,'name':'企业 HTTP 交付演练 '+str(int(time.time())),'supplier':'受控模拟采购'},'operator')
    baseline=get('/runs?status=SUCCEEDED')['items'][0]
    run=post('/runs',{'revision_id':batch['revision']['id'],'catalog_version_id':baseline['catalog_version_id'],'policy_id':baseline['policy_id']},'operator')
    until(lambda:get('/runs/'+run['id']),lambda r:r['status']=='SUCCEEDED')
    items=get('/runs/'+run['id']+'/items?limit=100','reviewer')['items'];mapped=0
    for item in items:
        claim=post('/items/'+item['id']+'/claim',role='reviewer')
        body={'action':'unmatched','reason':'受控模拟演练：独立核对记录','expected_version':item['version'],'claim_token':claim['claim_token']}
        if item['suggestion']=='RECOMMENDED':
            detail=get('/items/'+item['id']+'/candidates','reviewer')
            body.update(action='confirm',product_id=detail['candidates'][0]['product_id']);mapped+=1
        post('/items/'+item['id']+'/decisions',body,'reviewer')
        request('PUT','/sources/'+item['source']['id']+'/identity','operator',{'stable_key':'row:'+str(item['source']['row_no']),'reason':'受控模拟：逐行确认来源身份'})
    for old in get('/integrations')['items']:
        if old['name']=='本机 HTTP 模拟采购':request('PUT','/integrations/'+old['id'],body={'active':False})
    account=post('/service-accounts',{'name':'隔离采购模拟器','scopes':['releases:read','deliveries:read','receipts:write'],'expires_in_days':1})
    integration=post('/integrations',{'name':'本机 HTTP 模拟采购','account_id':account['id'],'url':'http://127.0.0.1:18768/notify','active':True})
    config={'api_url':base+'/api/v1','org_id':headers['admin']['X-Organization-ID'],'credential':account['credential'],'signing_secret':integration['signing_secret'],'fail_first_notifications':1}
    config_path=folder/'receiver-config.json';config_path.write_text(json.dumps(config));config_path.chmod(0o600)
    state=folder/'receiver.db';log=(folder/'receiver.log').open('w')
    receiver=subprocess.Popen([sys.executable,'-m','scripts.mock_receiver','--config',str(config_path),'--state',str(state),'--port','18768'],stdout=log,stderr=log)
    timeline=[]
    try:
        time.sleep(1)
        release=post('/batches/'+batch['batch']['id']+'/releases',{'baseline_run_id':run['id']});post('/releases/'+release['id']+'/validate');release=until(lambda:get('/releases/'+release['id']),lambda r:r['status']!='VALIDATING');assert release['status']=='READY',release['validation']
        fixture=folder/'browser-release.json';fixture.write_text(json.dumps({'release_id':release['id'],'batch_name':batch['batch']['name']}))
        subprocess.run([node,'scripts/browser-release.mjs',str(fixture)],env={**os.environ,'APP_URL':base},check=True)
        def delivery(rid,kind):return next((x for x in get('/deliveries')['items'] if x['release_id']==rid and x['kind']==kind),{})
        applied=until(lambda:delivery(release['id'],'ARTIFACTS_READY'),lambda r:r.get('status')=='APPLIED',180)
        attempts=get('/deliveries/'+applied['id']+'/attempts')['items'];assert any(x['status_code']==503 for x in attempts) and any(x['status_code']==202 for x in attempts)
        timeline.append({'case':'temporary_503_then_durable_retry_and_authenticated_APPLIED','attempts':[{k:x[k] for k in ('number','status_code','error')} for x in attempts]})
        release=get('/releases/'+release['id'])
        payload={'event_id':applied['event_id'],'delivery_id':applied['id'],'release_id':release['id'],'kind':applied['kind'],'base_release_id':release['base_release_id'],'snapshot_hash':release['snapshot_hash'],'number':release['number'],'org_id':headers['admin']['X-Organization-ID'],'fetch_path':'/api/v1/service/releases/'+release['id']}
        body=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
        for _ in range(10):
            stamp=str(int(time.time()));r=httpx.post('http://127.0.0.1:18768/notify',content=body,headers={'X-Event-ID':applied['event_id'],'X-Timestamp':stamp,'X-Signature':signature(config['signing_secret'],applied['event_id'],stamp,body)});assert r.status_code==202
        with sqlite3.connect(state) as s:
            events=s.execute('SELECT COUNT(*) FROM events WHERE event_id=?',(applied['event_id'],)).fetchone()[0];rows=s.execute('SELECT COUNT(*) FROM mappings WHERE batch_id=?',(batch['batch']['id'],)).fetchone()[0]
        assert events==1 and rows==release['manifest']['total'];timeline.append({'case':'ten_duplicate_notifications','event_records':events,'unique_source_rows':rows})
        config['partial_keys']=['row:'+str(items[0]['source']['row_no'])];config_path.write_text(json.dumps(config))
        second=post('/batches/'+batch['batch']['id']+'/releases',{'baseline_run_id':run['id'],'base_release_id':release['id']});post('/releases/'+second['id']+'/validate');second=until(lambda:get('/releases/'+second['id']),lambda r:r['status']!='VALIDATING');assert second['status']=='READY';post('/releases/'+second['id']+'/publish',{'expected_version':second['version']})
        partial=until(lambda:delivery(second['id'],'ARTIFACTS_READY'),lambda r:r.get('status')=='PARTIAL');assert len(partial['receipt']['failed_rows'])==1
        config['partial_keys']=[];config_path.write_text(json.dumps(config));post('/deliveries/'+partial['id']+'/replay',{'reason':'受控单条错误已修复，保留事件编号补偿'})
        recovered=until(lambda:delivery(second['id'],'ARTIFACTS_READY'),lambda r:r.get('status')=='APPLIED');assert partial['event_id']==recovered['event_id']
        timeline.append({'case':'partial_single_row_repair_and_replay','failed_rows':1,'same_event_id':True})
        post('/releases/'+second['id']+'/revoke',{'reason':'模拟采购撤销验收'});revoked=until(lambda:delivery(second['id'],'REVOKED'),lambda r:r.get('status')=='APPLIED')
        with sqlite3.connect(state) as s:invalidated=s.execute('SELECT invalidated FROM batches WHERE batch_id=?',(batch['batch']['id'],)).fetchone()[0]
        assert invalidated==1;timeline.append({'case':'separate_revocation_event_and_APPLIED_receipt','invalidated':True})
        content=request('GET','/releases/'+release['id']+'/artifacts/full/download').content;browser_result=json.loads(Path('docs/enterprise/browser/release-results.json').read_text());assert hashlib.sha256(content).hexdigest()==browser_result['download_sha256']
        result={'passed':True,'scope':'Actual TCP local API + SQLite durable worker + HTTP procurement simulator; no real ERP','total_rows':rows,'mapped':mapped,'timeline':timeline,'immutable_full_sha256':hashlib.sha256(content).hexdigest(),'release_ids':[release['id'],second['id']],'integration_id':integration['id'],'batch_id':batch['batch']['id']}
        Path(output).write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:receiver.terminate();receiver.wait(timeout=15);log.close();api.close()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base',default='http://127.0.0.1:18766');p.add_argument('--work',default='../../work/enterprise-protocol');p.add_argument('--output',default='docs/enterprise/protocol.json');p.add_argument('--node',default='node');a=p.parse_args();main(a.base,a.work,a.output,a.node)
