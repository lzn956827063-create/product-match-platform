"""Equal-budget selection experiments on controlled, entity-split phone pairs.

Only training labels can be acquired by the selector; test is read once after
all branches, rounds and validation decisions are frozen. No human time is invented.
"""
import argparse,json,random,time
from pathlib import Path
from collections import defaultdict
import joblib
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
import lightgbm as lgb
from packages.matching.engine import Matcher,FEATURE_NAMES,frozen_pair_features
from packages.matching.normalize import normalize,REQUIRED,digest
from ml.eval.retrieval import metrics,validate_manifest
from ml.train.phone_experiment import reliability


def main(a):
    start=time.monotonic();m=json.loads(Path(a.dataset).read_text());validate_manifest(m)
    catalog={p['id']:p for p in m['catalog']};norm={p['id']:normalize(p['fields']) for p in m['catalog']};qn={q['id']:normalize(q['fields']) for q in m['queries']};entities=set(m['partitions']['train'])
    text=sorted({qn[q['id']]['search_text'] for q in m['queries'] if q['partition']=='train'}|{norm[p['id']]['search_text'] for p in m['catalog'] if p['entity_id'] in entities})
    vectorizer=TfidfVectorizer(analyzer='char',ngram_range=(2,4),dtype=np.float32,max_features=80000).fit(text)
    matcher=Matcher([{'id':i,'normalized':n} for i,n in norm.items()]);parts=defaultdict(list)
    for q in m['queries']:
        _,cs=matcher.match(qn[q['id']])
        if q['partition']=='train':cs=[c for c in cs if catalog[c['product_id']]['entity_id'] in entities]
        fs=frozen_pair_features(qn[q['id']],[norm[c['product_id']] for c in cs],vectorizer) if cs else []
        for c,f in zip(cs,fs):parts[q['partition']].append({'q':q,'target':c['product_id'],'x':[f[n] for n in FEATURE_NAMES],'y':int(catalog[c['product_id']]['entity_id']==q['entity_id']),'rule':c['score'],'conflict':bool(c['conflicts']),'missing':bool(c['missing'])})
    train=parts['train'];X=np.array([r['x'] for r in train]);y=np.array([r['y'] for r in train]);fixed_models=[];protocol=[]
    def predict(model,part):return model.predict_proba(np.array([r['x'] for r in parts[part]]))[:,1]
    def recommendations(part,scores,threshold,margin):
        grouped=defaultdict(list)
        for r,score in zip(parts[part],scores):grouped[r['q']['id']].append((r,float(score)))
        output=[]
        for q in m['queries']:
            if q['partition']!=part:continue
            cs=sorted(grouped[q['id']],key=lambda t:(t[0]['conflict'],-t[1],t[0]['target']))
            good=[t for t in cs if not t[0]['conflict']]
            recommended=bool(good and not good[0][0]['missing'] and good[0][1]>=threshold and good[0][1]-(good[1][1] if len(good)>1 else 0)>=margin)
            output.append({'id':q['id'],'entity_id':q['entity_id'],'matchable':q['catalog_exists'],'retrieved':any(r['y'] for r,_ in cs),'recommended':recommended,'correct':recommended and bool(good[0][0]['y'])})
        return output
    for seed in a.seeds:
        rng=random.Random(seed);initial=rng.sample(range(len(train)),min(a.initial,len(train)))
        if len(set(y[initial]))<2:raise ValueError('Initial random pool lacks both classes; increase the common initial budget')
        for strategy in ('random','uncertainty','disagreement'):
            acquired=list(initial);rng=random.Random(seed);round_reports=[]
            for round_no in range(a.rounds+1):
                model=lgb.LGBMClassifier(n_estimators=80,num_leaves=7,max_depth=4,min_child_samples=5,n_jobs=1,random_state=seed,verbosity=-1)
                model.fit(X[acquired],y[acquired]);val=predict(model,'threshold');options=[]
                for threshold in (.8,.85,.9,.92,.94,.96,.98,.99,1.):
                    for margin in (0.,.02,.08):
                        scored=metrics(recommendations('threshold',val,threshold,margin),False)
                        if scored['top1_precision'] is not None and scored['top1_precision']>=.98:options.append((scored['coverage'],threshold,margin,scored))
                selected=max(options,key=lambda t:(t[0],t[1],t[2])) if options else (0,1.,1.,{})
                round_reports.append({'round':round_no,'labelled_training_pairs':len(acquired),'independent_training_entities':len({train[i]['q']['entity_id'] for i in acquired}),'validation':selected[3],'threshold':selected[1],'margin':selected[2],'actual_human_minutes':None})
                if round_no==a.rounds:
                    fixed_models.append((seed,strategy,model,selected[1],selected[2],list(acquired)));break
                remaining=[i for i in range(len(train)) if i not in set(acquired)];rng.shuffle(remaining);n=min(a.budget,len(remaining));random_n=max(1,n//5);chosen=remaining[:random_n];rest=remaining[random_n:]
                if strategy!='random':
                    scores=model.predict_proba(X[rest])[:,1]
                    order=np.argsort(np.abs(scores-.5)) if strategy=='uncertainty' else np.argsort(-np.abs(scores-np.array([train[i]['rule'] for i in rest])))
                    rest=[rest[int(i)] for i in order]
                acquired+=chosen+rest[:n-random_n]
            protocol.append({'seed':seed,'strategy':strategy,'initial_ids_hash':digest(initial),'rounds':round_reports,'selected_training_pairs_hash':digest(acquired)})
    # Final round, thresholds and training sets are fixed before first test prediction.
    frozen_protocol_hash=digest(protocol);final=[]
    for seed,strategy,model,threshold,margin,acquired in fixed_models:
        # Common calibration pool never participates in active selection.
        raw=predict(model,'calibration');clipped=np.clip(raw,1e-6,1-1e-6);cal=LogisticRegression(random_state=seed).fit(np.log(clipped/(1-clipped))[:,None],np.array([r['y'] for r in parts['calibration']]))
        scores=predict(model,'test');test_y=np.array([r['y'] for r in parts['test']]);score_metrics=metrics(recommendations('test',scores,threshold,margin))
        logits=np.log(np.clip(scores,1e-6,1-1e-6)/np.clip(1-scores,1e-6,1));calibrated=cal.predict_proba(logits[:,None])[:,1]
        final.append({'seed':seed,'strategy':strategy,'training_pairs':len(acquired),'metrics':score_metrics,'raw_calibration':reliability(test_y,scores),'platt_diagnostic':reliability(test_y,calibrated),'calibration_scope':'Platt is diagnostic only; recommendation thresholds use the predeclared raw scores.','actual_human_minutes':None})
    report={'scope':'controlled_simulation_only','dataset_hash':digest(m),'code_hash':digest(Path(__file__).read_bytes()),'frozen_protocol_hash_before_test':frozen_protocol_hash,'precision_constraint':.98,'candidate_budget':20,'initial_pairs':a.initial,'round_budget':a.budget,'rounds':a.rounds,'seeds':a.seeds,'fixed_evaluation_pair_counts':{k:len(v) for k,v in parts.items() if k!='train'},'sampling':'Only train partition queries and train target entities; 20% random budget preserved. Same initial labels and model hyperparameters per seed; independent training per branch. Test labels inaccessible to selectors.','annotation_time':'No human annotation took place; minutes are null, never estimated from pair count.','eligibility':'EXPERIMENTAL; independent authorized phone labels and actual annotation cost still required.','protocol':protocol,'final_test':final,'elapsed_seconds':time.monotonic()-start,'conclusion':'Report all branches including ties and negative results. This experiment does not promote a business policy.'}
    Path(a.output).write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps({'elapsed_seconds':report['elapsed_seconds'],'final':[{'seed':r['seed'],'strategy':r['strategy'],**r['metrics']} for r in final]},indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--dataset',default='ml/datasets/phone-sim-v1/manifest.json');p.add_argument('--initial',type=int,default=100);p.add_argument('--budget',type=int,default=100);p.add_argument('--rounds',type=int,default=3);p.add_argument('--seeds',type=int,nargs='+',default=[17,42,89]);p.add_argument('--output',default='docs/enterprise/active-learning.json');main(p.parse_args())
