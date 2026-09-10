import copy
import json
from pathlib import Path
from uuid import uuid4
import pytest
from sqlalchemy import event, func, select
from packages.domain import storage
from packages.domain.auth import Context
from packages.domain.db import engine,transaction,uid
from packages.domain.models import *
from packages.matching.normalize import digest,normalize
from workers.imports import process_import
from workers.jobs import process_run,process_export
from workers.maintenance import schedule_cleanup,process_cleanup
from tests.test_business import P,new_run,confirm,recommended
from scripts.seed import MAPPING,csv_bytes,sample_rows


def upload(c,h,rows):
    r=c.post(P+'/files',headers=h,files={'file':('optimization.csv',csv_bytes(rows),'text/csv')});assert r.status_code==202,r.text
    f=r.json();process_import(f['job_id']);return f


def revision(c,h,run,rows):
    f=upload(c,h,rows);r=c.post(f"{P}/batches/{run['batch_id']}/revisions",headers=h,json={'file_id':f['id'],'sheet':'CSV','header_row':1,'mapping':MAPPING,'exclude_rows':[]});assert r.status_code==202,r.text
    new=new_run(c,h,{**run,'revision_id':r.json()['id']}).json();process_run(new['id']);return new


def test_sql_page_constant_and_tenant_scoped(env):
    c,h,run,items=env;sample=recommended(items);org=h['operator']['X-Organization-ID']
    with transaction(write=True) as s:
        for n in range(110):
            src=Source(id=uid(),org_id=org,revision_id=run['revision_id'],row_no=1000+n,sku=f'P-{n}',generated_sku=False,raw=sample['source']['raw'],normalized=sample['source']['normalized'],issues=[],excluded=False);s.add(src);s.flush()
            item=Item(id=uid(),org_id=org,run_id=run['id'],source_id=src.id,suggestion='RECOMMENDED');s.add(item);s.flush()
            for candidate in sample['candidates']:
                s.add(Candidate(org_id=org,item_id=item.id,**{k:candidate[k] for k in ('product_id','rank','score','features','evidence','conflicts','missing')}))
    counts=[]
    for limit in (10,100):
        statements=[]
        def count(conn,cursor,statement,*args):statements.append(statement)
        event.listen(engine,'before_cursor_execute',count)
        try:r=c.get(f"{P}/runs/{run['id']}/items?limit={limit}",headers=h['operator'])
        finally:event.remove(engine,'before_cursor_execute',count)
        assert r.status_code==200 and len(r.json()['items'])==limit
        counts.append(len(statements))
    assert counts[0]==counts[1] and counts[1]-4<=10,counts
    assert c.get(f"{P}/runs/{run['id']}/items?limit=100",headers=h['other']).status_code==404


def test_reordered_file_has_zero_business_difference_and_five_field_changes(env):
    c,h,run,items=env;_,rows=sample_rows();rows=rows[:29][::-1]
    reordered=revision(c,h['operator'],run,rows)
    result=c.get(f"{P}/runs/{run['id']}/compare/{reordered['id']}",headers=h['operator']).json()
    assert result['total']==0 and result['ambiguity_total']==0,result
    for row in rows[:5]:row[5]='777'
    changed=revision(c,h['operator'],run,rows)
    result=c.get(f"{P}/runs/{reordered['id']}/compare/{changed['id']}?limit=2",headers=h['operator']).json()
    assert result['total']==5 and len(result['changes'])==2 and result['next_cursor']=='2'
    assert all('identity_fields' in x['types'] for x in result['changes'])


def test_duplicate_and_generated_ids_are_explicit_ambiguities(env):
    c,h,run,items=env;_,rows=sample_rows();rows=rows[:29];rows[1][0]=rows[0][0];rows[2][0]=''
    new=revision(c,h['operator'],run,rows)
    result=c.get(f"{P}/runs/{run['id']}/compare/{new['id']}",headers=h['operator']).json()
    assert result['ambiguity_total']==2
    assert any('缺少稳定' in a['reason'] for a in result['ambiguities'])


