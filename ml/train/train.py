"""Frozen official pair splits, held-out calibration, fixed search budget and three seeds.

Public mixed-category benchmark results do not admit a smartphone SKU production policy.
"""
import argparse
import csv
import hashlib
import json
import platform
import time
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, f1_score, precision_score, recall_score
from sklearn.model_selection import GroupShuffleSplit

from packages.matching.engine import FEATURE_NAMES, FEATURE_VERSION, features, rule_score
from packages.matching.normalize import RULE_VERSION, digest, normalize


def read(path):
    with open(path,encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))


def metric(y,score,threshold):
    pred=np.asarray(score)>=threshold
    return {'n':len(y),'positives':int(np.sum(y)),'predicted_positive':int(np.sum(pred)),'precision':float(precision_score(y,pred,zero_division=0)),'recall':float(recall_score(y,pred,zero_division=0)),'f1':float(f1_score(y,pred,zero_division=0)),'average_precision':float(average_precision_score(y,score)),'threshold':float(threshold)}


def threshold(y,scores):
    choices=np.linspace(0,1,201)
    return max(choices,key=lambda t:(f1_score(y,np.asarray(scores)>=t,zero_division=0),t))


def platt_fit(y,scores):
    logits=np.log(np.clip(scores,1e-6,1-1e-6)/np.clip(1-np.asarray(scores),1e-6,1))[:,None]
    model=LogisticRegression(random_state=42).fit(logits,y)
    return {'coef':float(model.coef_[0,0]),'intercept':float(model.intercept_[0])}


def calibrated(scores,cal):
    logits=np.log(np.clip(scores,1e-6,1-1e-6)/np.clip(1-np.asarray(scores),1e-6,1))
    return 1/(1+np.exp(-(logits*cal['coef']+cal['intercept'])))


def group_interval(y,scores,t,groups,seed=42):
    rng=np.random.default_rng(seed);unique=np.unique(groups);values=[]
    members={g:np.where(groups==g)[0] for g in unique}
    for _ in range(400):
        idx=np.concatenate([members[g] for g in rng.choice(unique,len(unique),replace=True)])
        pred=scores[idx]>=t
        if pred.sum():values.append(float(np.sum((y[idx]==1)&pred)/pred.sum()))
    return np.quantile(values,[.025,.975]).tolist() if values else None


