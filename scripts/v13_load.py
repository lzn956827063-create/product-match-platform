"""Fixed-code 30-minute local mixed load with raw traces, continuous jobs, publish/download."""
import argparse,json,os,sys,time,threading,random,subprocess,hashlib,platform,re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
import httpx,numpy as np


def prepare_publication(definitions):
    from sqlalchemy import select
    from packages.domain.db import transaction,uid
    from packages.domain.models import User,Membership,Run,Batch,Source,Revision,Item,BatchRelease
    from packages.domain.auth import Context
    from packages.domain.services import create_revision,create_run,decide
    from packages.domain.releases import create_release,publish_release
    from scripts.seed import add_file,csv_bytes,MAPPING
    from workers.jobs import process_run
    from workers.releases import process_validation,process_files
    for d in definitions:
        if d.get('publication_batch_id'):continue
        with transaction(write=True) as s:
            actor=s.scalar(select(User).where(User.email==f'operator@{d["domain"]}.local'));c=Context(d['org_id'],actor.id,['operator','publisher']);reviewer=s.scalar(select(User).where(User.email==f'reviewer@{d["domain"]}.local'));review=Context(d['org_id'],reviewer.id,['reviewer'])
            sources=list(s.scalars(select(Source).where(Source.revision_id==d['revision_id']).order_by(Source.row_no).limit(100)));revision=s.get(Revision,d['revision_id']);records=[[str(src.raw.get(revision.mapping.get(k),'')) for k in MAPPING] for src in sources]
            f=add_file(s,d['org_id'],'continuous-publication.csv',csv_bytes(records));batch=Batch(id=uid(),org_id=d['org_id'],name='持续发布隔离验收（100 条）',supplier='受控构造来源',created_by=actor.id);s.add(batch);s.flush();rev=create_revision(s,c,batch,{'file_id':f.id,'sheet':'CSV','header_row':1,'mapping':MAPPING,'exclude_rows':[]});run=create_run(s,c,{'revision_id':rev.id,'catalog_version_id':d['catalog_version_id'],'policy_id':d['policy_id']});batch_id=batch.id
        process_run(run['id'])
        with transaction(write=True) as s:
            for item in s.scalars(select(Item).where(Item.run_id==run['id'])):decide(s,review,item.id,{'action':'unmatched','expected_version':item.version,'reason':'持续负载预置人工决定（构造测试，不是真实真值）'})
        base=None
        for _ in range(10):
            with transaction(write=True) as s:
                r=create_release(s,c,batch_id,{'baseline_run_id':run['id'],'base_release_id':base,'correction_run_ids':[],'branch_choices':{},'confirm_scope_change':False,'scope_reason':''});r.status='VALIDATING';ident=r.id
            process_validation(ident)
            with transaction(write=True) as s:r=s.get(BatchRelease,ident);assert r.status=='READY',r.validation;publish_release(s,c,r,r.version)
            process_files(ident);base=ident
        d.update(publication_batch_id=batch_id,publication_run_id=run['id'],publication_base_id=base)
    return definitions


