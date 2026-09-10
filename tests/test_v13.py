"""Adversarial workflow regressions on isolated, explicitly synthetic business fixtures."""
import csv,io,time,json
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import select,func,delete,event
from tests.test_enterprise import setup_enterprise,ready_release,post,validate,draft
from packages.domain.db import transaction,uid,engine
from packages.domain.models import *
from packages.domain.auth import Context
from packages.domain.catalog_changes import create_change
from packages.domain.releases import touch_batch
from workers.releases import process_files
from workers.catalog_changes import process_change
from workers.catalog_plans import process_plan
from workers.quality import process_quality
from packages.matching.normalize import digest


def impact_fixture(env):
    client,h,run,items,release=ready_release(env);org=h['admin']['X-Organization-ID']
    with transaction(write=True) as s:
        actor=s.scalar(select(User).where(User.email=='reviewer@demo.local'));admin=s.scalar(select(User).where(User.email=='admin@demo.local'))
        product=s.scalar(select(Product).where(Product.version_id==run['catalog_version_id']))
        chosen=list(s.scalars(select(Item).where(Item.run_id==run['id']).order_by(Item.id).limit(2)))
        for item in chosen:
            e=ReviewEvent(id=uid(),org_id=org,item_id=item.id,seq=2,action='confirm',product_id=product.id,reason='隔离测试人工决定夹具',actor_id=actor.id,selection_source='fixture');s.add(e);s.flush();item.status='CONFIRMED';item.current_decision_id=e.id;item.version=2;s.add(Mapping(org_id=org,item_id=item.id,source_id=item.source_id,decision_id=e.id,product_id=product.id))
        touch_batch(s,org,run['batch_id']);product_id=product.id;chosen_ids=[x.id for x in chosen]
    release=validate(client,h,release);assert release['status']=='READY',release
    assert post(client,h['admin'],'/releases/'+release['id']+'/publish',{'expected_version':release['version']}).status_code==200
    process_files(release['id']);original_bytes=client.get('/api/v1/releases/'+release['id']+'/artifacts/full/download',headers=h['admin']).content
    with transaction(write=True) as s:
        before=s.get(CatalogVersion,run['catalog_version_id']);after=CatalogVersion(id=uid(),org_id=org,catalog_id=before.catalog_id,file_id=before.file_id,number=before.number+1,status='PUBLISHED',content_hash='v13-test',row_count=before.row_count,errors=[],mapping=before.mapping);s.add(after);s.flush();links={}
        for p in s.scalars(select(Product).where(Product.version_id==before.id)):
            norm={**p.normalized,'storage':'999GB'} if p.id==product_id else p.normalized
            new=Product(id=uid(),org_id=org,version_id=after.id,sku=p.sku,raw=p.raw,normalized=norm,fingerprint=digest(norm));s.add(new);links[p.id]=new.id
        s.flush();change=create_change(s,Context(org,admin.id,['admin']),before.id,after.id,links);change_id=change.id
        newrun=Run(id=uid(),org_id=org,revision_id=run['revision_id'],catalog_version_id=after.id,policy_id=run['policy_id'],created_by=run['created_by'],manifest=run['manifest'],status='SUCCEEDED',total=run['total'],excluded=run['excluded'],processed=run['total']);s.add(newrun);s.flush()
        for old in s.scalars(select(Item).where(Item.run_id==run['id'])):
            item=Item(id=uid(),org_id=org,run_id=newrun.id,source_id=old.source_id,suggestion='REVIEW',status='UNMATCHED',version=1);s.add(item);s.flush()
            e=ReviewEvent(id=uid(),org_id=org,item_id=item.id,seq=1,action='unmatched',reason='新库复核：受控测试无匹配',actor_id=actor.id,selection_source='fixture');s.add(e);s.flush();item.current_decision_id=e.id
        new_run={**run,'id':newrun.id,'catalog_version_id':after.id};touch_batch(s,org,run['batch_id'])
    process_change(change_id);process_change(change_id)
    tasks=client.get('/api/v1/impact-tasks?limit=100',headers=h['admin']).json()['items'];task=next(x for x in tasks if x['release_id']==release['id'])
    return client,h,run,new_run,release,task,original_bytes


