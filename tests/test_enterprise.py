import csv
import io
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import pytest
from sqlalchemy import select
from packages.domain.db import transaction
from packages.domain.models import *
from workers.releases import process_validation,process_files
from workers.jobs import process_run


def setup_enterprise(env):
    client,h,run,items=env
    with transaction(write=True) as s:
        m=s.scalar(select(Membership).where(Membership.org_id==h['admin']['X-Organization-ID'],Membership.user_id.in_(select(User.id).where(User.email=='admin@demo.local'))))
        m.roles=[*m.roles,'publisher','supervisor','integration_manager','adjudicator']
    return client,h,run,items


def post(client,h,path,body=None,key=None):
    return client.post('/api/v1'+path,headers={**h,'Idempotency-Key':key or str(uuid4())},json=body or {})


def finish(client,h,run):
    items=client.get('/api/v1/runs/'+run['id']+'/items?limit=100',headers=h['reviewer']).json()['items']
    for item in items:
        if item['status']!='UNMATCHED':
            r=post(client,h['reviewer'],'/items/'+item['id']+'/decisions',{'action':'unmatched','expected_version':item['version'],'reason':'受控测试：独立核对无匹配'})
            assert r.status_code==201,r.text


def draft(client,h,run,**extra):
    body={'baseline_run_id':run['id'],**extra}
    r=post(client,h['admin'],'/batches/'+run['batch_id']+'/releases',body)
    assert r.status_code==201,r.text
    return r.json()


def validate(client,h,r):
    response=post(client,h['admin'],'/releases/'+r['id']+'/validate');assert response.status_code==202,response.text
    process_validation(r['id'])
    return client.get('/api/v1/releases/'+r['id'],headers=h['admin']).json()


def ready_release(env):
    client,h,run,items=setup_enterprise(env);finish(client,h,run)
    # Seed contains a generated identifier; explicit identity decisions are required.
    with transaction(write=True) as s:
        for src in s.scalars(select(Source).where(Source.org_id==h['operator']['X-Organization-ID'],Source.revision_id==run['revision_id'])):
            s.add(SourceIdentity(org_id=src.org_id,batch_id=run['batch_id'],source_id=src.id,stable_key='row:'+str(src.row_no),reason='受控测试人工消歧',actor_id=run['created_by']))
    r=validate(client,h,draft(client,h,run));assert r['status']=='READY',r['validation']
    return client,h,run,items,r


def test_full_release_and_immutable_artifacts(env):
    client,h,run,items,r=ready_release(env)
    key=str(uuid4());body={'expected_version':r['version']}
    first=post(client,h['admin'],'/releases/'+r['id']+'/publish',body,key);assert first.status_code==200,first.text
    again=post(client,h['admin'],'/releases/'+r['id']+'/publish',body,key);assert again.json()==first.json()
    pending=client.get('/api/v1/releases/'+r['id']+'/artifacts/full/download',headers=h['admin']);assert pending.status_code==409
    process_files(r['id']);process_files(r['id'])
    downloaded=client.get('/api/v1/releases/'+r['id']+'/artifacts/full/download',headers=h['admin']);assert downloaded.status_code==200
    rows=list(csv.DictReader(io.StringIO(downloaded.content.decode('utf-8-sig'))));assert len(rows)==run['total']+run['excluded']
    assert len({x['stable_key'] for x in rows})==len(rows)
    assert client.get('/api/v1/releases/'+r['id'],headers=h['other']).status_code==404
    assert client.put('/api/v1/releases/'+r['id'],headers=h['admin'],json={**r['config'],'expected_version':2}).status_code==409
    revocation=post(client,h['reviewer'],'/items/'+items[0]['id']+'/decisions',{'action':'revoke','reason':'测试撤销','expected_version':1});assert revocation.status_code==201,revocation.text
    assert client.get('/api/v1/releases/'+r['id'],headers=h['admin']).json()['invalidated']
    assert client.get('/api/v1/releases/'+r['id']+'/artifacts/full/download',headers=h['admin']).content==downloaded.content