def main(a):
    folder=Path(a.data).resolve();os.environ['DATA_DIR']=str(folder);os.environ['DATABASE_URL']='sqlite:///'+str(folder/'app.db');os.environ['DB_AUTO_CREATE']='false'
    definitions=json.loads((folder/'fixture.json').read_text());definitions=prepare_publication(definitions);(folder/'fixture.json').write_text(json.dumps(definitions))
    output=Path(a.output);output.mkdir(parents=True,exist_ok=True)
    code=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip();source_hash=hashlib.sha256(b''.join(p.read_bytes() for parent in ['apps/api','packages','workers'] for p in sorted(Path(parent).rglob('*.py')))).hexdigest()
    run_label=str(int(time.time()));stop=threading.Event();lock=threading.Lock();samples=[];resources=[];background=[];jobs=[];logs=[];clients=[];errors=[]
    raw=(output/'requests.jsonl').open('w');resource_file=(output/'resources.jsonl').open('w');started=time.monotonic();deadline=started+a.seconds
    def record(kind,tick,response=None,error=None):
        sample={'elapsed':tick-started,'phase':'cold' if tick-started<120 else 'steady','kind':kind,'method':response.request.method if response else None,'route':re.sub(r'[0-9a-f]{8}-[0-9a-f-]{27,}', '{id}', response.request.url.path) if response else kind,'status':response.status_code if response else 0,'milliseconds':(time.monotonic()-tick)*1000,'request_id':response.headers.get('X-Request-ID') if response else None,'bytes':len(response.content) if response else 0,'error':error}
        with lock:samples.append(sample);raw.write(json.dumps(sample)+'\n');raw.flush()
        return response
    def call(c,kind,method,path,**kw):
        tick=time.monotonic()
        try:return record(kind,tick,c.request(method,path,**kw))
        except httpx.HTTPError as e:record(kind,tick,error=type(e).__name__);raise
    def login(index):
        d=definitions[index//5] if index<10 else definitions[index-10];n=index%5 if index<10 else 0
        c=httpx.Client(base_url=f'http://127.0.0.1:{a.port}/api/v1',timeout=30)
        r=c.post('/auth/login',json={'email':f'enterprise-load-{d["domain"]}-{n}@test.local','password':'Load2026!match'});r.raise_for_status();c.headers.update({'Authorization':'Bearer '+r.json()['access_token'],'X-Organization-ID':d['org_id']});return c
    try:
        for name,command in [('api',['uvicorn','apps.api.main:app','--host','127.0.0.1','--port',str(a.port),'--no-access-log','--log-level','info']),('worker',['workers.dispatcher'])]:
            log=(folder/(name+'-v13.log')).open('w');logs.append(log);jobs.append(subprocess.Popen([sys.executable,'-m',*command],stdout=log,stderr=log,env=os.environ.copy()))
        for _ in range(100):
            try:
                if httpx.get(f'http://127.0.0.1:{a.port}/api/v1/live',timeout=1).status_code==200:break
            except httpx.HTTPError:pass
            time.sleep(.2)
        clients=[login(i) for i in range(12)];started=time.monotonic();deadline=started+a.seconds
        def refresh(c):
            r=call(c,'auth_refresh','POST','/auth/refresh',headers={'X-CSRF-Token':c.cookies.get('csrf_token','')});r.raise_for_status();c.headers['Authorization']='Bearer '+r.json()['access_token']
        def monitor():
            while not stop.wait(5):
                result=subprocess.run(['ps','-o','pid=,rss=,pcpu=','-p',','.join(str(p.pid) for p in jobs)],capture_output=True,text=True);processes=[]
                for line in result.stdout.splitlines():
                    pid,rss,cpu=map(float,line.split());processes.append({'pid':int(pid),'rss_mb':rss/1024,'cpu_percent':cpu})
                row={'elapsed':time.monotonic()-started,'processes':processes,'total_rss_mb':sum(p['rss_mb'] for p in processes)}
                resources.append(row);resource_file.write(json.dumps(row)+'\n');resource_file.flush()
        def user(index):
            c=clients[index];d=definitions[index//5];rng=random.Random(index);cursor=None;chosen=None;refresh_at=started+600
            while time.monotonic()<deadline:
                tick=time.monotonic()
                try:
                    if tick>refresh_at:refresh(c);refresh_at=tick+600
                    value=rng.random()
                    if value<.5 or chosen is None:
                        r=call(c,'list','GET','/runs/'+d['run_id']+'/items',params={'details':False,'limit':50,**({'cursor':cursor} if cursor else {})})
                        if r.status_code==200:body=r.json();cursor=body['next_cursor'];chosen=rng.choice(body['items']) if body['items'] else None
                    elif value<.65:call(c,'detail','GET','/items/'+chosen['id']+'/candidates')
                    elif value<.82:
                        r=call(c,'review','POST','/items/'+chosen['id']+'/decisions',json={'action':'unmatched','expected_version':chosen['version'],'reason':'持续构造数据验收操作，不代表真人结论'})
                        if r.status_code==201:chosen['version']=r.json()['item_version']
                        elif r.status_code==409:chosen=None
                    elif value<.97:
                        mode=rng.choice(['exact','prefix','fuzzy']);q=('STD-' if mode=='exact' else 'm')+str(rng.randrange(50000)).zfill(6 if mode=='exact' else 5)
                        call(c,'search_'+mode,'GET','/catalog-versions/'+d['catalog_version_id']+'/products',params={'q':q,'mode':mode,'limit':50})
                    else:call(c,'release_list','GET','/releases',params={'batch_id':d['publication_batch_id']})
                except httpx.HTTPError:pass
                stop.wait(max(0,a.think-(time.monotonic()-tick)))
        def workflow(index):
            c=clients[10+index];d=definitions[index];active=[];next_publish=started;refresh_at=started+600
            while time.monotonic()<deadline:
                try:
                    if time.monotonic()>refresh_at:refresh(c);refresh_at=time.monotonic()+600
                    retained=[]
                    for ident in active:
                        r=call(c,'background_status','GET','/runs/'+ident);r.raise_for_status();row=r.json()
                        if row['status'] in ('SUCCEEDED','FAILED','CANCELLED'):
                            background.append({k:row.get(k) for k in ('id','status','created_at','started_at','finished_at','processed','timings')})
                        else:retained.append(ident)
                    active=retained
                    # Keep one executing and one queued for each organization throughout measurement.
                    while len(active)<2 and time.monotonic()<deadline-10:
                        r=call(c,'background_create','POST','/runs',headers={'Idempotency-Key':run_label+'-'+str(index)+'-'+str(time.monotonic_ns())},json={k:d[k] for k in ('revision_id','catalog_version_id','policy_id')});r.raise_for_status();active.append(r.json()['id'])
                    if time.monotonic()>=next_publish:
                        r=call(c,'release_create','POST','/batches/'+d['publication_batch_id']+'/releases',headers={'Idempotency-Key':run_label+'-release-'+str(time.monotonic_ns())},json={'baseline_run_id':d['publication_run_id'],'base_release_id':d['publication_base_id']});r.raise_for_status();release=r.json();ident=release['id'];call(c,'release_validate','POST','/releases/'+ident+'/validate').raise_for_status()
                        until=min(deadline,time.monotonic()+60)
                        while time.monotonic()<until:
                            r=call(c,'release_status','GET','/releases/'+ident);r.raise_for_status();release=r.json()
                            if release['status']!='VALIDATING':break
                            stop.wait(.5)
                        if release['status']=='READY':
                            r=call(c,'release_publish','POST','/releases/'+ident+'/publish',headers={'Idempotency-Key':run_label+'-publish-'+ident},json={'expected_version':release['version']});r.raise_for_status();d['publication_base_id']=ident
                            until=min(deadline,time.monotonic()+60)
                            while time.monotonic()<until:
                                r=call(c,'release_status','GET','/releases/'+ident);r.raise_for_status()
                                if len(r.json()['artifacts'])==3 and all(x['status']=='SUCCEEDED' for x in r.json()['artifacts']):break
                                stop.wait(.5)
                            for kind in ('full','mapped','delta'):
                                r=call(c,'download_'+kind,'GET','/releases/'+ident+'/artifacts/'+kind+'/download');r.raise_for_status()
                                if hashlib.sha256(r.content).hexdigest()!=r.headers['X-File-SHA256']:raise ValueError('ARTIFACT_HASH')
                        else:errors.append({'phase':'publication','status':release['status']})
                        next_publish=time.monotonic()+60
                except (httpx.HTTPError,ValueError) as e:errors.append({'phase':'workflow','error':type(e).__name__})
                stop.wait(3)
            for ident in active:
                try:
                    r=c.get('/runs/'+ident).json();background.append({k:r.get(k) for k in ('id','status','created_at','started_at','finished_at','processed','timings')})
                except Exception:pass
        monitor_thread=threading.Thread(target=monitor,daemon=True);monitor_thread.start()
        with ThreadPoolExecutor(12) as pool:
            futures=[pool.submit(user,i) for i in range(10)]+[pool.submit(workflow,i) for i in range(2)]
            for f in futures:f.result()
        stop.set();monitor_thread.join(timeout=2)
        summaries={}
        for phase in ('all','cold','steady'):
            selected=[s for s in samples if phase=='all' or s['phase']==phase];summaries[phase]={}
            for kind in sorted({s['kind'] for s in selected}):
                rows=[s for s in selected if s['kind']==kind];values=[s['milliseconds'] for s in rows]
                summaries[phase][kind]={'count':len(rows),'p50_ms':float(np.quantile(values,.5)),'p95_ms':float(np.quantile(values,.95)),'p99_ms':float(np.quantile(values,.99)),'statuses':dict(Counter(s['status'] for s in rows))}
        report={'scope':'local SQLite WAL, actual TCP, no container limits or production dependencies; synthetic data','code_commit':code,'source_hash':source_hash,'seconds':time.monotonic()-started,'users':10,'workflow_drivers':2,'think_seconds':a.think,'organizations':2,'catalog_per_org':50000,'sources_per_main_batch':10000,'publication_rows':100,'history_releases_per_org_before_run':10,'cold_seconds':120,'requests':len(samples),'unexpected_errors':sum(s['status']==0 or s['status']>=500 for s in samples),'workflow_errors':errors,'results':summaries,'background_runs':background,'peak_total_sampled_rss_mb':max((r['total_rss_mb'] for r in resources),default=None),'platform':platform.platform(),'cpu_count':os.cpu_count(),'raw_requests':'requests.jsonl','resources':'resources.jsonl','limitations':['SQLite writer scheduling is not PostgreSQL transaction capacity','2 workflow drivers supplement 10 interactive users','100-row publications are separate from 10000-row matching batches','First 120 seconds classified cold; existing model files present, worker process cache starts empty','Jobs pending at measurement end are listed and not counted as completed','DB lock-statement timing includes statement work; PostgreSQL wait-event analysis not executed']}
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps({'seconds':report['seconds'],'requests':report['requests'],'unexpected_errors':report['unexpected_errors'],'workflow_errors':len(errors),'background_runs':len(background),'commit':code}),flush=True)
    finally:
        (folder/'fixture.json').write_text(json.dumps(definitions))
        stop.set();raw.close();resource_file.close()
        for p in jobs:p.terminate()
        for p in jobs:
            try:p.wait(timeout=15)
            except subprocess.TimeoutExpired:p.kill()
        for c in clients:c.close()
        for f in logs:f.close()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--data',required=True);p.add_argument('--output',required=True);p.add_argument('--seconds',type=int,default=1800);p.add_argument('--think',type=float,default=1);p.add_argument('--port',type=int,default=18767);main(p.parse_args())