def publish_new(client,h,new_run,old):
    r=validate(client,h,draft(client,h,new_run,base_release_id=old['id']));assert r['status']=='READY',r['validation']
    response=post(client,h['admin'],'/releases/'+r['id']+'/publish',{'expected_version':r['version']});assert response.status_code==200,response.text
    process_files(r['id']);return client.get('/api/v1/releases/'+r['id'],headers=h['admin']).json()


def test_release_impact_cannot_close_from_arbitrary_review(env):
    client,h,run,new,old,task,_=impact_fixture(env)
    candidate=client.get('/api/v1/runs/'+new['id']+'/items',headers=h['reviewer']).json()['items'][0]
    r=post(client,h['admin'],'/impact-tasks/'+task['id']+'/resolve',{'reason':'回归错误关闭','replacement_item_id':candidate['id']})
    assert r.status_code==422 and r.json()['code']=='RELEASE_DISPOSITION'
    assert post(client,h['reviewer'],'/impact-tasks/'+task['id']+'/resolve',{'reason':'无发布权限'}).status_code==403
    assert client.get('/api/v1/impact-tasks/'+task['id']+'/context',headers=h['other']).status_code==404


def test_release_disposition_all_sources_ready_current_and_idempotent(env):
    client,h,run,new,old,task,original=impact_fixture(env);replacement=publish_new(client,h,new,old)
    body={'reason':'新库复核全部来源后替代发布','replacement_release_id':replacement['id'],'expected_version':0}
    with transaction(write=True) as s:
        row=s.scalar(select(ReleaseRow).where(ReleaseRow.release_id==replacement['id'],ReleaseRow.stable_key.in_(select(ReleaseRow.stable_key).where(ReleaseRow.release_id==old['id'],ReleaseRow.product_id==task['product_id']))));item=s.get(Item,row.item_id);saved=item.status;item.status='PENDING';item_id=item.id
    bad=post(client,h['admin'],'/impact-tasks/'+task['id']+'/preflight',body).json();assert not bad['ready'] and bad['problems'][0]['code']=='REVIEW_REQUIRED'
    with transaction(write=True) as s:s.get(Item,item_id).status=saved
    pre=post(client,h['admin'],'/impact-tasks/'+task['id']+'/preflight',body).json();assert pre['ready'] and len(pre['evidence']['affected_keys'])==2
    with ThreadPoolExecutor(2) as pool:responses=list(pool.map(lambda _:post(client,h['admin'],'/impact-tasks/'+task['id']+'/resolve',body),range(2)))
    assert all(r.status_code==200 for r in responses),[r.text for r in responses]
    assert responses[0].json()['status']=='LOCAL_RESOLVED'
    with transaction() as s:assert s.scalar(select(func.count()).select_from(Audit).where(Audit.action=='impact.resolve',Audit.resource_id==task['id']))==1
    assert client.get('/api/v1/releases/'+old['id']+'/artifacts/full/download',headers=h['admin']).content==original


def test_record_impact_rejects_unrelated_source_and_catalog(env):
    client,h,run,new,old,task,_=impact_fixture(env)
    records=[t for t in client.get('/api/v1/impact-tasks',headers=h['reviewer']).json()['items'] if t['item_id']]
    with transaction() as s:
        prior=s.get(Item,records[0]['item_id']);valid=s.scalar(select(Item).where(Item.run_id==new['id'],Item.source_id==prior.source_id));wrong=s.scalar(select(Item).where(Item.run_id==new['id'],Item.source_id!=prior.source_id));valid_id,wrong_id=valid.id,wrong.id
    path='/impact-tasks/'+records[0]['id']+'/resolve'
    r=post(client,h['reviewer'],path,{'reason':'错误来源不应关闭','replacement_item_id':wrong_id});assert r.status_code==422 and r.json()['code']=='REPLACEMENT_SOURCE'
    r=post(client,h['reviewer'],path,{'reason':'旧库不应关闭','replacement_item_id':prior.id});assert r.status_code==409 and r.json()['code']=='REVIEW_REQUIRED'
    assert post(client,h['reviewer'],path,{'reason':'同一来源在新库下复核','replacement_item_id':valid_id}).status_code==200