def test_release_detects_decision_change_after_validation(env):
    client,h,run,items,r=ready_release(env)
    response=post(client,h['reviewer'],'/items/'+items[0]['id']+'/decisions',{'action':'revoke','reason':'校验后撤销','expected_version':1});assert response.status_code==201
    result=post(client,h['admin'],'/releases/'+r['id']+'/publish',{'expected_version':r['version']})
    assert result.status_code==409 and result.json()['code']=='RESULT_CHANGED'
    r=validate(client,h,r);assert r['status']=='DRAFT'
    assert any(p['code']=='UNRESOLVED_DECISION' for x in r['validation']['blockers'] for p in x['problems'])


def test_unreviewed_correction_blocks_and_then_merges_full(env):
    client,h,run,items,r=ready_release(env)
    src=items[0]['source'];path='/runs/'+run['id']+'/corrections/'+src['id']
    response=client.put('/api/v1'+path,headers=h['operator'],json={'fields':{'price':'100'},'evidence':'供应商授权补数文件（受控测试）','expected_version':0})
    assert response.status_code==200,response.text
    draft_id=response.json()['id']
    response=post(client,h['operator'],'/runs/'+run['id']+'/corrections/submit',{'draft_ids':[draft_id]});assert response.status_code==202,response.text
    corrected=response.json();process_run(corrected['id'])
    r=validate(client,h,r);assert r['status']=='DRAFT'
    finish(client,h,corrected)
    r=validate(client,h,r);assert r['status']=='READY',r['validation']
    assert r['manifest']['total']==run['total']+run['excluded']
    with transaction() as s:
        rows=s.scalars(select(ReleaseRow).where(ReleaseRow.release_id==r['id'])).all()
        assert sum(x.data['corrected'] for x in rows)==1
        row=next(x for x in rows if x.data['corrected']);assert row.data['run_id']==corrected['id'] and row.data['decision_id']
    newrun=client.get('/api/v1/runs/'+corrected['id'],headers=h['operator']).json()
    assert post(client,h['admin'],'/batches/'+run['batch_id']+'/releases',{'baseline_run_id':newrun['id']}).status_code==422


def test_claim_twenty_concurrent_requests_one_winner(env):
    client,h,run,items=setup_enterprise(env);item=items[0]
    with ThreadPoolExecutor(max_workers=20) as pool:responses=list(pool.map(lambda _:post(client,h['reviewer'],'/items/'+item['id']+'/claim'),range(20)))
    assert sum(r.status_code==200 for r in responses)==1,[r.status_code for r in responses]
    token=next(r.json()['claim_token'] for r in responses if r.status_code==200)
    assert post(client,h['reviewer'],'/items/'+item['id']+'/decisions',{'action':'unmatched','reason':'测试','expected_version':0}).status_code==409
    accepted=post(client,h['reviewer'],'/items/'+item['id']+'/decisions',{'action':'unmatched','reason':'测试','expected_version':0,'claim_token':token});assert accepted.status_code==201,accepted.text
    assert post(client,h['reviewer'],'/items/'+item['id']+'/claim/renew',{'claim_token':token}).status_code==409


def test_claim_expiry_fences_old_token_and_assignment_audit(env):
    client,h,run,items=setup_enterprise(env);item=items[0]
    first=post(client,h['reviewer'],'/items/'+item['id']+'/claim').json()['claim_token']
    with transaction(write=True) as s:
        claim=s.scalar(select(ReviewClaim).where(ReviewClaim.item_id==item['id']));claim.lease_until=time.time()-1
        reviewer=claim.holder_id
    second=post(client,h['reviewer'],'/items/'+item['id']+'/claim');assert second.status_code==200
    assert post(client,h['reviewer'],'/items/'+item['id']+'/claim/renew',{'claim_token':first}).status_code==409
    assigned=post(client,h['admin'],'/review-assignments',{'item_ids':[item['id']],'assignee_id':reviewer,'reason':'主管明确分派'});assert assigned.status_code==200
    assert post(client,h['reviewer'],'/items/'+item['id']+'/claim/renew',{'claim_token':second.json()['claim_token']}).status_code==409
    events=client.get('/api/v1/items/'+item['id']+'/coordination',headers=h['admin']).json()['events'];assert {'CLAIMED','EXPIRED','ASSIGNED'}<={e['kind'] for e in events}
    assert post(client,h['other'],'/items/'+item['id']+'/claim').status_code==404


