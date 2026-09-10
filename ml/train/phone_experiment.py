"""Fixed-budget, same-catalog phone experiment. Controlled data cannot admit a policy."""
import argparse
import json
import random
from pathlib import Path
import joblib
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss
from packages.matching.engine import DEFAULT_POLICY, FEATURE_NAMES, FEATURE_VERSION, Matcher, frozen_pair_features
from packages.matching.normalize import RULE_VERSION, REQUIRED, digest, normalize
from packages.matching.policy import make_bundle, policy_hash, admission
from ml.eval.retrieval import evaluate, validate_manifest


def reliability(y,p):
    bins=[];ece=0
    for low in np.linspace(0,.9,10):
        mask=(p>=low)&(p<(low+.1) if low<.9 else p<=1)
        n=int(mask.sum());prediction=float(np.mean(p[mask])) if n else None;observed=float(np.mean(y[mask])) if n else None
        if n:ece+=n/len(y)*abs(prediction-observed)
        bins.append({'from':float(low),'to':float(low+.1),'n':n,'predicted':prediction,'observed':observed})
    return {'brier':float(brier_score_loss(y,p)),'ece_10_equal_width_bins':ece,'reliability':bins,'n':len(y)}


def calibrate(p,cal):
    p=np.clip(p,1e-6,1-1e-6);return 1/(1+np.exp(-(np.log(p/(1-p))*cal['coef']+cal['intercept'])))


def choose_policy(m,config):
    products=[{'id':p['id'],'normalized':normalize(p['fields'])} for p in m['catalog']]
    truth={p['id']:p['entity_id'] for p in m['catalog']}
    matcher=Matcher(products,config);outputs=[]
    for q in m['queries']:
        if q['partition']=='threshold':outputs.append((q,matcher.match(normalize(q['fields']))[1]))
    options=[]
    for threshold in [.80,.85,.90,.92,.94,.96,.98,.99,1.0]:
        for margin in [0.0,.02,.08]:
            rec=[]
            for q,cs in outputs:
                good=[c for c in cs if not c['conflicts']]
                if good and not good[0]['missing'] and good[0]['score']>=threshold and good[0]['score']-(good[1]['score'] if len(good)>1 else 0)>=margin:rec.append(truth[good[0]['product_id']]==q['entity_id'])
            precision=sum(rec)/len(rec) if rec else None
            options.append({'threshold':threshold,'margin':margin,'precision':precision,'coverage':len(rec)/len(outputs),'recommended':len(rec)})
    valid=[v for v in options if v['precision'] is not None and v['precision']>=.98]
    best=max(valid,key=lambda v:(v['coverage'],v['precision'],v['threshold'],v['margin'])) if valid else {'threshold':1.,'margin':1.}
    config={**config,'high_threshold':best['threshold'],'margin':best['margin']}
    if config['engine']=='lightgbm':config['bundle_hash']=policy_hash(config)
    return config,options


