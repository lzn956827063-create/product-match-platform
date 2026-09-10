"""30-minute mixed local workload, or reusable fixtures for a container deployment.

The local run explicitly does not certify 4 CPU / 16 GB PostgreSQL capacity.
"""
import argparse,json,os,platform,random,subprocess,sys,threading,time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import httpx
import numpy as np


def prepare(folder):
    os.environ['DATA_DIR']=str(folder);os.environ['DATABASE_URL']='sqlite:///'+str(folder/'app.db')
    from scripts.seed import main as seed,add_file,csv_bytes,MAPPING
    from packages.domain.db import transaction,uid
    from packages.domain.models import User,Membership,Run,Catalog,Batch
    from packages.domain.auth import Context,password_hash
    from packages.domain.services import create_catalog_version,create_revision,create_run
    from workers.jobs import process_run
    from sqlalchemy import select
    seed();definitions=[]
    for domain in ('demo','other'):
        with transaction(write=True) as s:
            actor=s.scalar(select(User).where(User.email==f'operator@{domain}.local'));m=s.scalar(select(Membership).where(Membership.user_id==actor.id));c=Context(m.org_id,actor.id,['operator','admin'])
            base=s.scalar(select(Run).where(Run.org_id==m.org_id));standard=[];sources=[]
            for i in range(50000):
                brand=['A牌','B牌','C牌','D牌'][i%4];capacity=['128','256','512','1024'][i%4];color=['黑色','白色','蓝色'][i%3];region=['国行','港版'][i%2]
                row=[f'STD-{i:06d}',f'{brand} M{i:05d} 8+{capacity} {color} {region} 单机',brand,f'm{i:05d}','8',capacity,color,region,'1','1999','CNY'];standard.append(row)
                if i<10000:
                    q=list(row);q[0]=f'SRC-{i:06d}'
                    if i%10==0:q[1]=q[1].replace(color,'');q[6]=''
                    elif i%10==1:q[5]='64'
                    elif i%10==2:q[1]=q[1].replace(f'M{i:05d}',f'M{90000+i}');q[3]=f'm{90000+i}'
                    elif i%10==3:q[1]=q[1].replace('单机','原封 单台')
                    sources.append(q)
            cat=Catalog(id=uid(),org_id=c.org_id,name='企业容量受控模拟标准库');s.add(cat);s.flush();f=add_file(s,c.org_id,'enterprise-standard.csv',csv_bytes(standard));cv=create_catalog_version(s,c,cat,{'file_id':f.id,'sheet':'CSV','header_row':1,'mapping':MAPPING});cv.status='PUBLISHED'
            batch=Batch(id=uid(),org_id=c.org_id,name='企业容量混合来源（构造数据）',supplier='独立合成供应商 '+domain,created_by=actor.id);s.add(batch);s.flush();f=add_file(s,c.org_id,'enterprise-source.csv',csv_bytes(sources));rev=create_revision(s,c,batch,{'file_id':f.id,'sheet':'CSV','header_row':1,'mapping':MAPPING,'exclude_rows':[]});run=create_run(s,c,{'revision_id':rev.id,'catalog_version_id':cv.id,'policy_id':base.policy_id})
            for index in range(5):
                u=User(id=uid(),email=f'enterprise-load-{domain}-{index}@test.local',name=f'模拟并发用户 {domain} {index}',password_hash=password_hash('Load2026!match'));s.add(u);s.flush();s.add(Membership(org_id=c.org_id,user_id=u.id,roles=['operator','reviewer','publisher']))
            definitions.append({'run_id':run['id'],'revision_id':rev.id,'catalog_version_id':cv.id,'policy_id':base.policy_id,'batch_id':batch.id,'org_id':c.org_id,'domain':domain})
        process_run(run['id'])
    return definitions


