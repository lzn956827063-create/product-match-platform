"""Provision an isolated Docker acceptance project and record a real broker interruption.

Not executed by application startup. Requires Docker and an explicit CLI invocation.
Never stops another project or deletes volumes; use the generated command to clean up.
"""
import argparse
import json
import os
import secrets
import subprocess
import time
from pathlib import Path
import httpx


def main(folder,port):
    folder=Path(folder).resolve();folder.mkdir(parents=True,exist_ok=True)
    envfile=folder/'acceptance.env'
    if not envfile.exists():
        content='\n'.join([f'POSTGRES_PASSWORD={secrets.token_hex(24)}',f'JWT_SECRET={secrets.token_hex(48)}',f'S3_ACCESS_KEY={secrets.token_hex(12)}',f'S3_SECRET_KEY={secrets.token_hex(24)}',f'WEB_PORT={port}',f'ALLOWED_ORIGINS=http://127.0.0.1:{port},http://localhost:{port}'])+'\n'
        fd=os.open(envfile,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:f.write(content)
    project='product-match-acceptance'
    command=['docker','compose','--env-file',str(envfile),'-p',project,'-f','compose.yaml','-f','deploy/compose.acceptance.yaml']
    def compose(*args):
        r=subprocess.run(command+list(args),capture_output=True,text=True)
        (folder/'commands.log').open('a').write(' '.join(args)+'\n'+r.stdout+'\n'+r.stderr+'\n')
        r.check_returncode();return r.stdout
    subprocess.run(['docker','info'],check=True,stdout=subprocess.DEVNULL)
    compose('up','-d','--build')
    base=f'http://127.0.0.1:{port}/api/v1'
    for _ in range(180):
        try:
            if httpx.get(base+'/readiness',timeout=2).status_code==200:break
        except httpx.HTTPError:pass
        time.sleep(2)
    else:raise RuntimeError('Stack readiness timeout; inspect commands.log')
    compose('exec','-T','api','python','-m','scripts.seed')
    timeline=[]
    with httpx.Client(base_url=base,timeout=30) as c:
        r=c.post('/auth/login',json={'email':'operator@demo.local','password':'Demo2026!match'});r.raise_for_status();c.headers['Authorization']='Bearer '+r.json()['access_token'];c.headers['X-Organization-ID']=c.get('/memberships').json()['items'][0]['org_id']
        original=c.get('/runs').json()['items'][0]
        compose('stop','redis');timeline.append({'event':'redis_stopped','at':time.time()})
        try:
            r=c.post('/runs',headers={'Idempotency-Key':secrets.token_hex(12)},json={k:original[k] for k in ('revision_id','catalog_version_id','policy_id')});r.raise_for_status();ident=r.json()['id']
            timeline.append({'event':'run_accepted_during_broker_outage','at':time.time(),'status':r.status_code,'run_id':ident})
            time.sleep(5);assert c.get('/runs/'+ident).json()['status']=='QUEUED'
        finally:compose('start','redis');timeline.append({'event':'redis_started','at':time.time()})
        for _ in range(120):
            result=c.get('/runs/'+ident).json()
            if result['status']=='SUCCEEDED':break
            time.sleep(2)
        else:raise RuntimeError('Run not recovered after broker restoration')
        timeline.append({'event':'run_succeeded','at':time.time(),'processed':result['processed']})
        ex=c.post('/runs/'+ident+'/exports',headers={'Idempotency-Key':secrets.token_hex(12)},json={'kind':'all','format':'csv'});ex.raise_for_status();eid=ex.json()['id']
        for _ in range(120):
            download=c.get('/exports/'+eid+'/download')
            if download.status_code==200:break
            time.sleep(1)
        else:raise RuntimeError('Export timeout')
        from packages.matching.normalize import digest
        report={'scope':'Actual dedicated PostgreSQL/Redis/Celery/MinIO/Nginx integration when this script completes','timeline':timeline,'export_bytes':len(download.content),'export_sha256':digest(download.content),'compose_services':compose('ps','--format','json'),'docker_version':subprocess.check_output(['docker','version','--format','{{.Server.Version}}'],text=True).strip(),'remaining_drills':['Real Celery worker SIGKILL with natural 120s lease','Database and S3 interruption/recovery','Two concurrent 10k x 50k workers under resource limits'],'cleanup_command':' '.join(command+['down'])}
    (folder/'stack-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));compose('logs','--no-color','--tail','500','api','worker','dispatcher');print(json.dumps({'result':str(folder/'stack-result.json'),'completed':True}))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',default='../../work/stack-acceptance');p.add_argument('--port',type=int,default=18800);a=p.parse_args();main(a.output,a.port)