def test_service_credential_scope_rotation_and_tenant(env):
    client,h,run,items=setup_enterprise(env)
    response=post(client,h['admin'],'/service-accounts',{'name':'试用采购系统','scopes':['releases:read'],'expires_in_days':1});assert response.status_code==201,response.text
    a=response.json();headers={'Authorization':'Bearer '+a['credential'],'X-Organization-ID':h['admin']['X-Organization-ID']}
    assert client.get('/api/v1/service/releases',headers=headers).status_code==200
    assert client.get('/api/v1/service/deliveries/nope',headers=headers).status_code==403
    assert client.get('/api/v1/service/releases',headers={**headers,'X-Organization-ID':h['other']['X-Organization-ID']}).status_code==404
    post(client,h['admin'],'/service-accounts/'+a['id']+'/rotate')
    assert client.get('/api/v1/service/releases',headers=headers).status_code==401


def test_quality_is_recomputable(env):
    client,h,run,items=setup_enterprise(env)
    report=client.get('/api/v1/revisions/'+run['revision_id']+'/quality?limit=100',headers=h['operator']).json()
    with transaction() as s:
        rows=s.scalars(select(Source).where(Source.revision_id==run['revision_id'])).all()
        for field,metric in report['missing_fields'].items():
            assert metric['denominator']==len(rows)
            assert metric['numerator']==sum(r.normalized.get(field) in (None,'') for r in rows)


def test_blind_two_person_annotation_and_freeze(env):
    client,h,run,items=setup_enterprise(env)
    with transaction(write=True) as s:
        for email in ('operator@demo.local','reviewer@demo.local'):
            user=s.scalar(select(User).where(User.email==email));m=s.scalar(select(Membership).where(Membership.user_id==user.id));m.roles=[*m.roles,'annotator']
    d=post(client,h['admin'],'/datasets',{'name':'受控测试','source_type':'simulation','evidence':'受控合成样本，非业务真值','entity_grouping_rule':'按已知合成商品实体分组'}).json()
    item=next(i for i in items if i['candidates'])
    task=post(client,h['admin'],'/datasets/'+d['id']+'/pairs',{'item_id':item['id'],'product_id':item['candidates'][0]['product_id'],'entity_key':'sim-entity-1'}).json()
    first=post(client,h['operator'],'/annotation-tasks/'+task['id']+'/decisions',{'label':'MATCH','reason':'独立核对合成规格','seconds':30});assert first.status_code==200,first.text
    visible=client.get('/api/v1/datasets/'+d['id']+'/tasks',headers=h['reviewer']).json()['items'][0]
    assert visible['mine'] is None and visible['label'] is None and 'score' not in visible['snapshot']
    second=post(client,h['reviewer'],'/annotation-tasks/'+task['id']+'/decisions',{'label':'INSUFFICIENT','reason':'合成来源仍需资料','seconds':40});assert second.json()['status']=='DISPUTED'
    assert post(client,h['admin'],'/datasets/'+d['id']+'/freeze').status_code==409
    final=post(client,h['admin'],'/annotation-tasks/'+task['id']+'/adjudicate',{'label':'INSUFFICIENT','reason':'保留资料不足，不作负例','seconds':20});assert final.status_code==200,final.text
    frozen=post(client,h['admin'],'/datasets/'+d['id']+'/freeze');assert frozen.status_code==200,frozen.text
    assert frozen.json()['manifest']['insufficient_info']==1
    assert post(client,h['operator'],'/annotation-tasks/'+task['id']+'/decisions',{'label':'NO_MATCH','reason':'再次修改测试','seconds':10}).status_code==409


def test_storage_and_queue_quota_idempotent_reservations(env):
    client,h,run,items=setup_enterprise(env)
    from packages.domain import storage
    from packages.domain.quotas import reserve,queue_available
    from packages.domain.errors import Problem
    org=h['admin']['X-Organization-ID']
    with transaction(write=True) as s:
        s.add(WorkflowSettings(org_id=org,storage_bytes=1048576,queue_limit=1))
    key1=storage.quota_put(org,b'synthetic quota payload','txt');key2=storage.quota_put(org,b'synthetic quota payload','txt');assert key1==key2
    with transaction() as s:
        assert len(s.scalars(select(QuotaReservation).where(QuotaReservation.org_id==org,QuotaReservation.resource_key==key1)).all())==1
    with pytest.raises(Problem) as exc:storage.quota_put(org,b'x'*1048576,'txt')
    assert exc.value.code=='STORAGE_QUOTA'
    response=post(client,h['operator'],'/runs',{'revision_id':run['revision_id'],'catalog_version_id':run['catalog_version_id']});assert response.status_code==202,response.text
    second=post(client,h['operator'],'/runs',{'revision_id':run['revision_id'],'catalog_version_id':run['catalog_version_id']});assert second.status_code==429
    post(client,h['operator'],'/runs/'+response.json()['id']+'/cancel',{'reason':'测试取消'})
    process_run(response.json()['id'])
    with transaction() as s:queue_available(s,org)


