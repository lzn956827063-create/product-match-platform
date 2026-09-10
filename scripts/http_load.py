"""Real TCP HTTP workload. Defaults to isolated local SQLite; no PostgreSQL claim."""
import argparse
import json
import os
import platform
import random
import subprocess
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import httpx
import numpy as np


def prepare(folder, reuse=False):
    os.environ['DATA_DIR']=str(folder);os.environ['DATABASE_URL']='sqlite:///'+str(folder/'app.db')
    from scripts.seed import main as seed, sample_rows, csv_bytes, MAPPING, add_file
    from packages.domain.db import transaction, uid
    from packages.domain.auth import Context,password_hash
    from packages.domain.models import User,Membership,Batch,Run,Revision
    from packages.domain.services import create_revision,create_run
    from workers.jobs import process_run
    from sqlalchemy import select
    if reuse and (folder/'app.db').exists():
        with transaction() as s:return s.scalar(select(Run).where(Run.total==10000)).id
    seed()
    with transaction(write=True) as s:
        operator=s.scalar(select(User).where(User.email=='operator@demo.local'));member=s.scalar(select(Membership).where(Membership.user_id==operator.id));c=Context(member.org_id,operator.id,['operator'])
        old=s.scalar(select(Run).where(Run.org_id==member.org_id));catalog=old.catalog_version_id;policy=old.policy_id;revision=old.revision_id
        for i in range(10):
            u=User(id=uid(),email=f'load-{i}@local.test',name=f'Load User {i}',password_hash=password_hash('Load2026!match'));s.add(u);s.flush();s.add(Membership(org_id=c.org_id,user_id=u.id,roles=['reviewer']))
        standard,_=sample_rows();sources=[]
        for i in range(10000):
            row=standard[i%len(standard)].copy();row[0]=f'LOAD-{i:06d}';sources.append(row)
        file=add_file(s,c.org_id,'http-load-10000.csv',csv_bytes(sources))
        batch=Batch(id=uid(),org_id=c.org_id,name='HTTP load 10000 synthetic records',supplier='Controlled simulation',created_by=operator.id);s.add(batch);s.flush()
        rev=create_revision(s,c,batch,{'file_id':file.id,'sheet':'CSV','header_row':1,'mapping':MAPPING,'exclude_rows':[]})
        run=create_run(s,c,{'revision_id':rev.id,'catalog_version_id':catalog,'policy_id':policy})
        historic=[create_run(s,c,{'revision_id':revision,'catalog_version_id':catalog,'policy_id':policy})['id'] for _ in range(3)]
    process_run(run['id'])
    for ident in historic:process_run(ident)
    return run['id']


