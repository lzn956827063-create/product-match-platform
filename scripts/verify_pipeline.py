"""Numeric parity with the scoring kernel used by workers, not HTTP load evidence."""
import json
from pathlib import Path
import numpy as np
from packages.matching.engine import Matcher,FEATURE_NAMES,frozen_pair_features,load_model,load_vectorizer
from packages.matching.normalize import normalize
from packages.matching.policy import artifact_path,validate_bundle


def main(output='docs/optimization/pipeline-parity.json'):
    config=json.loads(Path('models/phone-sim-v1/bundle.json').read_text());validate_bundle(config)
    manifest=json.loads(Path('ml/datasets/phone-sim-v1/manifest.json').read_text());products=[{'id':p['id'],'normalized':normalize(p['fields'])} for p in manifest['catalog']];by_id={p['id']:p['normalized'] for p in products}
    matcher=Matcher(products,config);vectorizer=load_vectorizer(str(artifact_path(config['vectorizer_artifact'])),config['vectorizer_hash']);model=load_model(str(artifact_path(config['model_artifact'])),config['model_hash'])
    feature_error=score_error=0.;pairs=0;states={}
    for q in manifest['queries'][::37]:
        source=normalize(q['fields']);state,rows=matcher.match(source);fs=frozen_pair_features(source,[by_id[r['product_id']] for r in rows],vectorizer)
        x=np.array([[f[k] for k in FEATURE_NAMES] for f in fs]);actual=np.array([[r['features'][k] for k in FEATURE_NAMES] for r in rows]);feature_error=max(feature_error,float(np.abs(x-actual).max()))
        scores=model.predict(x,num_threads=1);cal=config['calibration'];logits=np.log(np.clip(scores,1e-6,1-1e-6)/np.clip(1-scores,1e-6,1));scores=1/(1+np.exp(-(logits*cal['coef']+cal['intercept'])));score_error=max(score_error,float(np.abs(scores-[r['score'] for r in rows]).max()))
        ordered=sorted(zip(rows,scores),key=lambda pair:(bool(pair[0]['conflicts']),-pair[1],pair[0]['product_id']));assert [r['product_id'] for r,_ in ordered]==[r['product_id'] for r in rows]
        good=[(r,score) for r,score in ordered if not r['conflicts']]
        expected='NO_CANDIDATE' if not rows else 'CONFLICT' if not good else 'REVIEW' if good[0][0]['missing'] else 'RECOMMENDED' if good[0][1]>=config['high_threshold'] and good[0][1]-(good[1][1] if len(good)>1 else 0)>=config['margin'] else 'REVIEW'
        assert state==expected;pairs+=len(rows);states[state]=states.get(state,0)+1
    assert max(feature_error,score_error)<=1e-6
    result={'scope':'Offline frozen-vector features + direct Booster scores vs production Matcher kernel used by worker and shadow jobs','inputs':len(manifest['queries'][::37]),'candidate_pairs':pairs,'maximum_feature_absolute_error':feature_error,'maximum_score_absolute_error':score_error,'tolerance':1e-6,'ranking_and_status_equal':True,'states':states,'bundle_hash':config['bundle_hash'],'model_hash':config['model_hash'],'vectorizer_hash':config['vectorizer_hash']}
    Path(output).write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2));return result


if __name__=='__main__':main()