def test_delivery_retries_authenticated_receipt_and_revocation(env,monkeypatch):
    client,h,run,items,r=ready_release(env)
    monkeypatch.setenv('WEBHOOK_ALLOWED_HOSTS','127.0.0.1');monkeypatch.setenv('WEBHOOK_ALLOW_LOOPBACK','true')
    account=post(client,h['admin'],'/service-accounts',{'name':'模拟采购端','scopes':['releases:read','deliveries:read','receipts:write'],'expires_in_days':1}).json()
    integration=post(client,h['admin'],'/integrations',{'name':'模拟接收端','account_id':account['id'],'url':'http://127.0.0.1:18890/notify','active':True});assert integration.status_code==201,integration.text
    post(client,h['admin'],'/releases/'+r['id']+'/publish',{'expected_version':r['version']});process_files(r['id'])
    d=client.get('/api/v1/deliveries',headers=h['admin']).json()['items'][0]
    from workers import deliveries
    monkeypatch.setattr(deliveries,'send_notification',lambda *args:503)
    deliveries.process_delivery(d['id'])
    with transaction(write=True) as s:
        item=s.get(Delivery,d['id']);assert item.status=='RETRY';item.next_retry=0
    monkeypatch.setattr(deliveries,'send_notification',lambda *args:202)
    deliveries.process_delivery(d['id'])
    with transaction() as s:assert s.get(Delivery,d['id']).status=='RECEIVED'
    headers={'Authorization':'Bearer '+account['credential'],'X-Organization-ID':h['admin']['X-Organization-ID']}
    body={'event_id':d['event_id'],'status':'APPLIED','failed_rows':[],'message':'模拟采购端已应用'}
    for _ in range(10):
        response=post(client,headers,'/service/deliveries/'+d['id']+'/receipt',body);assert response.status_code==200,response.text
    with transaction() as s:assert s.get(Delivery,d['id']).attempts==2
    revoke=post(client,h['admin'],'/releases/'+r['id']+'/revoke',{'reason':'撤销通知验收'});assert revoke.status_code==200
    all_deliveries=client.get('/api/v1/deliveries',headers=h['admin']).json()['items'];assert len(all_deliveries)==2 and any(x['kind']=='REVOKED' for x in all_deliveries)


def test_webhook_signature_and_outbound_restrictions(monkeypatch):
    from packages.domain.integrations import signature,verify_signature,validate_url
    from packages.domain.errors import Problem
    stamp=str(int(time.time()));sig=signature('secret','event',stamp,b'{}')
    assert verify_signature('secret','event',stamp,b'{}',sig)
    assert not verify_signature('secret','event',stamp,b'{"changed":true}',sig)
    assert not verify_signature('secret','event',stamp,b'{}',sig,now_value=int(stamp)+301)
    monkeypatch.setenv('WEBHOOK_ALLOWED_HOSTS','127.0.0.1')
    with pytest.raises(Problem):validate_url('https://127.0.0.1/notify')