def main(data_dir,output):
    import lightgbm as lgb
    data_dir,output=Path(data_dir),Path(output);output.mkdir(parents=True,exist_ok=True)
    started=time.perf_counter()
    manifest=json.loads((data_dir/'manifest.json').read_text())
    for f,info in manifest['files'].items():
        if digest((data_dir/f).read_bytes())!=info['sha256']:raise ValueError('DATA_HASH_MISMATCH: '+f)
    left_raw={r['id']:r for r in read(data_dir/'tableA.csv')};right_raw={r['id']:r for r in read(data_dir/'tableB.csv')}
    left={k:normalize({'name':v['name']}) for k,v in left_raw.items()};right={k:normalize({'name':v['name']}) for k,v in right_raw.items()}
    pairs={n:read(data_dir/f'{n}.csv') for n in ['train','valid','test']}
    # Fit lexical preprocessing only on text referenced by training pairs.
    train_text=list({left[r['ltable_id']]['search_text'] for r in pairs['train']}|{right[r['rtable_id']]['search_text'] for r in pairs['train']})
    tfidf=TfidfVectorizer(analyzer='char',ngram_range=(2,4),max_features=80000,dtype=np.float32).fit(sorted(train_text))
    lv={k:tfidf.transform([v['search_text']]) for k,v in left.items()};rv={k:tfidf.transform([v['search_text']]) for k,v in right.items()}
    arrays={}
    for split,rows in pairs.items():
        xs=[];ys=[];baseline=[];groups=[]
        for r in rows:
            lid,rid=r['ltable_id'],r['rtable_id'];sim=float(lv[lid].multiply(rv[rid]).sum())
            fs=features(left[lid],right[rid],sim)
            xs.append([fs[k] for k in FEATURE_NAMES]);baseline.append(rule_score(fs));ys.append(int(r['label']));groups.append(lid)
        arrays[split]=(np.array(xs),np.array(ys),np.array(baseline),np.array(groups))
    xtr,ytr,_,_=arrays['train'];xv,yv,bv,gv=arrays['valid'];xt,yt,bt,gt=arrays['test']
    tuning,rest=next(GroupShuffleSplit(n_splits=1,train_size=.5,random_state=42).split(xv,yv,gv))
    cal_local,policy_local=next(GroupShuffleSplit(n_splits=1,train_size=.5,random_state=43).split(xv[rest],yv[rest],gv[rest]))
    cal_idx,policy_idx=rest[cal_local],rest[policy_local]
    split_manifest={'validation_tuning_pair_indices':tuning.tolist(),'validation_calibration_pair_indices':cal_idx.tolist(),'validation_threshold_pair_indices':policy_idx.tolist(),'grouping':'left record ID (official split does not provide complete product-entity IDs)','seed_selection':'Seed 42 predeclared for export; other seeds measure variation only.'}
    (output/'split-manifest.json').write_text(json.dumps(split_manifest,indent=2))
    trials=[];winner=None
    for leaves,depth in [(15,5),(15,8),(31,5),(31,8)]:
        model=lgb.LGBMClassifier(n_estimators=350,num_leaves=leaves,max_depth=depth,learning_rate=.05,random_state=42,n_jobs=2,verbosity=-1,min_child_samples=15)
        model.fit(xtr,ytr,eval_set=[(xv[tuning],yv[tuning])],callbacks=[lgb.early_stopping(30,verbose=False)])
        score=average_precision_score(yv[tuning],model.predict_proba(xv[tuning])[:,1])
        trials.append({'num_leaves':leaves,'max_depth':depth,'best_iteration':model.best_iteration_,'validation_average_precision':float(score)})
        if winner is None or score>winner[0]:winner=(score,leaves,depth)
    seed_results=[];exported=None
    for seed in [42,43,44]:
        model=lgb.LGBMClassifier(n_estimators=350,num_leaves=winner[1],max_depth=winner[2],learning_rate=.05,random_state=seed,n_jobs=2,verbosity=-1,min_child_samples=15,subsample=.9,subsample_freq=1,colsample_bytree=.9)
        model.fit(xtr,ytr,eval_set=[(xv[tuning],yv[tuning])],callbacks=[lgb.early_stopping(30,verbose=False)])
        raw_valid=model.predict_proba(xv)[:,1]
        calibration=platt_fit(yv[cal_idx],raw_valid[cal_idx])
        t=threshold(yv[policy_idx],calibrated(raw_valid[policy_idx],calibration))
        raw_test=model.predict_proba(xt)[:,1];scores=calibrated(raw_test,calibration)
        result={'seed':seed,**metric(yt,scores,t),'precision_95_ci_group_bootstrap':group_interval(yt,scores,t,gt)}
        seed_results.append(result)
        if seed==42:exported=(model,calibration,t,scores,raw_valid,raw_test)
    model,calibration,t,scores,raw_valid,raw_test=exported
    model.booster_.save_model(str(output/'model.txt'))
    import joblib
    joblib.dump(tfidf,output/'tfidf.joblib')
    rule_t=threshold(yv[policy_idx],bv[policy_idx])
    baseline=metric(yt,bt,rule_t)
    # Ablations re-fit on the same split, selecting only on tuning/threshold partitions.
    ablations=[]
    for name,indices in [('without_specs',[i for i,f in enumerate(FEATURE_NAMES) if not any(f.startswith(k+'_') for k in ['ram','storage','color','region','pack_count']) and f!='coverage']),('name_features_only',[0,1,2])]:
        ab=lgb.LGBMClassifier(n_estimators=350,num_leaves=winner[1],max_depth=winner[2],learning_rate=.05,random_state=42,n_jobs=2,verbosity=-1,min_child_samples=15)
        ab.fit(xtr[:,indices],ytr,eval_set=[(xv[tuning][:,indices],yv[tuning])],callbacks=[lgb.early_stopping(30,verbose=False)])
        sv=ab.predict_proba(xv[policy_idx][:,indices])[:,1];st=ab.predict_proba(xt[:,indices])[:,1]
        ablations.append({'name':name,**metric(yt,st,threshold(yv[policy_idx],sv))})
    ablations.append({'name':'without_calibration',**metric(yt,raw_test,threshold(yv[policy_idx],raw_valid[policy_idx]))})
    # Official split can share products; explicitly measure record overlap, not call it unseen-entity performance.
    overlap={side:len(set(r[side] for r in pairs['train'])&set(r[side] for r in pairs['test'])) for side in ['ltable_id','rtable_id']}
    results={'dataset':manifest,'protocol':'Official Abt-Buy labeled pair classification; test labels are used only for final metrics and error reporting. No retrieval or business effectiveness claims.','model_admission':'EXPERIMENTAL: public pair test alone does not meet smartphone SKU end-to-end release gate. Production remains rules.','feature_schema':FEATURE_VERSION,'feature_names':FEATURE_NAMES,'rule_version':RULE_VERSION,'seeds':seed_results,'rule_baseline':baseline,'ablations':ablations,'hyperparameter_trials':trials,'search_budget':4,'training_rows':len(ytr),'training_positives':int(ytr.sum()),'resampling':'none; no class weighting','calibration':calibration,'classification_threshold':float(t),'mean_f1':float(np.mean([r['f1'] for r in seed_results])),'std_f1':float(np.std([r['f1'] for r in seed_results])),'train_test_record_overlap':overlap,'uncertainty':'Bootstrap groups by left record ID; complete entity grouping unavailable, so intervals are exploratory.','elapsed_seconds':time.perf_counter()-started,'runtime':{'python':platform.python_version(),'lightgbm':lgb.__version__,'numpy':np.__version__,'platform':platform.platform()},'model_sha256':digest((output/'model.txt').read_bytes()),'tfidf_sha256':digest((output/'tfidf.joblib').read_bytes()),'split_manifest_sha256':digest((output/'split-manifest.json').read_bytes())}
    results['training_code_sha256'] = digest(Path(__file__).read_bytes())
    lock = Path(__file__).resolve().parents[2] / 'requirements.lock'
    results['dependency_lock_sha256'] = digest(lock.read_bytes()) if lock.exists() else None
    (output/'metrics.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
    errors=[{'pair_index':i,'left_id':r['ltable_id'],'right_id':r['rtable_id'],'label':int(yt[i]),'score':float(scores[i]),'predicted':int(scores[i]>=t)} for i,r in enumerate(pairs['test']) if int(scores[i]>=t)!=yt[i]]
    (output/'errors.json').write_text(json.dumps(errors,indent=2))
    (output/'data-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    print(json.dumps({k:results[k] for k in ['rule_baseline','seeds','mean_f1','std_f1','elapsed_seconds','model_admission']},ensure_ascii=False,indent=2))
    return results


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--data',default='ml/data/downloads/abt-buy');parser.add_argument('--output',default='models/abt-buy-v1');args=parser.parse_args();main(args.data,args.output)
