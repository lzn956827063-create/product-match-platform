import csv
import io
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from openpyxl import load_workbook
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from packages.domain.db import transaction
from packages.domain.models import *
from scripts.seed import MAPPING, csv_bytes, sample_rows
from workers.jobs import claim_chunk, commit_chunk, process_export, process_run, start_run

P='/api/v1'


def recommended(items):
    return next(i for i in items if i['suggestion']=='RECOMMENDED')


def confirm(client,headers,item,reason='已核对'):
    return client.post(f"{P}/items/{item['id']}/decisions",headers=headers,json={'action':'confirm','product_id':item['candidates'][0]['product_id'],'expected_version':item['version'],'reason':reason})


def new_run(client,headers,run,key=None):
    return client.post(P+'/runs',headers={**headers,'Idempotency-Key':key or str(uuid4())},json={k:run[k] for k in ['revision_id','catalog_version_id','policy_id']})


def test_full_import_review_export_revoke(env):
    c,h,run,items=env
    products,sources=sample_rows()
    f=c.post(P+'/files',headers=h['operator'],files={'file':('新商品表.csv',csv_bytes(sources),'text/csv')})
    assert f.status_code==201
    config={'file_id':f.json()['id'],'sheet':'CSV','header_row':1,'mapping':MAPPING,'exclude_rows':[]}
    q=c.post(P+'/imports/validate',headers=h['operator'],json=config)
    assert q.json()['error_rows']==[31] and q.json()['valid']==29
    invalid=c.post(P+'/batches',headers=h['operator'],json={**config,'name':'测试任务','supplier':'供应商'})
    assert invalid.status_code==422 and invalid.json()['code']=='EXCLUSION_CONFIRMATION_REQUIRED'
    config['exclude_rows']=[31]
    batch=c.post(P+'/batches',headers=h['operator'],json={**config,'name':'测试任务','supplier':'供应商'}).json()
    new=new_run(c,h['operator'],{**run,'revision_id':batch['revision']['id']}).json()
    process_run(new['id'])
    item=recommended(c.get(f"{P}/runs/{new['id']}/items",headers=h['operator']).json()['items'])
    assert confirm(c,h['reviewer'],item).status_code==201
    ex=c.post(f"{P}/runs/{new['id']}/exports",headers={**h['operator'],'Idempotency-Key':str(uuid4())},json={'kind':'confirmed','format':'xlsx','include_raw':True})
    assert ex.status_code==202 and ex.json()['count']==1
    # Change after freeze, before generation; frozen output still contains original decision.
    revoke=c.post(f"{P}/items/{item['id']}/decisions",headers=h['reviewer'],json={'action':'revoke','reason':'供应商更新资料','expected_version':1})
    assert revoke.status_code==201
    process_export(ex.json()['id'])
    downloaded=c.get(f"{P}/exports/{ex.json()['id']}/download",headers=h['operator'])
    assert downloaded.status_code==200
    wb=load_workbook(io.BytesIO(downloaded.content)); rows=list(wb.active.values)
    assert len(rows)==2 and rows[1][3].startswith('000') and rows[1][9]=='已确认'
    listing=c.get(P+'/exports',headers=h['operator']).json()['items']
    assert listing[0]['stale'] is True
    ex2=c.post(f"{P}/runs/{new['id']}/exports",headers={**h['operator'],'Idempotency-Key':str(uuid4())},json={'kind':'confirmed','format':'csv'})
    assert ex2.json()['count']==0
    assert len(c.get(f"{P}/items/{item['id']}/history",headers=h['operator']).json()['items'])==2


def test_roles_self_review_and_snapshot_policy(env):
    c,h,run,items=env;i=recommended(items)
    assert confirm(c,h['operator'],i).status_code==403
    assert confirm(c,h['admin'],i).status_code==403
    with transaction(write=True) as s:
        m=s.scalar(select(Membership).where(Membership.org_id==h['operator']['X-Organization-ID'],Membership.user_id==run['created_by']))
        m.roles=['operator','reviewer','admin']
        s.get(Organization,m.org_id).dual_review=False
    denied=confirm(c,h['operator'],i)
    assert denied.status_code==403 and denied.json()['code']=='SELF_REVIEW_DENIED'
    assert c.get(f"{P}/runs/{run['id']}",headers=h['operator']).json()['manifest']['dual_review'] is True
    assert confirm(c,h['reviewer'],i).status_code==201