def test_async_parse_and_validation_cache_full_paging_retry_and_isolation(env,monkeypatch):
    c,h,run,items=env
    original=__import__('workers.imports',fromlist=['parse_file']).parse_file;calls=[]
    def parsed(*args,**kwargs):calls.append(1);return original(*args,**kwargs)
    monkeypatch.setattr('workers.imports.parse_file',parsed)
    _,rows=sample_rows();f=upload(c,h['operator'],rows);again=upload(c,h['operator'],rows)
    assert f['job_id']==again['job_id'] and len(calls)==1
    config={'file_id':f['id'],'sheet':'CSV','header_row':1,'mapping':MAPPING,'exclude_rows':[]}
    a=c.post(P+'/import-jobs',headers=h['operator'],json=config);b=c.post(P+'/import-jobs',headers=h['operator'],json=config)
    assert a.status_code==202 and a.json()['id']==b.json()['id'];job=a.json()['id'];process_import(job);process_import(job)
    listing=c.get(f'{P}/import-jobs/{job}/rows?limit=7',headers=h['operator']).json()
    assert listing['total']==30 and len(listing['items'])==7 and listing['items'][0]['row_no']==31
    assert c.get(f'{P}/import-jobs/{job}',headers=h['other']).status_code==404
    assert c.get(f'{P}/import-jobs/{job}/rows?download=true',headers=h['operator']).headers['content-type'].startswith('text/csv')
    assert len(calls)==1
    bad=c.post(P+'/files',headers=h['operator'],files={'file':('bad.csv',b'\xff','text/csv')}).json();process_import(bad['job_id'])
    assert c.get(P+'/import-jobs/'+bad['job_id'],headers=h['operator']).json()['status']=='FAILED'
    assert c.post(P+'/import-jobs/'+bad['job_id']+'/retry',headers=h['operator']).status_code==202
    process_import(bad['job_id'])
    assert c.get(P+'/import-jobs/'+bad['job_id'],headers=h['operator']).json()['attempts']==2


def test_mapping_templates_versioned_no_duplicate_and_cross_tenant_hidden(env):
    c,h,run,items=env;body={'name':'手机映射','supplier':'供应商甲','headers':list(MAPPING.values()),'mapping':MAPPING}
    a=c.post(P+'/mapping-templates',headers=h['operator'],json=body);b=c.post(P+'/mapping-templates',headers=h['operator'],json=body)
    assert a.status_code==201 and a.json()['id']==b.json()['id']
    body['headers']=body['headers']+['新名称'];body['mapping']={**MAPPING,'name':'新名称'}
    r=c.post(P+'/mapping-templates',headers=h['operator'],json=body);assert r.json()['number']==2
    assert c.get(P+'/mapping-templates',headers=h['other']).json()['items']==[]


def test_correction_subset_immutable_traceable_idempotent_and_dual_review(env):
    c,h,run,items=env;item=next(i for i in items if 'color' in i['source']['normalized']['missing']);old=copy.deepcopy(item['source']);path=f"{P}/runs/{run['id']}/corrections/{old['id']}"
    target=item['candidates'][0]['product']['normalized']['color']
    body={'fields':{'color':target},'evidence':'受控测试：独立来源规格单','expected_version':0}
    assert c.put(path,headers=h['reviewer'],json=body).status_code==403
    draft=c.put(path,headers=h['operator'],json=body);assert draft.status_code==200,draft.text
    assert c.put(path,headers=h['operator'],json=body).status_code==409
    k=str(uuid4());headers={**h['operator'],'Idempotency-Key':k};body={'draft_ids':[draft.json()['id']]};path=f"{P}/runs/{run['id']}/corrections/submit"
    a=c.post(path,headers=headers,json=body);b=c.post(path,headers=headers,json=body);assert a.status_code==202 and a.json()==b.json(),a.text
    new=a.json();assert new['affected']==1;process_run(new['id'])
    result=c.get(f"{P}/runs/{new['id']}/items",headers=h['operator']).json()['items'];assert len(result)==1
    assert result[0]['source']['correction']['parent_source_id']==old['id']
    assert result[0]['source']['correction']['evidence']=='受控测试：独立来源规格单'
    assert c.get(f"{P}/items/{item['id']}/candidates",headers=h['operator']).json()['source']==old
    assert confirm(c,h['operator'],result[0]).status_code==403
    assert confirm(c,h['reviewer'],result[0]).status_code==201
    comparison=c.get(f"{P}/runs/{run['id']}/compare/{new['id']}",headers=h['operator']).json()
    assert comparison['total']==1 and 'removed' not in comparison['changes'][0]['types']
    assert c.get(f"{P}/runs/{new['id']}",headers=h['operator']).json()['manifest']['correction_scope']=='correction_subset'


def test_101st_batch_member_and_history_discoverable(env):
    c,h,run,items=env;org=h['operator']['X-Organization-ID']
    with transaction(write=True) as s:
        for n in range(105):s.add(Batch(org_id=org,name=f'历史第{n:03d}批',supplier='测试',created_by=run['created_by']))
    result=c.get(P+'/batches?q=历史第104',headers=h['operator']).json();assert len(result['items'])==1
    found=[];cursor=None
    while True:
        r=c.get(P+'/batches',headers=h['operator'],params={'limit':20,**({'cursor':cursor} if cursor else {})}).json();found+=r['items'];cursor=r['next_cursor']
        if not cursor:break
    assert len(found)==106
    assert len(c.get(P+'/members?q=reviewer',headers=h['admin']).json()['items'])==1
    item=recommended(items)
    for seq in range(4):item['version']=seq;assert confirm(c,h['reviewer'],item).status_code==201
    r=c.get(f"{P}/items/{item['id']}/history?limit=2",headers=h['operator']).json();assert [e['seq'] for e in r['items']]==[4,3]
    r=c.get(f"{P}/items/{item['id']}/history?limit=2&cursor=3",headers=h['operator']).json();assert [e['seq'] for e in r['items']]==[2,1]


