"""Migrate a restored v1.2 backup and exercise business without create_all/drop_all."""
import os,sys,json,sqlite3,subprocess,hashlib,time
from pathlib import Path
source,destination,output=map(lambda x:Path(x).resolve(),sys.argv[1:4]);started=time.perf_counter()
os.environ['DATA_DIR']=str(destination);os.environ['DATABASE_URL']='sqlite:///'+str(destination/'app.db');os.environ['MODEL_DIR']=str(destination/'models');os.environ['DB_AUTO_CREATE']='false'
keys=json.loads((source/'recovery-keys.json').read_text()) if (source/'recovery-keys.json').exists() else {}
os.environ.update(keys)
if 'JWT_SECRET' not in keys and (source/'session.key').exists():os.environ['JWT_SECRET']=(source/'session.key').read_text()
from scripts.backup import restore
destination.rmdir()  # config creates an empty DATA_DIR; restore requires a fresh destination.
restore(source,destination)

def snapshot():
    with sqlite3.connect(destination/'app.db') as s:
        tables=[r[0] for r in s.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name!='alembic_version'")]
        result={}
        for table in tables:
            columns=[r[1] for r in s.execute('PRAGMA table_info("'+table+'")')];rows=s.execute('SELECT '+','.join('"'+c+'"' for c in columns)+' FROM "'+table+'" ORDER BY id').fetchall();result[table]={'columns':columns,'rows':rows}
        return result
before=snapshot();p=subprocess.run([sys.executable,'-m','scripts.upgrade','--backup',str(destination.parent/(destination.name+'-before-upgrade'))],capture_output=True,text=True,env=os.environ.copy());assert p.returncode==0,p.stderr+p.stdout
with sqlite3.connect(destination/'app.db') as s:
    preserved=[];reopened=0
    for table,prior in before.items():
        rows=s.execute('SELECT '+','.join('"'+c+'"' for c in prior['columns'])+' FROM "'+table+'" ORDER BY id').fetchall()
        if rows==prior['rows']:preserved.append(table);continue
        assert table=='impact_tasks'
        fields=prior['columns'];status,resolution=fields.index('status'),fields.index('resolution')
        for old,new in zip(prior['rows'],rows):
            if old!=new:
                assert old[status]=='RESOLVED' and new[status]=='OPEN';assert json.loads(new[resolution])['legacy_resolution']==json.loads(old[resolution]);reopened+=1
    assert not s.execute('PRAGMA foreign_key_check').fetchall();assert s.execute('PRAGMA integrity_check').fetchone()[0]=='ok';head=s.execute('SELECT version_num FROM alembic_version').fetchone()[0]
from packages.domain.db import Base
# Fail visibly if application startup or business routes try to rebuild schema.
def forbid(*args,**kwargs):raise AssertionError('Schema recreation forbidden during migrated business acceptance')
Base.metadata.create_all=forbid;Base.metadata.drop_all=forbid
from fastapi.testclient import TestClient
from apps.api.main import app
from uuid import uuid4
from workers.jobs import process_run
from workers.releases import process_validation,process_files
from scripts.seed import MAPPING,csv_bytes
with TestClient(app) as client:
    headers={}
    for role in ('admin','operator','reviewer'):
        r=client.post('/api/v1/auth/login',json={'email':role+'@demo.local','password':'Demo2026!match'});assert r.status_code==200,r.text;h={'Authorization':'Bearer '+r.json()['access_token']};org=client.get('/api/v1/memberships',headers=h).json()['items'][0]['org_id'];h['X-Organization-ID']=org;headers[role]=h
    def post(role,path,body=None,**kw):return client.post('/api/v1'+path,headers={**headers[role],'Idempotency-Key':str(uuid4())},json=body,**kw)
    data=csv_bytes([[f'MIGR-{n}','小米 15 12+256 黑色 国行 单机','小米','15','12','256','黑色','国行','1','1999','CNY'] for n in range(3)])
    f=client.post('/api/v1/files',headers=headers['operator'],files={'file':('migration-business.csv',data,'text/csv')});assert f.status_code==202,f.text
    from workers.imports import process_import
    process_import(f.json()['job_id'])
    batch=post('operator','/batches',{'name':'迁移后业务隔离验收','supplier':'迁移夹具','file_id':f.json()['id'],'sheet':'CSV','header_row':1,'mapping':MAPPING,'exclude_rows':[]});assert batch.status_code==201,batch.text
    cv=next(v for c in client.get('/api/v1/catalogs',headers=headers['operator']).json()['items'] for v in c['versions'] if v['status']=='PUBLISHED')
    run=post('operator','/runs',{'revision_id':batch.json()['revision']['id'],'catalog_version_id':cv['id']});assert run.status_code==202,run.text;ident=run.json()['id'];process_run(ident)
    for item in client.get('/api/v1/runs/'+ident+'/items',headers=headers['reviewer']).json()['items']:
        r=post('reviewer','/items/'+item['id']+'/decisions',{'action':'unmatched','expected_version':item['version'],'reason':'迁移结构直接执行业务回归'});assert r.status_code==201,r.text
    r=post('admin','/batches/'+batch.json()['batch']['id']+'/releases',{'baseline_run_id':ident});assert r.status_code==201,r.text;release=r.json()['id'];assert post('admin','/releases/'+release+'/validate',{}).status_code==202;process_validation(release)
    r=client.get('/api/v1/releases/'+release,headers=headers['admin']).json();assert r['status']=='READY',r
    r=post('admin','/releases/'+release+'/publish',{'expected_version':r['version']});assert r.status_code==200,r.text;process_files(release)
    downloaded=client.get('/api/v1/releases/'+release+'/artifacts/full/download',headers=headers['admin']);assert downloaded.status_code==200;assert hashlib.sha256(downloaded.content).hexdigest()==downloaded.headers['X-File-SHA256']
report={'passed':True,'scope':'restored v1.2 SQLite backup upgraded through Alembic, create_all/drop_all replaced by failing guards','head':head,'preserved_old_tables':preserved,'reopened_legacy_release_impacts':reopened,'business':['login','CSV upload','revision import','match execution','independent review','full validation','publish','artifact download/hash'],'new_source_rows':3,'seconds':time.perf_counter()-started,'production_stack':False}
output.write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps({k:v for k,v in report.items() if k!='preserved_old_tables'},ensure_ascii=False))