def test_catalog_identity_impact_and_retirement_blocks_release(env):
    client,h,run,items,r=ready_release(env)
    from packages.domain.catalog_changes import create_change,activate,release_problem_map
    from packages.domain.auth import Context
    from workers.catalog_changes import process_change
    from packages.domain.db import uid
    org=h['admin']['X-Organization-ID']
    with transaction(write=True) as s:
        admin=s.scalar(select(User).where(User.email=='admin@demo.local'));ctx=Context(org,admin.id,['admin'])
        old=s.get(CatalogVersion,run['catalog_version_id'])
        new=CatalogVersion(id=uid(),org_id=org,catalog_id=old.catalog_id,file_id=old.file_id,number=old.number+1,status='PUBLISHED',content_hash='test',row_count=1,errors=[],mapping=old.mapping);s.add(new);s.flush()
        product=s.scalar(select(Product).where(Product.version_id==old.id));newproduct=Product(id=uid(),org_id=org,version_id=new.id,sku=product.sku,raw=product.raw,normalized={**product.normalized,'storage':'999GB'},fingerprint='test');s.add(newproduct);s.flush()
        change=create_change(s,ctx,old.id,new.id,{product.id:newproduct.id});change_id=change.id;product_id=product.id
    process_change(change_id);process_change(change_id)
    with transaction(write=True) as s:
        change=s.get(CatalogChange,change_id);assert change.report['counts']['identity']==1
        activate(s,ctx,change)
        assert release_problem_map(s,ctx,[s.get(Product,product_id)])[product_id]['code']=='CATALOG_REVIEW_REQUIRED'


def test_full_release_10000_rows_with_20_corrections(env):
    """10k materialized synthetic decisions; real 20-row correction and publication resolver."""
    from sqlalchemy import insert
    from packages.domain.db import uid
    from packages.domain.auth import Context
    from packages.domain.services import create_revision,create_run
    from scripts.seed import add_file,csv_bytes,MAPPING
    client,h,original,items=setup_enterprise(env);sample=next(x for x in items if x['suggestion']=='RECOMMENDED');org=h['operator']['X-Organization-ID']
    with transaction(write=True) as s:
        operator=s.scalar(select(User).where(User.email=='operator@demo.local'));reviewer=s.scalar(select(User).where(User.email=='reviewer@demo.local'));context=Context(org,operator.id,['operator'])
        original_revision=s.get(Revision,original['revision_id']);source=sample['source'];raw=[str(source['raw'].get(original_revision.mapping.get(k),'')) for k in MAPPING]
        records=[]
        for n in range(10000):r=list(raw);r[0]=f'SCALE-{n:05d}';records.append(r)
        f=add_file(s,org,'release-scale-10000.csv',csv_bytes(records));batch=Batch(id=uid(),org_id=org,name='全量发布规模测试',supplier='受控合成来源',created_by=operator.id);s.add(batch);s.flush()
        revision=create_revision(s,context,batch,{'file_id':f.id,'sheet':'CSV','header_row':1,'mapping':MAPPING,'exclude_rows':[]});run=create_run(s,context,{'revision_id':revision.id,'catalog_version_id':original['catalog_version_id'],'policy_id':original['policy_id']})
        sources=s.scalars(select(Source).where(Source.revision_id==revision.id).order_by(Source.row_no)).all();itemrows=[];events=[];mappings=[];original_decisions={};product_id=sample['candidates'][0]['product_id']
        for source in sources:
            item_id,event_id=uid(),uid();original_decisions[source.id]=event_id
            itemrows.append(dict(id=item_id,org_id=org,run_id=run['id'],source_id=source.id,suggestion='RECOMMENDED',status='CONFIRMED',current_decision_id=event_id,version=1))
            events.append(dict(id=event_id,org_id=org,item_id=item_id,seq=1,action='confirm',product_id=product_id,reason='受控测试预置决定，不代表真人标注',actor_id=reviewer.id,selection_source='fixture'))
            mappings.append(dict(id=uid(),org_id=org,item_id=item_id,decision_id=event_id,source_id=source.id,product_id=product_id))
        for offset in range(0,10000,500):
            s.execute(insert(Item),itemrows[offset:offset+500]);s.execute(insert(ReviewEvent),events[offset:offset+500]);s.execute(insert(Mapping),mappings[offset:offset+500])
        r=s.get(Run,run['id']);r.status='SUCCEEDED';r.processed=10000
        for chunk in s.scalars(select(Chunk).where(Chunk.run_id==r.id)):chunk.status='DONE'
        s.scalar(select(Outbox).where(Outbox.event_key=='match:'+r.id)).completed=True
        first_twenty=[x.id for x in sources[:20]];batch_id=batch.id
    drafts=[]
    for ident in first_twenty:
        response=client.put('/api/v1/runs/'+run['id']+'/corrections/'+ident,headers=h['operator'],json={'fields':{'price':'1234'},'evidence':'受控规模测试：只修正报价','expected_version':0});assert response.status_code==200,response.text;drafts.append(response.json()['id'])
    correction=post(client,h['operator'],'/runs/'+run['id']+'/corrections/submit',{'draft_ids':drafts});assert correction.status_code==202,correction.text
    corrected=correction.json();process_run(corrected['id']);finish(client,h,corrected)
    release=draft(client,h,{'id':run['id'],'batch_id':batch_id});release=validate(client,h,release);assert release['status']=='READY',release['validation']
    assert release['manifest']['total']==10000
    with transaction() as s:
        resolved=s.scalars(select(ReleaseRow).where(ReleaseRow.release_id==release['id'])).all();assert len(resolved)==len({x.stable_key for x in resolved})==10000
        assert sum(x.data['corrected'] for x in resolved)==20
        assert sum(x.decision_id==original_decisions[x.source_id] for x in resolved if not x.data['corrected'])==9980
        assert all(x.data['run_id']==corrected['id'] and x.decision_id not in original_decisions.values() for x in resolved if x.data['corrected'])


