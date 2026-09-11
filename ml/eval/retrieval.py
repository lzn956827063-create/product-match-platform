"""Full-catalog, entity-grouped evaluation with explicit admission denominators."""
import argparse
import json
import time
from collections import defaultdict
from pathlib import Path
import numpy as np
from packages.matching.engine import DEFAULT_POLICY, Matcher
from packages.matching.normalize import digest, normalize
from packages.matching.policy import policy_hash


def wilson(successes, n):
    if not n: return None
    z=1.959963984540054; p=successes/n; den=1+z*z/n
    center=(p+z*z/(2*n))/den; radius=z*((p*(1-p)/n+z*z/(4*n*n))**.5)/den
    return [max(0,center-radius), min(1,center+radius)]


def validate_manifest(m):
    if not m.get('provenance',{}).get('complete_entity_labels'):raise ValueError('COMPLETE_LABELS_REQUIRED')
    catalog, queries=m['catalog'],m['queries']
    if len({p['id'] for p in catalog})!=len(catalog) or len({q['id'] for q in queries})!=len(queries):raise ValueError('DUPLICATE_RECORD_ID')
    if {p['id'] for p in catalog}&{q['id'] for q in queries}:raise ValueError('QUERY_CATALOG_OVERLAP')
    entities={p['entity_id'] for p in catalog};seen={}
    for name,ids in m.get('partitions',{}).items():
        for ident in ids:
            if ident in seen:raise ValueError('ENTITY_SPLIT_LEAKAGE')
            seen[ident]=name
    for q in queries:
        if seen and seen.get(q['entity_id'])!=q.get('partition'):raise ValueError('QUERY_SPLIT_MISMATCH')
        if 'catalog_exists' in q and q['catalog_exists']!=(q['entity_id'] in entities):raise ValueError('NO_MATCH_CATALOG_LEAKAGE')
    removed=set(m['provenance'].get('removed_entities',[]))
    if removed&entities:raise ValueError('NO_MATCH_CATALOG_LEAKAGE')
    if seen:
        train_series={q.get('series') for q in queries if q['partition']=='train'}
        holdout={q.get('series') for q in queries if q['partition']=='series_holdout'}
        if (train_series&holdout)-{None}:raise ValueError('SERIES_LEAKAGE')
    # Truth is explicitly established by two independent assignments and adjudication.
    def approved(q):
        annotations=q.get('annotators',[])
        if len({a.get('id') for a in annotations if a.get('type')=='human' and a.get('id')})<2:return False
        if q.get('review_status') not in ('AGREED','ADJUDICATED') or not q.get('match_evidence'):return False
        labels={(a.get('entity_id'),a.get('catalog_exists')) for a in annotations}
        if len(labels)>1 and not q.get('adjudicator'):return False
        return q.get('review_status')=='ADJUDICATED' or labels=={(q['entity_id'],q.get('catalog_exists'))}
    eligible=m['provenance'].get('type') in ('licensed_public','authorized_supplier') and bool(m['provenance'].get('license')) and m['provenance'].get('near_duplicate_review')=='completed' and {'train','tuning','calibration','threshold','test'} <= set(m.get('partitions',{})) and bool(queries) and bool(seen) and all(approved(q) for q in queries)
    return {'frozen_test_eligible':eligible,'entity_splits_disjoint':True,'series_holdout_disjoint':True,'removed_entities_absent':True,'catalog_record_reuse':'Standard catalog may include test entities; scoring vocabulary may only fit train records.'}


