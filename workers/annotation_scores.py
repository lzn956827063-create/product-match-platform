from sqlalchemy import select
from packages.domain.db import transaction
from packages.domain.models import *
from packages.matching.policy import validate_bundle,artifact_path
from packages.matching.engine import FEATURE_NAMES,frozen_pair_features,load_model,load_vectorizer
import numpy as np


def authorized(s,org,user):
    member=s.scalar(select(Membership).join(User,User.id==Membership.user_id).where(Membership.org_id==org,Membership.user_id==user,Membership.active.is_(True),User.active.is_(True)))
    if not member or 'admin' not in member.roles:raise PermissionError('ACTOR_DISABLED')


def process_scores(ident):
    with transaction() as s:
        d=s.get(DatasetVersion,ident)
        if not d or d.status!='DRAFT' or d.provenance.get('scoring',{}).get('status')!='QUEUED':return
        definition=d.provenance['scoring'];org=d.org_id
        e=s.scalar(select(PolicyEvaluation).where(PolicyEvaluation.org_id==org,PolicyEvaluation.id==definition['evaluation_id']))
        config=e.config if e else None
        tasks=[(t.id,t.snapshot) for t in s.scalars(select(AnnotationTask).where(AnnotationTask.org_id==org,AnnotationTask.dataset_id==ident,AnnotationTask.partition=='train'))]
    try:
        with transaction() as s:authorized(s,org,definition['requested_by'])
        if not config:raise ValueError('EVALUATION_MISSING')
        validate_bundle(config);v=load_vectorizer(str(artifact_path(config['vectorizer_artifact'])),config['vectorizer_hash']);model=load_model(str(artifact_path(config['model_artifact'])),config['model_hash'])
        features=[frozen_pair_features(x['source'],[x['target']],v)[0] for _,x in tasks]
        scores=model.predict(np.array([[f[k] for k in FEATURE_NAMES] for f in features]),num_threads=1) if features else []
        cal=config.get('calibration')
        if cal and len(scores):
            scores=np.clip(scores,1e-6,1-1e-6);scores=1/(1+np.exp(-(np.log(scores/(1-scores))*cal['coef']+cal['intercept'])))
        with transaction(write=True) as s:
            s.scalar(select(Organization).where(Organization.id==org).with_for_update());d=s.get(DatasetVersion,ident)
            if d.status!='DRAFT' or d.provenance['scoring']!=definition:return
            authorized(s,org,definition['requested_by'])
            for (task_id,_),score in zip(tasks,scores):
                task=s.get(AnnotationTask,task_id);task.snapshot={**task.snapshot,'model_score':float(score),'scoring_bundle_hash':config['bundle_hash']}
            d.provenance={**d.provenance,'scoring':{**definition,'status':'SUCCEEDED','pairs':len(tasks)}}
            for event in s.scalars(select(Outbox).where(Outbox.kind=='annotation_score',Outbox.resource_id==ident)):event.completed=True
    except Exception as exc:
        with transaction(write=True) as s:
            d=s.get(DatasetVersion,ident)
            if d and d.provenance.get('scoring')==definition:
                d.provenance={**d.provenance,'scoring':{**definition,'status':'FAILED','error':type(exc).__name__}}
                for event in s.scalars(select(Outbox).where(Outbox.kind=='annotation_score',Outbox.resource_id==ident)):event.completed=True
