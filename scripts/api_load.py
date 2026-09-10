"""10 concurrent local API users; creates an isolated synthetic test database."""
import argparse
import json
import os
import platform
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import numpy as np

p=argparse.ArgumentParser();p.add_argument('--output',default='docs/api-load.json');a=p.parse_args()
folder=Path(tempfile.mkdtemp(prefix='product-match-api-load-'));os.environ['DATA_DIR']=str(folder);os.environ['DATABASE_URL']='sqlite:///'+str(folder/'app.db')
from fastapi.testclient import TestClient
from apps.api.main import app
from scripts.seed import main as seed
seed()

with TestClient(app) as c:
    def login(role):
        r=c.post('/api/v1/auth/login',json={'email':role+'@demo.local','password':'Demo2026!match'}).json()
        h={'Authorization':'Bearer '+r['access_token']};h['X-Organization-ID']=c.get('/api/v1/memberships',headers=h).json()['items'][0]['org_id'];return h
    headers=login('reviewer');run=c.get('/api/v1/runs',headers=headers).json()['items'][0]
    items=c.get('/api/v1/runs/'+run['id']+'/items',headers=headers).json()['items'];good=[i for i in items if i['suggestion']=='RECOMMENDED'][:10]
    def lists(_):
        t=time.perf_counter();r=c.get(f"/api/v1/runs/{run['id']}/items",headers=headers);return time.perf_counter()-t,r.status_code
    with ThreadPoolExecutor(max_workers=10) as pool:reads=list(pool.map(lists,range(100)))
    def review(i):
        t=time.perf_counter();r=c.post('/api/v1/items/'+i['id']+'/decisions',headers=headers,json={'action':'confirm','product_id':i['candidates'][0]['product_id'],'expected_version':0,'reason':'模拟并发负载测试'});return time.perf_counter()-t,r.status_code
    with ThreadPoolExecutor(max_workers=10) as pool:writes=list(pool.map(review,good))
    def summarize(rows):return {'n':len(rows),'p50_ms':float(np.quantile([r[0] for r in rows],.5)*1000),'p95_ms':float(np.quantile([r[0] for r in rows],.95)*1000),'max_ms':max(r[0] for r in rows)*1000,'status_counts':{str(code):sum(r[1]==code for r in rows) for code in set(r[1] for r in rows)}}
    result={'scope':'In-process ASGI TestClient over SQLite, 10 threads; 29-item demo run. Excludes network and PostgreSQL. 100 list requests, 10 independent review writes.','platform':platform.platform(),'lists':summarize(reads),'reviews':summarize(writes)}
Path(a.output).write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False,indent=2))