def test_conflict_missing_and_manual_selection(env):
    c,h,run,items=env
    conflict=next(i for i in items if i['suggestion']=='CONFLICT')
    r=confirm(c,h['reviewer'],conflict)
    assert r.status_code==409 and r.json()['code']=='SPEC_CONFLICT'
    missing=next(i for i in items if i['suggestion']=='REVIEW')
    r=confirm(c,h['reviewer'],missing)
    assert r.status_code==409 and r.json()['code']=='INSUFFICIENT_INFO'
    i=recommended(items);product=i['candidates'][0]['product_id']
    with transaction(write=True) as s:
        candidate=s.scalar(select(Candidate).where(Candidate.item_id==i['id'],Candidate.product_id==product));s.delete(candidate)
    assert confirm(c,h['reviewer'],i).status_code==201
    history=c.get(f"{P}/items/{i['id']}/history",headers=h['reviewer']).json()['items']
    assert history[0]['selection_source']=='manual_search'


def test_concurrent_decisions_and_current_mapping(env):
    c,h,run,items=env;i=recommended(items)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:confirm(c,h['reviewer'],i),range(2)))
    assert sorted(r.status_code for r in results)==[201,409]
    assert next(r for r in results if r.status_code==409).json()['details']['version']==1
    with transaction() as s:
        assert s.scalar(select(func.count()).select_from(Mapping).where(Mapping.item_id==i['id'],Mapping.valid_to.is_(None)))==1
        assert s.scalar(select(func.count()).select_from(ReviewEvent).where(ReviewEvent.item_id==i['id']))==1


def test_org_isolation_every_entry_and_fk(env):
    c,h,run,items=env;i=recommended(items)
    ex=c.post(f"{P}/runs/{run['id']}/exports",headers={**h['operator'],'Idempotency-Key':str(uuid4())},json={'kind':'all','format':'csv'}).json();process_export(ex['id'])
    paths=[f"/runs/{run['id']}",f"/items/{i['id']}/candidates",f"/items/{i['id']}/history",f"/catalog-versions/{run['catalog_version_id']}/products",f"/exports/{ex['id']}/download",f"/revisions/{run['revision_id']}/preview"]
    for path in paths:
        r=c.get(P+path,headers=h['other']);assert r.status_code==404,(path,r.text)
    # Reviewer role in another org must also receive 404, not data-dependent details.
    with transaction(write=True) as s:
        m=s.scalar(select(Membership).where(Membership.org_id==h['other']['X-Organization-ID'],Membership.roles.is_not(None)));m.roles=['admin','reviewer']
    other_item_result=confirm(c,h['other'],i)
    assert other_item_result.status_code in (403,404)
    result=c.post(P+'/runs',headers={**h['other'],'Idempotency-Key':str(uuid4())},json={k:run[k] for k in ['revision_id','catalog_version_id','policy_id']})
    assert result.status_code==404
    with pytest.raises(IntegrityError),transaction(write=True) as s:
        s.add(Product(org_id=h['other']['X-Organization-ID'],version_id=run['catalog_version_id'],sku='leak',raw={},normalized={},fingerprint='x'))
        s.flush()


def test_disabled_member_immediately_denied(env):
    c,h,run,items=env
    listing=c.get(P+'/members',headers=h['admin']).json()['items'];m=next(x for x in listing if x['email']=='reviewer@demo.local')
    assert c.put(P+'/members/'+m['id'],headers=h['admin'],json={'roles':['reviewer'],'active':False}).status_code==200
    assert c.get(P+'/runs',headers=h['reviewer']).status_code==404
    assert confirm(c,h['reviewer'],recommended(items)).status_code==404


def test_idempotency_and_bulk_partial_results(env):
    c,h,run,items=env;k=str(uuid4())
    a=new_run(c,h['operator'],run,k);b=new_run(c,h['operator'],run,k)
    assert a.status_code==202 and a.json()==b.json()
    changed=c.post(P+'/runs',headers={**h['operator'],'Idempotency-Key':k},json={'revision_id':'different','catalog_version_id':run['catalog_version_id'],'policy_id':run['policy_id']})
    assert changed.status_code==409
    good=recommended(items);bad=next(i for i in items if i['suggestion']=='CONFLICT')
    body={'items':[{'item_id':i['id'],'expected_version':0,'action':'confirm','product_id':i['candidates'][0]['product_id'],'reason':'批量核对'} for i in (good,bad)]}
    key=str(uuid4());path=f"{P}/runs/{run['id']}/bulk-decisions";headers={**h['reviewer'],'Idempotency-Key':key}
    a=c.post(path,headers=headers,json=body);b=c.post(path,headers=headers,json=body)
    assert a.status_code==200 and a.json()==b.json()
    assert a.json()['succeeded']==1 and a.json()['failed']==1