def metrics(rows, intervals=True):
    n=len(rows); matchable=sum(r['matchable'] for r in rows); recommended=sum(r['recommended'] for r in rows); correct=sum(r['correct'] for r in rows); absent=n-matchable
    timings=[r['inference_ms'] for r in rows if r.get('inference_ms') is not None]
    result={
        'n':n,'entities':len({r['entity_id'] for r in rows}),'matchable':matchable,'recommended':recommended,
        'recommended_entities':len({r['entity_id'] for r in rows if r['recommended']}),'no_match':absent,
        'recall_at_20':sum(r['retrieved'] and r['matchable'] for r in rows)/matchable if matchable else None,
        'top1_precision':correct/recommended if recommended else None,
        'mrr':sum((1/r.get('truth_rank')) if r.get('truth_rank') else 0 for r in rows if r['matchable'])/matchable if matchable else None,
        'ndcg_at_5':sum((1/np.log2(r['truth_rank']+1)) if r.get('truth_rank') and r['truth_rank']<=5 else 0 for r in rows if r['matchable'])/matchable if matchable else None,
        'coverage':recommended/n if n else None,'rejection_rate':1-recommended/n if n else None,
        'end_to_end_recall':correct/matchable if matchable else None,
        'no_match_false_recommendation':sum(r['recommended'] and not r['matchable'] for r in rows)/absent if absent else None,
        'hard_conflict_recommendations':sum(bool(r['recommended'] and r.get('top1_conflicts')) for r in rows),
        'inference_mean_ms':float(np.mean(timings)) if timings else None,
        'inference_p95_ms':float(np.quantile(timings,.95)) if timings else None,
    }
    if not intervals:return result
    grouped=defaultdict(list)
    for r in rows:grouped[r['entity_id']].append(r)
    values=[];rng=np.random.default_rng(42);keys=list(grouped)
    for _ in range(400 if keys else 0):
        sample=[r for g in rng.choice(keys,len(keys),replace=True) for r in grouped[g]]
        denom=sum(r['recommended'] for r in sample)
        if denom:values.append(sum(r['correct'] for r in sample)/denom)
    bootstrap=np.quantile(values,[.025,.975]).tolist() if values else None
    # Degenerate entity bootstrap at zero errors is supplemented by an entity Wilson bound.
    recgroups=[rs for rs in grouped.values() if any(r['recommended'] for r in rs)]
    rec_ci=wilson(sum(all(r['correct'] for r in rs if r['recommended']) for rs in recgroups),len(recgroups))
    absentgroups=[rs for rs in grouped.values() if not any(r['matchable'] for r in rs)]
    result.update({'top1_precision_95_ci_group_bootstrap':bootstrap,'top1_precision_95_ci_entity_wilson':rec_ci,'top1_precision_95_ci':[min(bootstrap[0],rec_ci[0]),max(bootstrap[1],rec_ci[1])] if bootstrap and rec_ci else None,'no_match_false_recommendation_95_ci_entity_wilson':wilson(sum(any(r['recommended'] for r in rs) for rs in absentgroups),len(absentgroups)),'no_match_entities':len(absentgroups)})
    return result


def evaluate(manifest,policy=None,partition=None):
    audit=validate_manifest(manifest);policy=policy or DEFAULT_POLICY
    catalog=manifest['catalog'];queries=[q for q in manifest['queries'] if partition is None or q.get('partition')==partition]
    entity_by_id={p['id']:p['entity_id'] for p in catalog};entities=set(entity_by_id.values())
    t0=time.perf_counter();matcher=Matcher([{'id':p['id'],'normalized':normalize(p['fields'])} for p in catalog],policy);index_time=time.perf_counter()-t0
    rows=[]
    for q in queries:
        query_started=time.perf_counter();state,candidates=matcher.match(normalize(q['fields']));inference_ms=(time.perf_counter()-query_started)*1000;matchable=q['entity_id'] in entities
        retrieved=any(entity_by_id[c['product_id']]==q['entity_id'] for c in candidates);recommended=state=='RECOMMENDED'
        correct=recommended and entity_by_id[candidates[0]['product_id']]==q['entity_id']
        truth_rank=next((index+1 for index,candidate in enumerate(candidates) if entity_by_id[candidate['product_id']]==q['entity_id']),None)
        rows.append({'id':q['id'],'entity_id':q['entity_id'] or q['id'],'matchable':matchable,'retrieved':retrieved,'truth_rank':truth_rank,'recommended':recommended,'correct':correct,'state':state,'slices':q.get('slices',[]),'top1':candidates[0]['product_id'] if candidates else None,'top1_conflicts':bool(candidates and candidates[0]['conflicts']),'inference_ms':inference_ms,'top20':[r['product_id'] for r in candidates]})
    overall=metrics(rows)
    result={**overall,'metrics':overall,'dataset_version':manifest.get('version'),'dataset_hash':digest(manifest),'policy_hash':policy_hash(policy),'partition':partition,'frozen_test_eligible':audit['frozen_test_eligible'] and partition=='test','audit':audit,'slices':{name:metrics([r for r in rows if name in r['slices']]) for name in sorted({v for r in rows for v in r['slices']})},'scope':'controlled_simulation' if manifest['provenance'].get('type')=='controlled_simulation' else 'declared_entity_dataset','elapsed_seconds':time.perf_counter()-t0,'index_seconds':index_time,'provenance':manifest['provenance'],'rows':rows}
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('manifest');p.add_argument('--partition',default='test');p.add_argument('--policy');p.add_argument('--output',default='docs/optimization/phone-evaluation.json');a=p.parse_args()
    m=json.loads(Path(a.manifest).read_text());policy=json.loads(Path(a.policy).read_text()) if a.policy else None
    result=evaluate(m,policy,a.partition);Path(a.output).write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result['metrics'],indent=2))