def integration_fixture(env,monkeypatch):
    client,h,run,items,r=ready_release(env)
    monkeypatch.setenv('WEBHOOK_ALLOWED_HOSTS','127.0.0.1');monkeypatch.setenv('WEBHOOK_ALLOW_LOOPBACK','true')
    a=post(client,h['admin'],'/service-accounts',{'name':'隔离接收端','scopes':['releases:read','deliveries:read','receipts:write']}).json()
    i=post(client,h['admin'],'/integrations',{'name':'超时验收','account_id':a['id'],'url':'http://127.0.0.1:18890/notify','active':True,'receipt_timeout_seconds':30});assert i.status_code==201,i.text
    post(client,h['admin'],'/releases/'+r['id']+'/publish',{'expected_version':r['version']});process_files(r['id'])
    d=client.get('/api/v1/deliveries',headers=h['admin']).json()['items'][0]
    from workers import deliveries
    monkeypatch.setattr(deliveries,'send_notification',lambda *args:202);deliveries.process_delivery(d['id'])
    headers={'Authorization':'Bearer '+a['credential'],'X-Organization-ID':h['admin']['X-Organization-ID']}
    return client,h,r,d,headers


def test_receipt_timeout_late_duplicate_and_replay_fence(env,monkeypatch):
    client,h,r,d,machine=integration_fixture(env,monkeypatch)
    from workers.deliveries import scan_receipts
    with transaction() as s:
        delivery=s.get(Delivery,d['id']);due=delivery.receipt_due_at;assert due-delivery.received_at==30
    assert scan_receipts(due-1)==0
    assert scan_receipts(due+1)==1
    assert client.get('/api/v1/deliveries/'+d['id'],headers=h['admin']).json()['status']=='RECONCILE'
    assert any(x['kind']=='delivery' for x in client.get('/api/v1/todos',headers=h['admin']).json()['items'])
    post(client,h['admin'],'/deliveries/'+d['id']+'/reconcile',{'action':'checked','reason':'对端应用中，继续等待真实回执'})
    assert client.get('/api/v1/deliveries/'+d['id'],headers=h['admin']).json()['status']=='RECONCILE'
    body={'event_id':d['event_id'],'status':'APPLIED','applied_release_id':r['id'],'base_release_id':None,'snapshot_hash':r['snapshot_hash']}
    bad=post(client,machine,'/service/deliveries/'+d['id']+'/receipt',{**body,'snapshot_hash':'wrong'});assert bad.status_code==409
    post(client,h['admin'],'/deliveries/'+d['id']+'/replay',{'reason':'已对账，重放同一事件'})
    for _ in range(3):assert post(client,machine,'/service/deliveries/'+d['id']+'/receipt',body).status_code==200
    from workers.deliveries import process_delivery
    process_delivery(d['id'])
    with transaction() as s:
        current=s.get(Delivery,d['id']);assert current.status=='APPLIED' and current.attempts==1 and current.event_id==d['event_id']
    assert not any(x['kind']=='delivery' for x in client.get('/api/v1/todos',headers=h['admin']).json()['items'])


def test_materialized_delta_bounded_and_hash_consistent(env):
    client,h,run,new,old,task,_=impact_fixture(env);replacement=publish_new(client,h,new,old)
    account=post(client,h['admin'],'/service-accounts',{'name':'增量分页读取','scopes':['releases:read']}).json();headers={'Authorization':'Bearer '+account['credential'],'X-Organization-ID':h['admin']['X-Organization-ID']}
    statements=[]
    def observe(conn,cursor,statement,*args):statements.append(statement)
    event.listen(engine,'before_cursor_execute',observe)
    try:
        values=[];offset=0
        while offset is not None:
            response=client.get(f"/api/v1/service/releases/{replacement['id']}/delta?base_release_id={old['id']}&offset={offset}&limit=3",headers=headers);assert response.status_code==200,response.text
            page=response.json();values.extend(page['items']);offset=page['next_offset']
        assert digest(values)==page['delta_hash']
        assert not any('FROM release_rows' in q for q in statements)
    finally:event.remove(engine,'before_cursor_execute',observe)
    with transaction() as s:assert s.scalar(select(func.count()).select_from(ReleaseDelta).where(ReleaseDelta.release_id==replacement['id']))==1
    assert client.get(f"/api/v1/service/releases/{replacement['id']}/delta?base_release_id=wrong",headers=headers).status_code==409


