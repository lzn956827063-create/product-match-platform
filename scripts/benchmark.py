"""Reproducible synthetic workload, separately reporting algorithm and full workflow cost."""
import argparse
import csv
import io
import json
import os
import platform
import resource
import tempfile
import time
from pathlib import Path

p=argparse.ArgumentParser();p.add_argument('--queries',type=int,default=1000);p.add_argument('--catalog',type=int,default=10000);p.add_argument('--output',default='docs/benchmark-1000x10000.json');p.add_argument('--algorithm-only',action='store_true');p.add_argument('--verify-cancel',action='store_true');args=p.parse_args()
directory=Path(tempfile.mkdtemp(prefix='product-match-benchmark-'));os.environ['DATA_DIR']=str(directory);os.environ['DATABASE_URL']='sqlite:///'+str(directory/'benchmark.db')
from packages.domain.db import initialize,transaction,uid
from packages.domain.auth import Context,password_hash
from packages.domain.models import Organization,User,Membership,Policy,Catalog,Batch,File,Run
from packages.domain.services import create_catalog_version,create_revision,create_run
from packages.domain import storage
from packages.matching.engine import DEFAULT_POLICY,Matcher
from packages.matching.normalize import digest,normalize
from scripts.seed import HEADERS,MAPPING,csv_bytes
from workers.jobs import process_run


def rows(n):
    return [[f'{i:06d}',f'A牌 M{i:05d} 8+128 黑色 国行 单机','A牌',f'm{i:05d}','8','128','黑色','国行','1','1999','CNY'] for i in range(n)]


started=time.perf_counter();standard=rows(args.catalog);source=rows(args.queries)
for r in source:r[1]=r[1].replace('单机','原封 单台')
if args.algorithm_only:
    products=[{'id':r[0],'normalized':normalize(dict(zip(MAPPING,r)))} for r in standard]
    queries=[normalize(dict(zip(MAPPING,r))) for r in source]
    normalize_seconds=time.perf_counter()-started;t=time.perf_counter();matcher=Matcher(products);index_seconds=time.perf_counter()-t
    t=time.perf_counter();states={};correct=0
    for idx,q in enumerate(queries):
        state,candidates=matcher.match(q);states[state]=states.get(state,0)+1
        correct+=bool(candidates and candidates[0]['product_id']==source[idx][0])
    result={'scope':'normalization, cold index and retrieval/rules only; excludes API, database writes, files and queue','normalization_seconds':normalize_seconds,'index_seconds':index_seconds,'matching_seconds':time.perf_counter()-t,'states':states,'top1_correct':correct}
else:
    initialize()
    with transaction(write=True) as s:
        org=Organization(id=uid(),name='Synthetic benchmark');user=User(id=uid(),email='bench@local',name='Benchmark',password_hash=password_hash('benchmark-only'))
        s.add_all([org,user]);s.flush();policy=Policy(id=uid(),org_id=org.id,name='Benchmark baseline',config=DEFAULT_POLICY);s.add(policy);org.default_policy_id=policy.id;s.flush()
        ctx=Context(org.id,user.id,['admin','operator']);s.add(Membership(org_id=org.id,user_id=user.id,roles=ctx.roles));s.flush()
        def file(name,records):
            content=csv_bytes(records);f=File(id=uid(),org_id=org.id,name=name,object_key=storage.put(org.id,content,'csv'),sha256=digest(content),size=len(content),encoding='utf-8',sheets=['CSV']);s.add(f);s.flush();return f
        cat=Catalog(id=uid(),org_id=org.id,name='Benchmark');s.add(cat);s.flush();f=file('catalog.csv',standard)
        cv=create_catalog_version(s,ctx,cat,{'file_id':f.id,'sheet':'CSV','header_row':1,'mapping':MAPPING});cv.status='PUBLISHED'
        batch=Batch(id=uid(),org_id=org.id,name='Benchmark',supplier='Synthetic',created_by=user.id);s.add(batch);s.flush();f=file('source.csv',source)
        rev=create_revision(s,ctx,batch,{'file_id':f.id,'sheet':'CSV','header_row':1,'mapping':MAPPING,'exclude_rows':[]})
        run=create_run(s,ctx,{'revision_id':rev.id,'catalog_version_id':cv.id,'policy_id':policy.id})
    prep=time.perf_counter()-started;t=time.perf_counter();process_run(run['id'])
    with transaction() as s:r=s.get(Run,run['id']);result={'scope':'CSV generation, storage, parsing, normalization, SQLite ingestion, run creation, cold index, matching and candidate persistence; excludes network upload','preparation_seconds':prep,'worker_seconds':time.perf_counter()-t,'status':r.status,'processed':r.processed,'failed':r.failed,'timings':r.timings}
    assert result['status']=='SUCCEEDED' and result['processed']==args.queries,result
result.update({'queries':args.queries,'catalog':args.catalog,'total_seconds':time.perf_counter()-started,'peak_rss_mb':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/(1024*1024 if platform.system()=='Darwin' else 1024),'platform':platform.platform(),'cpu_count':os.cpu_count(),'python':platform.python_version(),'dataset':'Controlled synthetic smartphone records, complete exact identity fields; not a quality benchmark or enterprise workload','isolated_database':str(directory)})
if args.verify_cancel and not args.algorithm_only:
    import threading
    with transaction(write=True) as s:
        cancelled=create_run(s,ctx,{'revision_id':rev.id,'catalog_version_id':cv.id,'policy_id':policy.id})
    thread=threading.Thread(target=process_run,args=(cancelled['id'],));thread.start()
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        with transaction() as s:r=s.get(Run,cancelled['id']);ready=r.processed>=200
        if ready:break
        time.sleep(.05)
    cancel_started=time.perf_counter()
    with transaction(write=True) as s:
        r=s.get(Run,cancelled['id']);r.status='CANCEL_REQUESTED';r.cancel_reason='Controlled capacity cancellation probe'
    thread.join(timeout=30)
    with transaction() as s:r=s.get(Run,cancelled['id']);result['cancellation']={'seconds':time.perf_counter()-cancel_started,'status':r.status,'processed_at_stop':r.processed,'worker_stopped':not thread.is_alive()}
    assert result['cancellation']['status']=='CANCELLED' and not thread.is_alive()
Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False,indent=2))