def main(dataset,output):
    import lightgbm as lgb
    output=Path(output);output.mkdir(parents=True,exist_ok=True);m=json.loads(Path(dataset).read_text());validate_manifest(m)
    catalog={p['id']:p for p in m['catalog']};norm={p['id']:normalize(p['fields']) for p in m['catalog']};train_entities=set(m['partitions']['train'])
    queries={q['id']:q for q in m['queries']};qn={q['id']:normalize(q['fields']) for q in m['queries']}
    train_text={qn[q['id']]['search_text'] for q in m['queries'] if q['partition']=='train'}|{norm[p['id']]['search_text'] for p in m['catalog'] if p['entity_id'] in train_entities}
    vectorizer=TfidfVectorizer(analyzer='char',ngram_range=(2,4),dtype=np.float32,max_features=80000).fit(sorted(train_text))
    joblib.dump(vectorizer,output/'tfidf.joblib')
    retriever=Matcher([{'id':k,'normalized':n} for k,n in norm.items()]);partitions={};records=[];rng=random.Random(42);sampling={'positive':0,'hard_negative':0,'random_negative':0}
    for q in m['queries']:
        state,cs=retriever.match(qn[q['id']]);selected=[]
        if q['partition']=='train':
            positives=[c for c in cs if catalog[c['product_id']]['entity_id']==q['entity_id']]
            negatives=[c for c in cs if catalog[c['product_id']]['entity_id']!=q['entity_id'] and catalog[c['product_id']]['entity_id'] in train_entities]
            hard=sorted(negatives,key=lambda c:sum(qn[q['id']].get(k)!=norm[c['product_id']].get(k) for k in REQUIRED))[:4]
            remaining=[c for c in negatives if c not in hard];randoms=rng.sample(remaining,min(4,len(remaining)))
            selected=[(c,'positive') for c in positives]+[(c,'hard_negative') for c in hard]+[(c,'random_negative') for c in randoms]
        else:selected=[(c,'evaluation_candidate') for c in cs]
        xs=frozen_pair_features(qn[q['id']],[norm[c['product_id']] for c,_ in selected],vectorizer) if selected else []
        for (c,kind),fs in zip(selected,xs):
            y=int(catalog[c['product_id']]['entity_id']==q['entity_id']);partitions.setdefault(q['partition'],[]).append(([fs[k] for k in FEATURE_NAMES],y,q['id'],c['product_id']))
            if kind in sampling:sampling[kind]+=1;records.append({'query_id':q['id'],'target_id':c['product_id'],'kind':kind,'label':y})
    arrays={k:(np.array([r[0] for r in rows]),np.array([r[1] for r in rows])) for k,rows in partitions.items()}
    x,y=arrays['train'];xv,yv=arrays['tuning'];trials=[];winner=None
    for leaves,depth in [(7,4),(7,6),(15,4),(15,6)]:
        model=lgb.LGBMClassifier(n_estimators=120,num_leaves=leaves,max_depth=depth,n_jobs=2,random_state=42,verbosity=-1,min_child_samples=10)
        model.fit(x,y);ap=float(average_precision_score(yv,model.predict_proba(xv)[:,1]));trials.append({'leaves':leaves,'depth':depth,'tuning_ap':ap})
        if winner is None or ap>winner[0]:winner=(ap,model)
    model=winner[1];model.booster_.save_model(str(output/'model.txt'))
    xc,yc=arrays['calibration'];raw=model.predict_proba(xc)[:,1];raw=np.clip(raw,1e-6,1-1e-6)
    calmodel=LogisticRegression(random_state=42).fit(np.log(raw/(1-raw))[:,None],yc);cal={'coef':float(calmodel.coef_[0,0]),'intercept':float(calmodel.intercept_[0])}
    metrics={'feature_schema':FEATURE_VERSION,'feature_names':FEATURE_NAMES,'rule_version':RULE_VERSION,'model_sha256':digest((output/'model.txt').read_bytes()),'tfidf_sha256':digest((output/'tfidf.joblib').read_bytes()),'calibration':cal,'status':'EXPERIMENTAL','scope':'controlled_simulation','training_sample_counts':sampling,'trials':trials,'candidate_budget':20,'vocabulary_fit':'train query and train catalog entities only','training_entity_count':len(train_entities),'seed':42,'dataset_hash':digest(m)}
    (output/'metrics.json').write_text(json.dumps(metrics,indent=2));(output/'training-samples.json').write_text(json.dumps(records,indent=2))
    bundle=make_bundle(output,digest(m),m['version']);policies={'rules':dict(DEFAULT_POLICY),'text_constraints':{**DEFAULT_POLICY,'engine':'text_constraints'},'lightgbm':bundle};reports={}
    for name,policy in policies.items():
        chosen,budget=choose_policy(m,policy);report=evaluate(m,chosen,'test');holdout=evaluate(m,chosen,'series_holdout')
        reports[name]={'metrics':report['metrics'],'slices':report['slices'],'series_holdout':holdout['metrics'],'policy':chosen,'threshold_search':budget}
        if name=='lightgbm':
            (output/'bundle.json').write_text(json.dumps(chosen,indent=2));report['admission']=admission(report,chosen);(output/'evaluation-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    xt,yt=arrays['test'];raw=model.predict_proba(xt)[:,1]
    reports['calibration']={'raw':reliability(yt,raw),'calibrated':reliability(yt,calibrate(raw,cal)),'scope':'All recalled candidate pairs for held-out test queries; candidate pair metrics are separate from recommendation metrics.'}
    reports['protocol']={'dataset_hash':digest(m),'scope':'controlled_simulation','training':sampling,'budget':4,'candidate_budget':20,'training_code_hash':digest(Path(__file__).read_bytes()),'comparison':'Same frozen entity splits, same full catalog retrieval, same identity guards, precision-first thresholds chosen only on threshold partition. No business deployment claim.','no_match_scenarios':'Observed test fraction is controlled. Do not interpret coverage as production prevalence.'}
    Path('docs/optimization/phone-experiment.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2))
    print(json.dumps({k:v['metrics'] for k,v in reports.items() if 'metrics' in v},indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--dataset',default='ml/datasets/phone-sim-v1/manifest.json');p.add_argument('--output',default='models/phone-sim-v1');a=p.parse_args();main(a.dataset,a.output)