def test_catalog_plan_explicit_all_groups_and_retired_count(env):
    client,h,run,new,old,task,_=impact_fixture(env)
    response=post(client,h['admin'],'/catalog-plans',{'from_version_id':run['catalog_version_id'],'to_version_id':new['catalog_version_id']});assert response.status_code==202,response.text
    plan=response.json();process_plan(plan['id']);process_plan(plan['id']);detail=client.get('/api/v1/catalog-plans/'+plan['id']+'?limit=3',headers=h['admin']).json()
    assert len(detail['items'])==3 and detail['next_cursor']
    body={'expected_version':detail['version'],'retired_count':0,'reason':'完整身份检查'}
    assert post(client,h['admin'],'/catalog-plans/'+plan['id']+'/submit',body).json()['code']=='UNCONFIRMED_IDENTITIES'
    for group in detail['groups']:
        result=post(client,h['admin'],'/catalog-plans/'+plan['id']+'/confirm',{'expected_version':body['expected_version'],'group':group['group'],'reason':'受控数据分组确认'});assert result.status_code==200,result.text;body['expected_version']=result.json()['version']
    assert post(client,h['admin'],'/catalog-plans/'+plan['id']+'/submit',{**body,'retired_count':1}).json()['code']=='RETIREMENT_COUNT'
    assert post(client,h['admin'],'/catalog-plans/'+plan['id']+'/submit',body).status_code==202


def return_rows(client,h,run):
    content=client.get('/api/v1/runs/'+run['id']+'/corrections/return-template',headers=h['operator']).content
    rows=list(csv.DictReader(io.StringIO(content.decode('utf-8-sig'))))
    for n,row in enumerate(rows):row['price']=str(7200+n);row['资料来源']='隔离回填资料第 '+str(n)+' 项'
    return rows


def returned_bytes(rows):
    buf=io.StringIO();writer=csv.DictWriter(buf,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows);return buf.getvalue().encode('utf-8-sig')


def test_correction_return_unordered_idempotent_drafts_only(env):
    client,h,run,items=setup_enterprise(env)
    # Exactly 20 identified problems, regardless of the seed suggestion mix.
    with transaction(write=True) as s:
        chosen=list(s.scalars(select(Item).where(Item.run_id==run['id']).order_by(Item.id).limit(20)))
        for i in s.scalars(select(Item).where(Item.run_id==run['id'])):i.suggestion='RECOMMENDED'
        for i in chosen:i.status='NEEDS_INFO'
    rows=return_rows(client,h,run);assert len(rows)==20
    def upload(rows):return client.post('/api/v1/runs/'+run['id']+'/corrections/return',headers=h['operator'],files={'file':('returned.csv',returned_bytes(rows),'text/csv')})
    first=upload(list(reversed(rows)));assert first.status_code==200,first.text
    second=upload(rows);assert second.json()['draft_ids']==first.json()['draft_ids'] and first.json()['count']==20 and second.json()['duplicate'] and not first.json()['duplicate']
    with transaction() as s:
        drafts=list(s.scalars(select(CorrectionDraft).where(CorrectionDraft.run_id==run['id'])));assert len(drafts)==20 and all(d.submitted_revision_id is None for d in drafts)
        values={r['source_id']:r['price'] for r in rows};assert all(d.fields['price']==values[d.source_id] for d in drafts)
    assert upload(rows+[rows[0]]).json()['code']=='DUPLICATE_PROBLEMS'