def test_correction_branch_conflict_requires_explicit_leaf(env):
    from packages.domain.auth import Context
    from packages.domain.corrections import save_draft,submit_drafts
    from packages.domain.db import uid
    client,h,run,items,r=ready_release(env);source_id=items[0]['source_id'];org=h['admin']['X-Organization-ID']
    with transaction(write=True) as s:
        admin=s.scalar(select(User).where(User.email=='admin@demo.local'));m=s.scalar(select(Membership).where(Membership.user_id==admin.id));m.roles=[*m.roles,'operator'];ctx=Context(org,admin.id,['admin','operator'])
        base=s.get(Run,run['id']);source=s.get(Source,source_id);d,_=save_draft(s,ctx,base,source,{'price':'200'},'第二个受控来源补数分支',0);other=submit_drafts(s,ctx,base,[d.id])
    first=client.put('/api/v1/runs/'+run['id']+'/corrections/'+source_id,headers=h['operator'],json={'fields':{'price':'100'},'evidence':'第一个受控来源补数分支','expected_version':0}).json();corrected=post(client,h['operator'],'/runs/'+run['id']+'/corrections/submit',{'draft_ids':[first['id']]}).json()
    process_run(other['id']);process_run(corrected['id']);finish(client,h,other);finish(client,h,corrected)
    r=validate(client,h,r);assert any(p['code']=='BRANCH_CONFLICT' for b in r['validation']['blockers'] for p in b['problems'])
    with transaction() as s:leaf=s.scalar(select(Source.id).where(Source.revision_id==corrected['revision_id']))
    updated=client.put('/api/v1/releases/'+r['id'],headers=h['admin'],json={**r['config'],'branch_choices':{source_id:{'source_id':leaf,'reason':'人工核对两个补数来源，选择此分支'}},'expected_version':r['version']});assert updated.status_code==200,updated.text
    r=validate(client,h,r);assert r['status']=='READY',r['validation']