def main(a):
    folder=Path(a.data).resolve();folder.mkdir(parents=True,exist_ok=True)
    run_id=prepare(folder,a.reuse)
    log=(folder/'server.log').open('w');server=subprocess.Popen([sys.executable,'-m','uvicorn','apps.api.main:app','--host','127.0.0.1','--port',str(a.port),'--no-access-log','--log-level','warning'],env=os.environ.copy(),stdout=log,stderr=log)
    base=f'http://127.0.0.1:{a.port}/api/v1';samples=[];resources=[];stop=threading.Event();lock=threading.Lock()
    try:
        for _ in range(100):
            try:
                if httpx.get(base+'/health').status_code==200:break
            except httpx.HTTPError:pass
            time.sleep(.1)
        else:raise RuntimeError('HTTP server did not start')
        clients=[]
        for i in range(10):
            client=httpx.Client(base_url=base,timeout=30)
            login=client.post('/auth/login',json={'email':f'load-{i}@local.test','password':'Load2026!match'});login.raise_for_status();client.headers['Authorization']='Bearer '+login.json()['access_token'];membership=client.get('/memberships').json()['items'][0];client.headers['X-Organization-ID']=membership['org_id'];clients.append(client)
        catalog_version=clients[0].get('/runs/'+run_id).json()['catalog_version_id']
        started=time.monotonic();deadline=started+a.seconds
        def monitor():
            while not stop.wait(5):
                result=subprocess.run(['ps','-o','rss=,pcpu=','-p',str(server.pid)],capture_output=True,text=True)
                try:rss,cpu=map(float,result.stdout.split());resources.append({'elapsed':time.monotonic()-started,'rss_mb':rss/1024,'cpu_percent':cpu})
                except ValueError:pass
        thread=threading.Thread(target=monitor,daemon=True);thread.start()
        def user(index):
            client=clients[index];rng=random.Random(index);cursor=None;chosen=None;refresh_at=started+600
            while time.monotonic()<deadline:
                tick=time.monotonic()
                if tick>refresh_at:
                    r=client.post('/auth/refresh',headers={'X-CSRF-Token':client.cookies.get('csrf_token','')});r.raise_for_status();client.headers['Authorization']='Bearer '+r.json()['access_token'];refresh_at=tick+600
                x=rng.random();kind='items'
                try:
                    if x<.60 or chosen is None:
                        r=client.get(f'/runs/{run_id}/items',params={'limit':50,'details':False,**({'cursor':cursor} if cursor else {})});body=r.json()
                        if r.status_code==200:
                            cursor=body['next_cursor'];eligible=[i for i in body['items'] if i['suggestion']=='RECOMMENDED'];chosen=rng.choice(eligible) if eligible else chosen
                    elif x<.70:
                        kind='detail';r=client.get(f'/items/{chosen["id"]}/candidates')
                    elif x<.85:
                        kind='review';r=client.post(f'/items/{chosen["id"]}/decisions',json={'action':'confirm','product_id':chosen['candidates'][0]['product_id'],'expected_version':chosen['version'],'reason':'Controlled real HTTP load measurement'})
                        if r.status_code==201:chosen['version']=r.json()['item_version']
                        elif r.status_code==409:chosen=None
                    elif x<.90:kind='runs';r=client.get('/runs')
                    elif x<.95:kind='search';r=client.get(f'/catalog-versions/{catalog_version}/products',params={'q':'黑色'})
                    else:kind='exports';r=client.get('/exports')
                    elapsed=time.monotonic()-tick
                    with lock:samples.append({'kind':kind,'ms':elapsed*1000,'status':r.status_code})
                except httpx.HTTPError:
                    with lock:samples.append({'kind':kind,'ms':(time.monotonic()-tick)*1000,'status':0})
                stop.wait(max(0,a.think-(time.monotonic()-tick)))
        with ThreadPoolExecutor(max_workers=10) as pool:list(pool.map(user,range(10)))
        stop.set();thread.join(timeout=2)
        grouped={}
        for kind in sorted({r['kind'] for r in samples}):
            rows=[r for r in samples if r['kind']==kind];values=[r['ms'] for r in rows];codes=defaultdict(int)
            for r in rows:codes[str(r['status'])]+=1
            grouped[kind]={'n':len(rows),'p50_ms':float(np.quantile(values,.5)),'p95_ms':float(np.quantile(values,.95)),'p99_ms':float(np.quantile(values,.99)),'statuses':dict(codes)}
        result={'scope':'Real TCP HTTP to Uvicorn, local SQLite WAL, 10 independently authenticated simulated users; no reverse proxy or PostgreSQL. Summary list plus selected-candidate details; each user pauses to a one-second request cycle.','seconds':time.monotonic()-started,'concurrency':10,'think_seconds':a.think,'task_records':10000,'historical_runs':4,'catalog_products':12,'dataset':'Controlled repeated phone SKUs with unique source IDs; performance workload only','results':grouped,'requests':len(samples),'unexpected_5xx_fraction':sum(r['status']>=500 or r['status']==0 for r in samples)/len(samples),'business_409':sum(r['status']==409 for r in samples),'resources':{'server_peak_sampled_rss_mb':max(r['rss_mb'] for r in resources),'server_peak_sampled_cpu_percent':max(r['cpu_percent'] for r in resources),'samples':resources},'connection_wait':'Not separately instrumented; included in endpoint latency. PostgreSQL pool wait remains unverified.','platform':platform.platform(),'python':platform.python_version()}
        Path(a.output).write_text(json.dumps(result,indent=2));print(json.dumps({k:v for k,v in result.items() if k!='resources'},indent=2))
    finally:
        stop.set();server.terminate();server.wait(timeout=15);log.close()
        for client in locals().get('clients',[]):client.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--seconds',type=int,default=900);p.add_argument('--reuse',action='store_true');p.add_argument('--port',type=int,default=18767);p.add_argument('--think',type=float,default=1);p.add_argument('--data',default='../../work/http-load-v11');p.add_argument('--output',default='docs/optimization/http-load.json');main(p.parse_args())
