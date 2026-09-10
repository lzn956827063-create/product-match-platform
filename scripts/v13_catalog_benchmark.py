"""Independent synthetic catalog analysis benchmark; no production capacity claim."""
import argparse,os,time,json,threading
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--size',type=int,required=True);p.add_argument('--data',required=True);p.add_argument('--output',required=True);args=p.parse_args()
folder=Path(args.data).resolve();folder.mkdir(parents=True,exist_ok=True)
os.environ['DATA_DIR']=str(folder);os.environ['DATABASE_URL']='sqlite:///'+str(folder/'app.db')
from scripts.seed import main as seed
from scripts.demo_enterprise import main as roles
from packages.domain.db import transaction,uid
from packages.domain.models import *
from packages.domain.auth import Context
from packages.domain.catalog_changes import create_change
from packages.domain.catalog_plans import create as create_plan
from packages.matching.normalize import digest
from workers.catalog_changes import process_change
from workers.catalog_plans import process_plan
from sqlalchemy import select,insert,func
from fastapi.testclient import TestClient
from apps.api.main import app
import numpy as np
seed();roles()
with transaction(write=True) as s:
    actor=s.scalar(select(User).where(User.email=='admin@demo.local'));membership=s.scalar(select(Membership).where(Membership.user_id==actor.id));c=Context(membership.org_id,actor.id,membership.roles);run=s.scalar(select(Run).where(Run.org_id==c.org_id));source=s.scalar(select(Source).where(Source.revision_id==run.revision_id));item=s.scalar(select(Item).where(Item.source_id==source.id));cat=Catalog(id=uid(),org_id=c.org_id,name='独立规模夹具');s.add(cat);s.flush();prior=s.get(CatalogVersion,run.catalog_version_id)
    versions=[]
    for n in (1,2):
        v=CatalogVersion(id=uid(),org_id=c.org_id,catalog_id=cat.id,file_id=prior.file_id,number=n,status='PUBLISHED',content_hash=f'benchmark-{args.size}-{n}',row_count=args.size,errors=[],mapping=prior.mapping);s.add(v);versions.append(v)
    s.flush();old_ids=[];links={};before=[];after=[]
    for n in range(args.size):
        old_id,new_id=uid(),uid();old_ids.append(old_id);norm={'name':f'测试商品 M{n:06d}','model':f'm{n:06d}','brand':'A牌','storage':'128GB','ram':'8GB','color':'黑色','region':'国行','pack_count':1};changed={**norm,'storage':'256GB'} if n%100==1 else norm
        before.append(dict(id=old_id,org_id=c.org_id,version_id=versions[0].id,sku=f'P{n:06d}',raw=norm,normalized=norm,fingerprint=digest(norm)))
        after.append(dict(id=new_id,org_id=c.org_id,version_id=versions[1].id,sku=f'P{n:06d}',raw={**changed,'name':changed['name']+' 新展示'} if n%100==2 else changed,normalized=changed,fingerprint=digest(changed)))
        if n%100!=0:links[old_id]=new_id
    for start in range(0,args.size,500):s.execute(insert(Product),before[start:start+500]);s.execute(insert(Product),after[start:start+500])
    revision=s.get(Revision,run.revision_id);release_rows=[]
    for n in range(10):
        r=BatchRelease(id=uid(),org_id=c.org_id,batch_id=revision.batch_id,number=n+1,baseline_run_id=run.id,status='SUPERSEDED',created_by=actor.id,config={},manifest={},validation={},snapshot_hash='synthetic-not-downloadable');s.add(r);s.flush()
        for index in range(n,args.size,10):release_rows.append(dict(id=uid(),org_id=c.org_id,release_id=r.id,stable_key=f'bench:{index}',source_id=source.id,item_id=item.id,product_id=old_ids[index],status='CONFIRMED',data={'scope':'synthetic reference only'}))
    for start in range(0,len(release_rows),500):s.execute(insert(ReleaseRow),release_rows[start:start+500])
    change=create_change(s,c,versions[0].id,versions[1].id,links);ident=change.id
    plan=create_plan(s,c,versions[0].id,versions[1].id);plan_id=plan.id;run_id=run.id
started=time.perf_counter();process_plan(plan_id);plan_seconds=time.perf_counter()-started
with TestClient(app) as api:
    login=api.post('/api/v1/auth/login',json={'email':'reviewer@demo.local','password':'Demo2026!match'}).json();headers={'Authorization':'Bearer '+login['access_token'],'X-Organization-ID':c.org_id}
    finished=threading.Event();samples=[];errors=[];list_samples=[]
    def background():
        try:process_change(ident)
        except Exception as e:errors.append(type(e).__name__)
        finally:finished.set()
    t=threading.Thread(target=background);start=time.perf_counter();t.start()
    while not finished.is_set():
        list_start=time.perf_counter();r=api.get('/api/v1/runs/'+run_id+'/items?details=false&limit=1',headers=headers);list_samples.append((time.perf_counter()-list_start)*1000);selected=r.json()['items'][0];tick=time.perf_counter();r=api.post('/api/v1/items/'+selected['id']+'/decisions',headers=headers,json={'action':'unmatched','expected_version':selected['version'],'reason':'规模隔离夹具：并发审核延迟采样'});samples.append({'seconds':time.perf_counter()-tick,'status':r.status_code});finished.wait(.02)
    t.join();seconds=time.perf_counter()-start
    login=api.post('/api/v1/auth/login',json={'email':'admin@demo.local','password':'Demo2026!match'}).json();headers['Authorization']='Bearer '+login['access_token'];first=api.get('/api/v1/catalog-plans/'+plan_id+'?limit=20',headers=headers)
with transaction() as s:
    job=s.get(CatalogChange,ident);total=s.scalar(select(func.count()).select_from(ImpactTask).where(ImpactTask.change_id==ident));report=job.report
    # 1% retired, 1% identity changes, 1% display changes across 10 historical releases.
    assert total==args.size*3//100 and not errors,(total,errors)
process_change(ident)
with transaction() as s:assert s.scalar(select(func.count()).select_from(ImpactTask).where(ImpactTask.change_id==ident))==total
result={'scope':'isolated synthetic SQLite, in-process FastAPI requests; 10 historical releases with total N references, not 10*N','products_per_version':args.size,'history_releases':10,'historical_reference_rows':args.size,'plan_seconds':plan_seconds,'analysis_seconds':seconds,'report':report,'impact_count':total,'first_page_rows':len(first.json()['items']),'first_page_bytes':len(first.content),'list_samples_ms':list_samples,'list_p95_ms':float(np.quantile(list_samples,.95)) if list_samples else None,'review_samples':samples,'review_p95_ms':float(np.quantile([r['seconds']*1000 for r in samples],.95)) if samples else None,'idempotent':True}
Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps({k:v for k,v in result.items() if k not in ('review_samples','report')},ensure_ascii=False))