def main(args):
    folder=Path(args.data).resolve();folder.mkdir(parents=True,exist_ok=True)
    fixture=folder/'fixture.json'
    if fixture.exists():
        os.environ['DATA_DIR']=str(folder);os.environ['DATABASE_URL']='sqlite:///'+str(folder/'app.db');definitions=json.loads(fixture.read_text())
    else:
        definitions=prepare(folder);fixture.write_text(json.dumps(definitions));print('Prepared two organizations, each 50,000 catalog and 10,000 source rows',flush=True)
    processes=[];logs=[];stop=threading.Event();samples=[];resources=[];lock=threading.Lock();clients=[]
    try:
        for name,command in [('api',['uvicorn','apps.api.main:app','--host','127.0.0.1','--port',str(args.port),'--no-access-log','--log-level','warning']),('worker',['workers.dispatcher'])]:
            log=(folder/(name+'.log')).open('w');logs.append(log);processes.append(subprocess.Popen([sys.executable,'-m',*command],stdout=log,stderr=log,env=os.environ.copy()))
        base=f'http://127.0.0.1:{args.port}/api/v1'
        for _ in range(120):
            try:
                if httpx.get(base+'/live',timeout=1).status_code==200:break
            except httpx.HTTPError:pass
            time.sleep(.25)
        else:raise RuntimeError('API startup failed')
        for index in range(10):
            d=definitions[index//5];c=httpx.Client(base_url=base,timeout=30);r=c.post('/auth/login',json={'email':f'enterprise-load-{d["domain"]}-{index%5}@test.local','password':'Load2026!match'});r.raise_for_status();c.headers.update({'Authorization':'Bearer '+r.json()['access_token'],'X-Organization-ID':d['org_id']});clients.append(c)
        # One concurrent background run per organization during API load.
        background=[]
        for index in (0,5):
            d=definitions[index//5];r=clients[index].post('/runs',headers={'Idempotency-Key':'mixed-background-1'},json={k:d[k] for k in ('revision_id','catalog_version_id','policy_id')});r.raise_for_status();background.append(r.json()['id'])
        started=time.monotonic();deadline=started+args.seconds
        def monitor():
            while not stop.wait(5):
                result=subprocess.run(['ps','-o','pid=,rss=,pcpu=','-p',','.join(str(p.pid) for p in processes)],capture_output=True,text=True)
                rows=[]
                for line in result.stdout.splitlines():
                    try:pid,rss,cpu=map(float,line.split());rows.append({'pid':int(pid),'rss_mb':rss/1024,'cpu_percent':cpu})
                    except ValueError:pass
                resources.append({'elapsed':time.monotonic()-started,'processes':rows,'total_rss_mb':sum(r['rss_mb'] for r in rows)})
        mt=threading.Thread(target=monitor,daemon=True);mt.start()
        def user(index):
            c=clients[index];d=definitions[index//5];rng=random.Random(index);cursor=None;chosen=None;refresh_at=started+600
            while time.monotonic()<deadline:
                tick=time.monotonic();kind='list'
                try:
                    if tick>refresh_at:
                        r=c.post('/auth/refresh',headers={'X-CSRF-Token':c.cookies.get('csrf_token','')});r.raise_for_status();c.headers['Authorization']='Bearer '+r.json()['access_token'];refresh_at=tick+600
                    p=rng.random()
                    if p<.55 or chosen is None:
                        r=c.get('/runs/'+d['run_id']+'/items',params={'details':False,'limit':50,**({'cursor':cursor} if cursor else {})})
                        if r.status_code==200:body=r.json();cursor=body['next_cursor'];chosen=rng.choice(body['items']) if body['items'] else None
                    elif p<.7:kind='detail';r=c.get('/items/'+chosen['id']+'/candidates')
                    elif p<.85:
                        kind='review';r=c.post('/items/'+chosen['id']+'/decisions',json={'action':'unmatched','expected_version':chosen['version'],'reason':'构造数据 HTTP 验收，不代表真实人工结论'})
                        if r.status_code==201:chosen['version']=r.json()['item_version']
                        elif r.status_code==409:chosen=None
                    elif p<.94:kind='catalog_search';r=c.get('/catalog-versions/'+d['catalog_version_id']+'/products',params={'q':'M'+str(rng.randrange(50000)).zfill(5)})
                    else:kind='release_list';r=c.get('/releases',params={'batch_id':d['batch_id']})
                    with lock:samples.append({'kind':kind,'elapsed':tick-started,'ms':(time.monotonic()-tick)*1000,'status':r.status_code})
                except httpx.HTTPError:
                    with lock:samples.append({'kind':kind,'elapsed':tick-started,'ms':(time.monotonic()-tick)*1000,'status':0})
                stop.wait(max(0,args.think-(time.monotonic()-tick)))
        with ThreadPoolExecutor(max_workers=10) as pool:list(pool.map(user,range(10)))
        stop.set();mt.join(timeout=2)
        results={}
        for kind in sorted({r['kind'] for r in samples}):
            rows=[r for r in samples if r['kind']==kind];v=[r['ms'] for r in rows];results[kind]={'n':len(v),'p50_ms':float(np.quantile(v,.5)),'p95_ms':float(np.quantile(v,.95)),'p99_ms':float(np.quantile(v,.99)),'statuses':dict(Counter(r['status'] for r in rows))}
        background_results=[clients[index].get('/runs/'+ident).json() for index,ident in zip((0,5),background)]
        report={'scope':'Actual local TCP API and database workers, SQLite WAL. No container CPU limit, PostgreSQL, Redis, MinIO or reverse proxy; not enterprise deployment admission.','seconds':time.monotonic()-started,'think_seconds':args.think,'users':10,'organizations':2,'catalog_per_org':50000,'sources_per_batch':10000,'data':'Controlled invented products with unique models, capacity/color/region variations; 10% missing, 10% internal conflict, 10% absent models, 10% aliases, 60% complete. Not matching accuracy truth.','results':results,'requests':len(samples),'unexpected_errors':sum(r['status']==0 or r['status']>=500 for r in samples),'business_conflicts':sum(r['status']==409 for r in samples),'limited':sum(r['status']==429 for r in samples),'background_runs':[{'id':r['id'],'status':r['status'],'processed':r['processed'],'timings':r['timings']} for r in background_results],'resources':resources,'peak_total_sampled_rss_mb':max((r['total_rss_mb'] for r in resources),default=None),'platform':platform.platform(),'cpu_count':os.cpu_count(),'limitations':['Database connection and lock waits not separately measured on SQLite','No formal release validation/download load in this mix; tested separately','Two background runs execute once at start; stable phase is primarily API traffic','Sampling RSS includes API and local dispatcher threads, not production prefork container workers']}
        Path(args.output).parent.mkdir(parents=True,exist_ok=True);Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps({k:v for k,v in report.items() if k!='resources'},ensure_ascii=False,indent=2),flush=True)
    finally:
        stop.set()
        for p in processes:p.terminate()
        for p in processes:
            try:p.wait(timeout=15)
            except subprocess.TimeoutExpired:p.kill()
        for c in clients:c.close()
        for log in logs:log.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--seconds',type=int,default=1800);p.add_argument('--think',type=float,default=1);p.add_argument('--port',type=int,default=18767);p.add_argument('--data',default='../../work/enterprise-load');p.add_argument('--output',default='docs/enterprise/local-mixed-load.json');main(p.parse_args())
