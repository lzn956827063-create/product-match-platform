import copy
import json
from pathlib import Path
import numpy as np
import pytest
from packages.matching.engine import Matcher,FEATURE_NAMES,frozen_pair_features,load_model,load_vectorizer
from packages.matching.normalize import digest,normalize
from packages.matching.policy import admission,artifact_path,policy_hash,validate_bundle
from ml.eval.retrieval import metrics,validate_manifest

ROOT=Path(__file__).resolve().parents[1]


def bundle():return json.loads((ROOT/'models/phone-sim-v1/bundle.json').read_text())


def test_offline_online_frozen_feature_score_ranking_status_parity():
    config=bundle();m=json.loads((ROOT/'ml/datasets/phone-sim-v1/manifest.json').read_text());products=[{'id':p['id'],'normalized':normalize(p['fields'])} for p in m['catalog']];by_id={p['id']:p['normalized'] for p in products};matcher=Matcher(products,config)
    vectorizer=load_vectorizer(str(artifact_path(config['vectorizer_artifact'])),config['vectorizer_hash']);model=load_model(str(artifact_path(config['model_artifact'])),config['model_hash'])
    for q in m['queries'][::37]:
        source=normalize(q['fields']);state,rows=matcher.match(source)
        offline=frozen_pair_features(source,[by_id[r['product_id']] for r in rows],vectorizer)
        x=np.array([[f[k] for k in FEATURE_NAMES] for f in offline]);online=np.array([[r['features'][k] for k in FEATURE_NAMES] for r in rows]);np.testing.assert_allclose(x,online,rtol=0,atol=1e-6)
        scores=model.predict(x,num_threads=1);cal=config['calibration'];logits=np.log(np.clip(scores,1e-6,1-1e-6)/np.clip(1-scores,1e-6,1));scores=1/(1+np.exp(-(logits*cal['coef']+cal['intercept'])))
        np.testing.assert_allclose(scores,[r['score'] for r in rows],rtol=0,atol=1e-6)
        replica=Matcher(products,config);assert replica.match(source)==(state,rows)


def test_whole_bundle_gate_rejects_tamper_failed_metrics_and_simulation():
    config=bundle();report=json.loads((ROOT/'models/phone-sim-v1/evaluation-report.json').read_text())
    assert not admission(report,config)['eligible']
    for field,value in [('high_threshold',.3),('margin',.4),('vectorizer_hash','0'*64),('feature_names',list(reversed(FEATURE_NAMES)))]:
        mutated={**config,field:value}
        with pytest.raises(ValueError):validate_bundle(mutated)
    mutated={**config,'high_threshold':.3};mutated['bundle_hash']=policy_hash(mutated)
    assert 'POLICY_REPORT_MISMATCH' in admission(report,mutated)['reasons']
    forged=copy.deepcopy(report);forged['frozen_test_eligible']=True;forged['metrics']['recommended_entities']=250;forged['metrics']['top1_precision']=.97
    assert 'METRIC_TOP1_PRECISION' in admission(forged,config)['reasons']


def test_empty_recommendations_not_perfect_and_zero_errors_have_nonzero_interval():
    rows=[{'entity_id':str(i),'matchable':False,'recommended':False,'correct':False,'retrieved':False} for i in range(20)]
    m=metrics(rows);assert m['top1_precision'] is None and m['top1_precision_95_ci'] is None
    assert m['no_match_false_recommendation_95_ci_entity_wilson'][1]>0


def test_entity_splits_and_entire_no_match_removal_enforced():
    m=json.loads((ROOT/'ml/datasets/phone-sim-v1/manifest.json').read_text());assert not validate_manifest(m)['frozen_test_eligible']
    duplicate=copy.deepcopy(m);duplicate['partitions']['test'].append(duplicate['partitions']['train'][0])
    with pytest.raises(ValueError,match='ENTITY_SPLIT_LEAKAGE'):validate_manifest(duplicate)
    bad=copy.deepcopy(m);bad['catalog'].append({'id':'leak','entity_id':m['provenance']['removed_entities'][0],'fields':{'name':'leak'}})
    with pytest.raises(ValueError,match='NO_MATCH_CATALOG_LEAKAGE'):validate_manifest(bad)