def test_correction_return_rejects_unknown_and_stale_atomically(env):
    client,h,run,items=setup_enterprise(env);rows=return_rows(client,h,run)
    def upload(values):return client.post('/api/v1/runs/'+run['id']+'/corrections/return',headers=h['operator'],files={'file':('returned.csv',returned_bytes(values),'text/csv')})
    bad=[dict(r) for r in rows];bad[0]['problem_id']=uid();assert upload(bad).status_code==422
    with transaction(write=True) as s:s.get(Item,rows[-1]['problem_id']).version+=1
    result=upload(rows);assert result.status_code==409 and result.json()['code']=='STALE_TEMPLATE'
    with transaction() as s:assert s.scalar(select(func.count()).select_from(CorrectionDraft))==0


def test_quality_snapshot_stale_review_refresh_and_bounded_pages(env):
    client,h,run,items=setup_enterprise(env);process_quality(run['revision_id'])
    path='/api/v1/revisions/'+run['revision_id']+'/quality?limit=2';report=client.get(path,headers=h['operator']).json();assert not report['stale']
    statements=[]
    def observe(conn,cursor,statement,*args):statements.append(statement)
    event.listen(engine,'before_cursor_execute',observe)
    try:client.get(path+'&offset=2',headers=h['operator'])
    finally:event.remove(engine,'before_cursor_execute',observe)
    assert not any('FROM source_records' in q or 'FROM review_events' in q for q in statements)
    item=items[0];assert post(client,h['reviewer'],'/items/'+item['id']+'/decisions',{'action':'unmatched','expected_version':item['version'],'reason':'复核后确认当前无匹配'}).status_code==201
    assert client.get(path,headers=h['operator']).json()['stale']
    process_quality(run['revision_id']);assert not client.get(path,headers=h['operator']).json()['stale']
    assert post(client,h['reviewer'],'/items/'+item['id']+'/decisions',{'action':'revoke','expected_version':item['version']+1,'reason':'复核撤销后的质量刷新'}).status_code==201
    process_quality(run['revision_id']);r=client.get(path,headers=h['operator']).json();assert r['completed_processing_seconds']['sample_count']==0


def test_supplier_200_versions_complete_paging(env):
    client,h,run,items=setup_enterprise(env);org=h['admin']['X-Organization-ID']
    supplier=post(client,h['admin'],'/suppliers',{'name':'200版历史来源'}).json();post(client,h['admin'],'/supplier-bindings',{'supplier_id':supplier['id'],'batch_ids':[run['batch_id']],'reason':'测试历史归属'})
    with transaction(write=True) as s:
        original=s.get(Revision,run['revision_id'])
        for n in range(2,201):
            values={col.name:getattr(original,col.name) for col in Revision.__table__.columns if col.name not in ('id','number','content_hash')};s.add(Revision(id=uid(),number=n,content_hash=f'history-{n}',**values))
    cursor=None;ids=[]
    while True:
        r=client.get('/api/v1/suppliers/'+supplier['id']+'/quality?limit=37'+('&cursor='+cursor if cursor else ''),headers=h['admin']).json();ids.extend(x['revision_id'] for x in r['items']);cursor=r['next_cursor']
        if not cursor:break
    assert len(ids)==len(set(ids))==200 and r['total_versions']==200 and r['summary']['pending_versions']==200