def test_full_replacement_deletion_delta_needs_scope_confirmation(env):
    from packages.domain.db import uid
    from packages.domain.auth import Context
    from packages.domain.services import create_revision,create_run
    from scripts.seed import add_file,csv_bytes,MAPPING
    from packages.domain.releases import delta_rows,frozen_rows
    client,h,old,items,r=ready_release(env)
    published=post(client,h['admin'],'/releases/'+r['id']+'/publish',{'expected_version':r['version']});assert published.status_code==200
    with transaction(write=True) as s:
        baseline=s.get(Revision,old['revision_id']);sources=s.scalars(select(Source).where(Source.revision_id==baseline.id).order_by(Source.row_no)).all()[5:];org=baseline.org_id
        actor=s.scalar(select(User).where(User.email=='operator@demo.local'));ctx=Context(org,actor.id,['operator'])
        rows=[[str(src.raw.get(baseline.mapping.get(k),'')) for k in MAPPING] for src in sources];f=add_file(s,org,'new-complete-delete-five.csv',csv_bytes(rows));batch=s.get(Batch,old['batch_id'])
        revision=create_revision(s,ctx,batch,{'file_id':f.id,'sheet':'CSV','header_row':1,'mapping':MAPPING,'exclude_rows':[n+2 for n,src in enumerate(sources) if src.excluded]})
        fresh=s.scalars(select(Source).where(Source.revision_id==revision.id).order_by(Source.row_no)).all()
        for source,parent in zip(fresh,sources):s.add(SourceIdentity(org_id=org,batch_id=batch.id,source_id=source.id,stable_key='row:'+str(parent.row_no),reason='新完整文件人工确认相同业务身份',actor_id=actor.id))
        new=create_run(s,ctx,{'revision_id':revision.id,'catalog_version_id':old['catalog_version_id'],'policy_id':old['policy_id']})
    process_run(new['id']);finish(client,h,new)
    next_release=draft(client,h,{'id':new['id'],'batch_id':old['batch_id']},base_release_id=r['id']);next_release=validate(client,h,next_release)
    assert next_release['status']=='DRAFT' and next_release['validation']['removed']==5
    assert any(p['code']=='SCOPE_CONFIRMATION_REQUIRED' for b in next_release['validation']['blockers'] for p in b['problems'])
    response=client.put('/api/v1/releases/'+next_release['id'],headers=h['admin'],json={**next_release['config'],'confirm_scope_change':True,'scope_reason':'供应商完整文件明确移除五条','expected_version':next_release['version']});assert response.status_code==200
    next_release=validate(client,h,next_release);assert next_release['status']=='READY',next_release['validation']
    with transaction() as s:
        changes=delta_rows(frozen_rows(s,s.get(BatchRelease,r['id'])),frozen_rows(s,s.get(BatchRelease,next_release['id'])))
        assert sum(c['op']=='delete' for c in changes)==5


def test_monitoring_identity_is_read_only_and_durable(env,monkeypatch):
    client,h,run,items=setup_enterprise(env);monkeypatch.setenv('METRICS_TOKEN','metrics-test-only')
    assert client.get('/api/v1/internal/metrics').status_code==403
    response=client.get('/api/v1/internal/metrics',headers={'Authorization':'Bearer metrics-test-only'})
    assert response.status_code==200 and 'product_match_outbox_pending' in response.text and 'product_match_process_peak_rss_bytes' in response.text
    assert '商品名称' not in response.text
    assert client.get('/api/v1/live').json()=={'status':'alive'}


def test_new_authorization_roles_do_not_implicitly_grant_review(env):
    client,h,run,items=setup_enterprise(env)
    assert post(client,h['admin'],'/items/'+items[0]['id']+'/claim').status_code==403
    assert post(client,h['operator'],'/items/'+items[0]['id']+'/claim').status_code==403
    r=post(client,h['admin'],'/review-assignments',{'item_ids':[items[0]['id']],'assignee_id':run['created_by'],'reason':'验证提交者自审不得绕过'})
    assert r.status_code==403


def test_concurrent_publish_one_event_and_storage_failure_retry(env,monkeypatch):
    client,h,run,items,r=ready_release(env)
    from threading import Barrier
    barrier=Barrier(8)
    def publish(_):
        barrier.wait();return post(client,h['admin'],'/releases/'+r['id']+'/publish',{'expected_version':r['version']})
    with ThreadPoolExecutor(max_workers=8) as pool:responses=list(pool.map(publish,range(8)))
    assert all(x.status_code in (200,409) for x in responses)
    with transaction() as s:
        assert len(list(s.scalars(select(ReleaseEvent).where(ReleaseEvent.release_id==r['id'],ReleaseEvent.kind=='PUBLISHED'))))==1
    from packages.domain import storage
    actual=storage.quota_put
    def fail(*args,**kwargs):raise OSError('controlled object-store unavailable')
    monkeypatch.setattr(storage,'quota_put',fail);process_files(r['id'])
    assert client.get('/api/v1/releases/'+r['id']+'/artifacts/full/download',headers=h['admin']).status_code==409
    with transaction() as s:assert not list(s.scalars(select(ReleaseEvent).where(ReleaseEvent.release_id==r['id'],ReleaseEvent.kind=='ARTIFACTS_READY')))
    monkeypatch.setattr(storage,'quota_put',actual)
    assert post(client,h['admin'],'/releases/'+r['id']+'/retry-files').status_code==202
    process_files(r['id']);content=client.get('/api/v1/releases/'+r['id']+'/artifacts/full/download',headers=h['admin']).content
    process_files(r['id']);assert client.get('/api/v1/releases/'+r['id']+'/artifacts/full/download',headers=h['admin']).content==content