def test_expired_export_cleanup_retries_and_preserves_snapshot(env,monkeypatch):
    c,h,run,items=env
    result=c.post(f"{P}/runs/{run['id']}/exports",headers={**h['operator'],'Idempotency-Key':str(uuid4())},json={'kind':'all','format':'csv'}).json();process_export(result['id'])
    with transaction(write=True) as s:
        export=s.get(Export,result['id']);export.expires_at=0;key=export.object_key;sha=export.file_hash
    schedule_cleanup()
    with transaction() as s:deletion=s.scalar(select(ArtifactDeletion).where(ArtifactDeletion.export_id==result['id']));ident=deletion.id
    actual=storage.delete
    def failure(_):raise ConnectionError('temporary object store outage')
    monkeypatch.setattr(storage,'delete',failure);process_cleanup(ident)
    with transaction() as s:assert s.get(ArtifactDeletion,ident).error=='ConnectionError'
    monkeypatch.setattr(storage,'delete',actual);process_cleanup(ident);process_cleanup(ident)
    with transaction() as s:
        assert s.get(ArtifactDeletion,ident).deleted_at and s.get(Export,result['id']).file_hash==sha
        assert s.scalar(select(func.count()).select_from(ExportRow).where(ExportRow.export_id==result['id']))==29
    with pytest.raises(FileNotFoundError):storage.read(key)
    assert c.get(f"{P}/exports/{result['id']}/download",headers=h['operator']).status_code==410


def test_usage_and_metrics_are_scoped(env):
    c,h,run,items=env;body={'run_id':run['id'],'item_id':items[0]['id'],'event':'review','duration_ms':900}
    assert c.post(P+'/usage-events',headers=h['reviewer'],json=body).status_code==201
    assert c.post(P+'/usage-events',headers=h['other'],json=body).status_code==404
    assert c.get(P+'/usage-events?run_id='+run['id'],headers=h['admin']).json()['review_median_ms']==900
    text=c.get(P+'/metrics',headers=h['admin']).text
    assert 'product_match_outbox_oldest_seconds' in text and run['id'] not in text


def test_summary_and_selected_details_agree(env):
    c,h,run,items=env
    result=c.get(f"{P}/runs/{run['id']}/items?details=false",headers=h['operator']);assert result.status_code==200
    summary=result.json()['items'][0];assert summary['summary_only'] and 'raw' not in summary['source']
    detail=c.get(f"{P}/items/{summary['id']}/candidates",headers=h['operator']).json()
    assert detail['source']['sku']==summary['source']['sku'] and detail['source']['normalized']['name']==summary['source']['normalized']['name']
    assert [x['product_id'] for x in detail['candidates'][:1]]==[x['product_id'] for x in summary['candidates']]


def test_policy_promotion_api_rejects_simulation_and_shadow_does_not_mutate(env):
    c,h,run,items=env
    config=json.loads(Path('models/phone-sim-v1/bundle.json').read_text());report=json.loads(Path('models/phone-sim-v1/evaluation-report.json').read_text())
    with transaction(write=True) as s:
        evaluation=PolicyEvaluation(id=uid(),org_id=h['admin']['X-Organization-ID'],name='受控模拟实验',config=config,report=report,report_hash=digest(report));s.add(evaluation);ident=evaluation.id
    result=c.post(P+'/policies',headers=h['admin'],json={'name':'不应发布','engine':'lightgbm','evaluation_id':ident,'high_threshold':config['high_threshold'],'margin':config['margin']})
    assert result.status_code==409 and result.json()['code']=='POLICY_NOT_READY',result.text
    result=c.post(f"{P}/runs/{run['id']}/shadow",headers={**h['admin'],'Idempotency-Key':str(uuid4())},json={'evaluation_id':ident});assert result.status_code==202,result.text
    from workers.shadow import process_shadow
    process_shadow(result.json()['id']);process_shadow(result.json()['id'])
    shadow=c.get(P+'/shadow-runs/'+result.json()['id'],headers=h['admin']).json();assert shadow['status']=='SUCCEEDED',shadow
    assert shadow['report']['n']==29
    assert c.get(f"{P}/runs/{run['id']}/items",headers=h['operator']).json()['items']==items
    assert c.get(P+'/shadow-runs/'+result.json()['id'],headers=h['other']).status_code==404


def test_chinese_search_uses_decoded_json_text(env):
    c,h,run,items=env
    r=c.get(f"{P}/runs/{run['id']}/items?q=小米&details=false",headers=h['operator']);assert r.status_code==200 and r.json()['items']
    assert all('小米' in i['source']['normalized']['name'] for i in r.json()['items'])
    r=c.get(f"{P}/catalog-versions/{run['catalog_version_id']}/products?q=黑色",headers=h['operator']);assert r.status_code==200 and r.json()['items']
    assert all('黑色' in p['normalized']['name'] for p in r.json()['items'])
