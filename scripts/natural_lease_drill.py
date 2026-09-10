"""SIGKILL a real worker after claiming; recover after the unmodified 120s lease."""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main(folder, output):
    folder=Path(folder).resolve();folder.mkdir(parents=True,exist_ok=True)
    os.environ['DATA_DIR']=str(folder);os.environ['DATABASE_URL']='sqlite:///'+str(folder/'app.db')
    from scripts.seed import main as seed
    from packages.domain.db import transaction
    from packages.domain.auth import Context
    from packages.domain.models import Run,Item,Chunk,Candidate
    from packages.domain.services import create_run
    from workers.jobs import process_run,commit_chunk
    from sqlalchemy import select,func
    seed()
    with transaction(write=True) as s:
        previous=s.scalar(select(Run).where(Run.status=='SUCCEEDED'));c=Context(previous.org_id,previous.created_by,['operator'])
        ident=create_run(s,c,{'revision_id':previous.revision_id,'catalog_version_id':previous.catalog_version_id,'policy_id':previous.policy_id})['id']
    marker=folder/'claimed.json'
    worker_code='''import json,sys,time
from pathlib import Path
from workers import jobs
original=jobs.claim_chunk
def claim(ident):
    result=original(ident)
    if result:
        Path(sys.argv[2]).write_text(json.dumps(result))
        while True:time.sleep(1)
    return result
jobs.claim_chunk=claim
jobs.process_run(sys.argv[1])
'''
    worker=subprocess.Popen([sys.executable,'-c',worker_code,ident,str(marker)],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    try:
        for _ in range(300):
            if marker.exists():break
            if worker.poll() is not None:raise RuntimeError(worker.stderr.read().decode())
            time.sleep(.1)
        else:raise RuntimeError('Worker did not claim')
        claim=json.loads(marker.read_text())
        with transaction() as s:expires=s.get(Chunk,claim['id']).lease_until
        killed_at=time.time();worker.kill();worker.wait(timeout=10)
        timeline=[{'event':'worker_sigkill','time':killed_at,'pid':worker.pid,'returncode':worker.returncode},{'event':'natural_lease_expiry','time':expires}]
        recovered=None
        # Observe ordinary recovery polling; do not change lease_until or fencing state.
        while time.time()-killed_at<190:
            process_run(ident)
            with transaction() as s:
                run=s.get(Run,ident);chunk=s.get(Chunk,claim['id']);state=run.status;token=chunk.fence_token
            if token>claim['token'] and recovered is None:recovered=time.time();timeline.append({'event':'recovery_observed','time':recovered,'token':token})
            if state=='SUCCEEDED':break
            time.sleep(2)
        assert state=='SUCCEEDED' and recovered is not None
        assert commit_chunk(claim,[],0) is False
        process_run(ident)
        with transaction() as s:
            count=s.scalar(select(func.count()).select_from(Item).where(Item.run_id==ident));distinct=s.scalar(select(func.count(func.distinct(Item.source_id))).where(Item.run_id==ident));run=s.get(Run,ident)
            result={'scope':'Local SQLite database dispatcher semantics, actual SIGKILL and natural 120-second lease; not Celery/Redis/PostgreSQL evidence','timeline':timeline,'lease_was_manually_modified':False,'recovery_observed_seconds_after_kill':recovered-killed_at,'target_seconds':180,'status':run.status,'total':run.total,'items':count,'unique_sources':distinct,'old_fence_rejected':True,'duplicate_delivery_no_duplicate_items':count==distinct==run.total}
        assert count==distinct==run.total and recovered-killed_at<=180
        Path(output).write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))
    finally:
        if worker.poll() is None:worker.kill();worker.wait()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--data',default='../../work/natural-lease-v11');p.add_argument('--output',default='docs/optimization/natural-lease.json');a=p.parse_args();main(a.data,a.output)