def test_catalog_ten_changed_products_exact_mapping_and_release_references(env):
    client,h,run,items=setup_enterprise(env)
    from packages.domain.auth import Context
    from packages.domain.catalog_changes import create_change
    from workers.catalog_changes import process_change
    for item in client.get('/api/v1/runs/'+run['id']+'/items?limit=100',headers=h['reviewer']).json()['items']:
        body={'action':'unmatched','expected_version':0,'reason':'受控范围核对'}
        if item['suggestion']=='RECOMMENDED':
            detail=client.get('/api/v1/items/'+item['id']+'/candidates',headers=h['reviewer']).json();body.update(action='confirm',product_id=detail['candidates'][0]['product_id'])
        result=post(client,h['reviewer'],'/items/'+item['id']+'/decisions',body);assert result.status_code==201,result.text
    with transaction(write=True) as s:
        for source in s.scalars(select(Source).where(Source.revision_id==run['revision_id'])):s.add(SourceIdentity(org_id=source.org_id,batch_id=run['batch_id'],source_id=source.id,stable_key='row:'+str(source.row_no),reason='测试身份',actor_id=run['created_by']))
    release=validate(client,h,draft(client,h,run));assert release['status']=='READY'
    assert post(client,h['admin'],'/releases/'+release['id']+'/publish',{'expected_version':release['version']}).status_code==200
    with transaction(write=True) as s:
        org=h['admin']['X-Organization-ID'];admin=s.scalar(select(User).where(User.email=='admin@demo.local'));ctx=Context(org,admin.id,['admin']);old=s.get(CatalogVersion,run['catalog_version_id'])
        new=CatalogVersion(id=str(uuid4()),org_id=org,catalog_id=old.catalog_id,file_id=old.file_id,number=old.number+1,status='PUBLISHED',content_hash='controlled-ten-changes',row_count=old.row_count,errors=[],mapping=old.mapping);s.add(new);s.flush()
        links={};changed=[]
        for index,p in enumerate(s.scalars(select(Product).where(Product.version_id==old.id).order_by(Product.sku))):
            normalized={**p.normalized}
            if index<10:normalized['storage']='999GB';changed.append(p.id)
            q=Product(id=str(uuid4()),org_id=org,version_id=new.id,sku=p.sku,normalized=normalized,raw=p.raw,fingerprint='test-'+str(index));s.add(q);links[p.id]=q.id
        s.flush();change=create_change(s,ctx,old.id,new.id,links);ident=change.id
        expected_items=set(s.scalars(select(Mapping.item_id).where(Mapping.org_id==org,Mapping.product_id.in_(changed),Mapping.valid_to.is_(None))))
        expected_products=set(s.scalars(select(ReleaseRow.product_id).where(ReleaseRow.release_id==release['id'],ReleaseRow.product_id.in_(changed))))
        assert expected_items and expected_products
    process_change(ident);process_change(ident)
    with transaction() as s:
        change=s.get(CatalogChange,ident);assert change.report['counts']=={'identity':10}
        tasks=list(s.scalars(select(ImpactTask).where(ImpactTask.change_id==ident)))
        assert {t.item_id for t in tasks if t.item_id}==expected_items
        assert {t.product_id for t in tasks if t.release_id}==expected_products
        assert len(tasks)==len(expected_items)+len(expected_products)


def test_annotation_score_worker_rechecks_disabled_actor(env):
    client,h,run,items=setup_enterprise(env)
    from workers.annotation_scores import process_scores
    with transaction(write=True) as s:
        org=h['admin']['X-Organization-ID'];admin=s.scalar(select(User).where(User.email=='admin@demo.local'))
        d=DatasetVersion(id=str(uuid4()),org_id=org,name='停权评分检查',provenance={'scoring':{'evaluation_id':'absent','requested_by':admin.id,'status':'QUEUED'}});s.add(d);s.flush();ident=d.id
        m=s.scalar(select(Membership).where(Membership.org_id==org,Membership.user_id==admin.id));m.active=False
    process_scores(ident)
    with transaction() as s:assert s.get(DatasetVersion,ident).provenance['scoring']['error']=='PermissionError'
