"""Evaluate a complete entity-labeled retrieval manifest, never unlabelled pairs.

JSON: {catalog:[{id,entity_id,fields}], queries:[{id,entity_id,fields}], provenance:{...}}
query entity_id may be null only for a verified/controlled no-match query.
"""
import argparse
import json
import time
from pathlib import Path
import numpy as np
from packages.matching.engine import Matcher
from packages.matching.normalize import digest, normalize


def evaluate(manifest,policy=None):
    assert 'provenance' in manifest and manifest['provenance'].get('complete_entity_labels'), 'Complete entity labels required'
    catalog=manifest['catalog'];queries=manifest['queries']
    assert len({p['id'] for p in catalog})==len(catalog)
    assert len({p['entity_id'] for p in catalog})==len(catalog),'Use one fixed representative per standard entity'
    assert not ({p['id'] for p in catalog}&{q['id'] for q in queries}),'Query/catalog records must be disjoint'
    entity_by_id={p['id']:p['entity_id'] for p in catalog};entities=set(entity_by_id.values())
    t0=time.perf_counter();matcher=Matcher([{'id':p['id'],'normalized':normalize(p['fields'])} for p in catalog],policy);index_time=time.perf_counter()-t0
    rows=[]
    for q in queries:
        state,candidates=matcher.match(normalize(q['fields']))
        matchable=q['entity_id'] in entities
        retrieved=any(entity_by_id[c['product_id']]==q['entity_id'] for c in candidates)
        recommended=state=='RECOMMENDED'
        correct=recommended and entity_by_id[candidates[0]['product_id']]==q['entity_id']
        rows.append({'id':q['id'],'entity_id':q['entity_id'] or q['id'],'matchable':matchable,'retrieved':retrieved,'recommended':recommended,'correct':correct,'state':state})
    def metrics(rows):
        n=len(rows);eligible=sum(r['matchable'] for r in rows);recommended=sum(r['recommended'] for r in rows);correct=sum(r['correct'] for r in rows);no_match=n-eligible
        return {'n':n,'matchable':eligible,'recommended':recommended,'no_match':no_match,'recall_at_20':sum(r['retrieved'] and r['matchable'] for r in rows)/eligible if eligible else None,'top1_precision':correct/recommended if recommended else None,'coverage':recommended/n if n else None,'end_to_end_recall':correct/eligible if eligible else None,'no_match_false_recommendation':sum(r['recommended'] and not r['matchable'] for r in rows)/no_match if no_match else None}
    result=metrics(rows);rng=np.random.default_rng(42);grouped={}
    for r in rows:grouped.setdefault(r['entity_id'],[]).append(r)
    intervals=[]
    for _ in range(400):
        sample=[r for g in rng.choice(list(grouped),len(grouped),replace=True) for r in grouped[g]]
        p=metrics(sample)['top1_precision']
        if p is not None:intervals.append(p)
    result.update({'top1_precision_95_ci':np.quantile(intervals,[.025,.975]).tolist() if intervals else None,'status':'exploratory' if result['recommended']<200 else 'report interval and independent entity count before admission','elapsed_seconds':time.perf_counter()-t0,'index_seconds':index_time,'manifest_sha256':digest(manifest),'provenance':manifest['provenance'],'states':{k:sum(r['state']==k for r in rows) for k in set(r['state'] for r in rows)},'rows':rows})
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('manifest');p.add_argument('--output',default='docs/retrieval-results.json');a=p.parse_args();m=json.loads(Path(a.manifest).read_text());Path(a.output).write_text(json.dumps(evaluate(m),ensure_ascii=False,indent=2))