def test_delta_receiver_stages_atomically_and_preserves_newer_versions():
    import sqlite3
    from scripts import delta_receiver as receiver
    from packages.domain.releases import delta_rows
    s=sqlite3.connect(':memory:');s.executescript('CREATE TABLE batches(batch_id TEXT PRIMARY KEY,release_id TEXT,number INTEGER,invalidated INTEGER); CREATE TABLE mappings(batch_id TEXT,stable_key TEXT,row_data TEXT,PRIMARY KEY(batch_id,stable_key));');receiver.initialize(s)
    old=[{'stable_key':'a','status':'CONFIRMED','target_sku':'OLD'},{'stable_key':'b','status':'CONFIRMED','target_sku':'DELETE'}]
    new=[{'stable_key':'a','status':'UNMATCHED','target_sku':None},{'stable_key':'c','status':'CONFIRMED','target_sku':'ADD'}];values=delta_rows(old,new)
    with s:
        s.execute('INSERT INTO batches VALUES(?,?,?,?)',('batch','base',1,0));s.executemany('INSERT INTO mappings VALUES(?,?,?)',[('batch',r['stable_key'],json.dumps(receiver.project(r))) for r in old])
    meta={'release_id':'new','base_release_id':'base','total':len(values),'delta_hash':digest(values)}
    with s:receiver.stage(s,'event',{**meta,'items':values[:1]},0)
    import pytest
    with pytest.raises(ValueError,match='DELTA_INCOMPLETE'):receiver.complete(s,'event')
    assert s.execute('SELECT count(*) FROM mappings').fetchone()[0]==2
    with s:receiver.stage(s,'event',{**meta,'items':values[1:]},1);receiver.stage(s,'event',{**meta,'items':values[1:]},1)
    with pytest.raises(RuntimeError):
        with s:receiver.apply(s,'event','batch','new',2);raise RuntimeError('connection lost before atomic commit')
    assert dict(s.execute('SELECT stable_key,row_data FROM mappings'))=={r['stable_key']:json.dumps(receiver.project(r)) for r in old}
    with s:assert receiver.apply(s,'event','batch','new',2)=='APPLIED';s.execute("UPDATE batches SET release_id='new',number=2")
    assert {k:json.loads(v) for k,v in s.execute('SELECT stable_key,row_data FROM mappings')}=={r['stable_key']:receiver.project(r) for r in new}
    with s:assert receiver.apply(s,'event','batch','new',2)=='OLDER_OR_DUPLICATE';s.execute("UPDATE batches SET release_id='newer',number=3")
    with s:assert receiver.apply(s,'event','batch','new',2)=='OLDER_OR_DUPLICATE'


def test_catalog_identity_batches_recover_and_serialize_duplicates(env):
    client,h,run,items=setup_enterprise(env);org=h['admin']['X-Organization-ID']
    with transaction(write=True) as s:
        actor=s.scalar(select(User).where(User.email=='admin@demo.local'));base=s.get(CatalogVersion,run['catalog_version_id']);catalog=Catalog(id=uid(),org_id=org,name='分段恢复夹具');s.add(catalog);s.flush();versions=[]
        for n in (1,2):
            version=CatalogVersion(id=uid(),org_id=org,catalog_id=catalog.id,file_id=base.file_id,number=n,status='PUBLISHED',content_hash=f'batch-{n}',row_count=600,mapping=base.mapping,errors=[]);s.add(version);versions.append(version)
        s.flush();links={}
        for n in range(600):
            a,b=uid(),uid();links[a]=b
            for ident,version in ((a,versions[0]),(b,versions[1])):s.add(Product(id=ident,org_id=org,version_id=version.id,sku=str(n),raw={'name':str(n)},normalized={'name':str(n)},fingerprint=str(n)))
        s.flush();job=create_change(s,Context(org,actor.id,['admin']),versions[0].id,versions[1].id,links);ident=job.id
    writes=0
    def interrupt(conn,cursor,statement,*args):
        nonlocal writes
        if statement.startswith('INSERT INTO product_identities'):
            writes+=1
            if writes==2:raise RuntimeError('controlled interruption after first identity batch')
    event.listen(engine,'before_cursor_execute',interrupt)
    import pytest
    try:
        with pytest.raises(RuntimeError):process_change(ident)
    finally:event.remove(engine,'before_cursor_execute',interrupt)
    with transaction() as s:assert s.get(CatalogChange,ident).status=='FAILED';assert s.scalar(select(func.count()).select_from(ProductIdentity).where(ProductIdentity.org_id==org))==500
    with ThreadPoolExecutor(2) as pool:list(pool.map(process_change,[ident,ident]))
    with transaction() as s:
        assert s.get(CatalogChange,ident).status=='SUCCEEDED'
        identities=list(s.scalars(select(ProductIdentity).where(ProductIdentity.org_id==org)));assert len(identities)==1200
        lookup={r.product_id:r.identity_id for r in identities};assert all(lookup[a]==lookup[b] for a,b in links.items())
