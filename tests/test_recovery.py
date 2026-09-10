import time
import os
import subprocess
import sys
import pytest
from uuid import uuid4
from sqlalchemy import func,select
from packages.domain.db import transaction
from packages.domain.models import Chunk,Item,Outbox,Run
from workers import dispatcher
from workers.jobs import claim_chunk,commit_chunk,fail_chunk,process_run,start_run
from tests.test_business import new_run,P


def test_outbox_retained_when_redis_unavailable(env,monkeypatch):
    c,h,run,_=env
    ident=new_run(c,h['operator'],run).json()['id']
    monkeypatch.setattr(dispatcher,'QUEUE_MODE','celery')
    from workers.tasks import execute
    def disconnected(*args,**kwargs):raise ConnectionError('simulated broker outage')
    monkeypatch.setattr(execute,'apply_async',disconnected)
    try:dispatcher.dispatch_once()
    except ConnectionError:pass
    with transaction() as s:
        event=s.scalar(select(Outbox).where(Outbox.resource_id==ident))
        assert event.published_at is None and not event.completed
    monkeypatch.setattr(dispatcher,'QUEUE_MODE','database')
    dispatcher.dispatch_once()
    assert c.get(f'{P}/runs/{ident}',headers=h['operator']).json()['status']=='SUCCEEDED'


def test_transient_retries_and_failed_run_closed(env):
    c,h,run,_=env;ident=new_run(c,h['operator'],run).json()['id'];start_run(ident)
    for attempt in range(4):
        claim=claim_chunk(ident);assert claim
        fail_chunk(claim,ConnectionError('simulated'))
        with transaction(write=True) as s:
            chunk=s.get(Chunk,claim['id']);chunk.retry_after=0
    result=c.get(f'{P}/runs/{ident}',headers=h['operator']).json()
    assert result['status']=='FAILED' and result['failed']==29
    assert claim_chunk(ident) is None
    assert commit_chunk(claim,[],0) is False


def test_stale_fence_and_expired_lease_cannot_commit(env):
    c,h,run,_=env;ident=new_run(c,h['operator'],run).json()['id'];start_run(ident);claim=claim_chunk(ident)
    with transaction(write=True) as s:s.get(Chunk,claim['id']).lease_until=time.time()-1
    assert commit_chunk(claim,[],0) is False
    process_run(ident)
    with transaction() as s:
        assert s.scalar(select(func.count()).select_from(Item).where(Item.run_id==ident))==29


def test_one_active_run_per_org(env):
    c,h,run,_=env
    first=new_run(c,h['operator'],run).json()['id'];second=new_run(c,h['operator'],run).json()['id']
    assert start_run(first)
    assert start_run(second) is None
    process_run(first)
    assert start_run(second)


def test_manifest_version_mismatch_fails_instead_of_reinterpreting(env):
    c,h,run,_=env;ident=new_run(c,h['operator'],run).json()['id']
    with transaction(write=True) as s:
        r=s.get(Run,ident);r.manifest={**r.manifest,'index_version':'unavailable-version'}
    process_run(ident)
    result=c.get(f'{P}/runs/{ident}',headers=h['operator']).json()
    assert result['status']=='FAILED' and result['processed']==0


@pytest.mark.parametrize('phase',['before_commit','after_commit'])
def test_real_worker_exit_before_and_after_commit(env,phase):
    c,h,run,_=env;ident=new_run(c,h['operator'],run).json()['id']
    script = """
import os,sys
from workers import jobs
ident,phase=sys.argv[1:]
if phase=='before_commit':
    jobs.start_run(ident);jobs.claim_chunk(ident);os._exit(21)
original=jobs.commit_chunk
def crash(*args,**kwargs):
    original(*args,**kwargs);os._exit(22)
jobs.commit_chunk=crash
jobs.process_run(ident)
"""
    process=subprocess.run([sys.executable,'-c',script,ident,phase],capture_output=True,timeout=30)
    assert process.returncode in (21,22),process.stderr.decode()
    with transaction(write=True) as s:
        for chunk in s.scalars(select(Chunk).where(Chunk.run_id==ident,Chunk.status=='RUNNING')):chunk.lease_until=time.time()-1
    process_run(ident);process_run(ident)
    with transaction() as s:
        assert s.get(Run,ident).status=='SUCCEEDED'
        assert s.scalar(select(func.count()).select_from(Item).where(Item.run_id==ident))==29