def test_duplicate_job_and_crash_recovery(env):
    c,h,run,items=env
    new=new_run(c,h['operator'],run).json()['id'];assert start_run(new)
    old=claim_chunk(new)
    with transaction(write=True) as s:s.get(Chunk,old['id']).lease_until=time.time()-1
    fresh=claim_chunk(new);assert fresh['token']>old['token']
    assert commit_chunk(old,[],0) is False
    with transaction(write=True) as s:s.get(Chunk,fresh['id']).lease_until=time.time()-1
    process_run(new);process_run(new) # crash before result / repeated delivery after commit
    final=c.get(f'{P}/runs/{new}',headers=h['operator']).json()
    assert final['status']=='SUCCEEDED' and final['processed']==29
    with transaction() as s:assert s.scalar(select(func.count()).select_from(Item).where(Item.run_id==new))==29


def test_cancel_fences_worker_and_retry_new_run(env):
    c,h,run,items=env
    new=new_run(c,h['operator'],run).json()['id'];start_run(new);claim=claim_chunk(new)
    assert c.post(f'{P}/runs/{new}/cancel',headers=h['operator'],json={'reason':'取消测试'}).status_code==202
    assert commit_chunk(claim,[],0) is False
    process_run(new)
    assert c.get(f'{P}/runs/{new}',headers=h['operator']).json()['status']=='CANCELLED'
    export=c.post(f'{P}/runs/{new}/exports',headers={**h['operator'],'Idempotency-Key':str(uuid4())},json={'kind':'all','format':'csv'})
    assert export.status_code==409
    retry=c.post(f'{P}/runs/{new}/retry',headers={**h['operator'],'Idempotency-Key':str(uuid4())})
    assert retry.status_code==202 and retry.json()['id']!=new
    assert c.get(f"{P}/runs/{retry.json()['id']}",headers=h['operator']).json()['retry_of']==new


def test_new_revision_and_catalog_do_not_change_old_run(env):
    c,h,run,items=env
    original=c.get(f"{P}/runs/{run['id']}",headers=h['operator']).json()['manifest']
    rows,_=sample_rows();rows[0][5]='512';rows[0][1]='修正商品'
    f=c.post(P+'/files',headers=h['operator'],files={'file':('修正.csv',csv_bytes(rows),'text/csv')}).json()
    body={'file_id':f['id'],'sheet':'CSV','header_row':1,'mapping':MAPPING,'exclude_rows':[]}
    revision=c.post(f"{P}/batches/{run['batch_id']}/revisions",headers=h['operator'],json=body)
    assert revision.status_code==202 and revision.json()['number']==2
    new=new_run(c,h['operator'],{**run,'revision_id':revision.json()['id']}).json()
    process_run(new['id'])
    assert c.get(f"{P}/runs/{run['id']}",headers=h['operator']).json()['manifest']==original
    assert c.get(f"{P}/runs/{run['id']}/compare/{new['id']}",headers=h['operator']).json()['changes']


def test_export_safe_text_and_complete_status(env):
    c,h,run,items=env
    i=recommended(items)
    assert c.post(f"{P}/items/{i['id']}/decisions",headers=h['reviewer'],json={'action':'needs_info','reason':'=HYPERLINK("evil")','expected_version':0}).status_code==201
    e=c.post(f"{P}/runs/{run['id']}/exports",headers={**h['operator'],'Idempotency-Key':str(uuid4())},json={'kind':'all','format':'csv'}).json();process_export(e['id'])
    content=c.get(f"{P}/exports/{e['id']}/download",headers=h['operator']).content.decode('utf-8-sig')
    rows=list(csv.DictReader(io.StringIO(content)))
    assert len(rows)==29
    row=next(r for r in rows if r['来源编号']==i['source']['sku'])
    assert row['审核原因'].startswith("'=") and row['审核状态']=='待补充'
    assert any(r['审核状态']=='未处理' for r in rows)


def test_refresh_csrf_logout_and_rate_limit(env):
    c,h,run,items=env
    assert c.post(P+'/auth/refresh').status_code==403
    response=c.post(P+'/auth/refresh',headers={'X-CSRF-Token':c.cookies['csrf_token']})
    assert response.status_code==200
    token=response.json()['access_token']
    assert c.post(P+'/auth/logout',headers={'X-CSRF-Token':c.cookies['csrf_token']}).status_code==200
    assert c.get(P+'/memberships',headers={'Authorization':'Bearer '+token}).status_code==401
    assert c.post(P+'/auth/login',headers={'Origin':'https://evil.example'},json={'email':'a','password':'x'}).status_code==403
    for _ in range(10):assert c.post(P+'/auth/login',json={'email':'missing@example.com','password':'bad'}).status_code==401
    assert c.post(P+'/auth/login',json={'email':'missing@example.com','password':'bad'}).status_code==429
