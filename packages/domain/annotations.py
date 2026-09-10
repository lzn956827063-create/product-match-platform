import random
from collections import Counter
from sqlalchemy import select
from .models import *
from .auth import scoped
from .db import uid
from .errors import require
from .services import audit
from ..matching.normalize import digest


def add_task(s,c,dataset,item_id,product_id,entity_key):
    c.permit('admin')
    require(dataset.status=='DRAFT',409,'DATASET_FROZEN','冻结数据集不可增加记录')
    require(dataset.provenance.get('scoring',{}).get('status')!='QUEUED' and not s.scalar(select(SamplingRound.id).where(SamplingRound.org_id==c.org_id,SamplingRound.dataset_id==dataset.id)),409,'POOL_FROZEN','评分中或开始选样后的训练池不可追加记录')
    item=scoped(s,Item,item_id,c);source=scoped(s,Source,item.source_id,c);product=scoped(s,Product,product_id,c);run=scoped(s,Run,item.run_id,c)
    require(product.version_id==run.catalog_version_id,422,'ANNOTATION_PRODUCT','候选商品必须属于该运行标准库')
    old=s.scalar(select(AnnotationTask).where(AnnotationTask.org_id==c.org_id,AnnotationTask.dataset_id==dataset.id,AnnotationTask.item_id==item.id,AnnotationTask.product_id==product.id))
    if old:
        require(old.entity_key==entity_key,409,'ENTITY_FROZEN','已入池候选对的实体分组不可更改');return old
    # A source and its near duplicates must be assigned as an entity by the curator.
    fingerprint=digest({k:source.normalized.get(k) for k in ('brand','model','ram','storage','color','region','pack_count','search_text')})
    duplicate_groups=s.scalars(select(AnnotationTask.entity_key).where(AnnotationTask.org_id==c.org_id,AnnotationTask.dataset_id==dataset.id,AnnotationTask.snapshot['source_fingerprint'].as_string()==fingerprint)).all()
    require(all(g==entity_key for g in duplicate_groups),422,'DUPLICATE_ENTITY_SPLIT','标准化后相同的来源记录必须归入同一实体，防止重复样本跨分区')
    peers=list(s.scalars(select(AnnotationTask).where(AnnotationTask.org_id==c.org_id,AnnotationTask.dataset_id==dataset.id,AnnotationTask.item_id==item.id)))
    require(all(p.entity_key==entity_key for p in peers),422,'ENTITY_GROUP_CONFLICT','同一来源记录的所有候选对必须归入同一实体')
    split=int(digest({'group':entity_key,'seed':dataset.provenance.get('split_seed',42)})[:8],16)%10
    partition='train' if split<6 else 'validation' if split<8 else 'test'
    candidate=s.scalar(select(Candidate).where(Candidate.org_id==c.org_id,Candidate.item_id==item.id,Candidate.product_id==product.id))
    obj=AnnotationTask(id=uid(),org_id=c.org_id,dataset_id=dataset.id,item_id=item.id,product_id=product.id,entity_key=entity_key,partition=partition,snapshot={'source_fingerprint':fingerprint,'source':source.normalized,'source_raw':source.raw,'target':product.normalized,'policy_id':run.policy_id,'run_id':run.id,'review_evidence_id':item.current_decision_id,'suggestion':item.suggestion,'score':candidate.score if candidate else None,'conflicts':candidate.conflicts if candidate else [],'display_mode':'blind_pair_no_recommendation'})
    s.add(obj);s.flush();return obj


def annotate(s,c,task,body,adjudicate=False):
    c.permit('adjudicator' if adjudicate else 'annotator')
    dataset=scoped(s,DatasetVersion,task.dataset_id,c)
    require(dataset.status=='DRAFT',409,'DATASET_FROZEN','冻结数据集不可改写')
    prior=list(s.scalars(select(AnnotationDecision).where(AnnotationDecision.org_id==c.org_id,AnnotationDecision.task_id==task.id)))
    require(not any(x.actor_id==c.user_id for x in prior),409,'ANNOTATION_DUPLICATE','每人对同一候选对只能提交一次独立决定')
    if adjudicate:require(task.status=='DISPUTED' and len(prior)==2,409,'ADJUDICATION_NOT_READY','只有两人分歧的记录可以裁决，裁决者须独立')
    else:require(task.status=='OPEN' and len(prior)<2,409,'ANNOTATION_COMPLETE','此记录已完成独立标注')
    obj=AnnotationDecision(org_id=c.org_id,task_id=task.id,actor_id=c.user_id,kind='adjudication' if adjudicate else 'independent',label=body['label'],reason=body['reason'],seconds=body['seconds']);s.add(obj)
    if adjudicate:task.status,task.label='RESOLVED',body['label']
    elif len(prior)==1:
        if prior[0].label==body['label']:task.status,task.label='RESOLVED',body['label']
        else:task.status='DISPUTED'
    audit(s,c,'annotation.'+obj.kind,task.id,{'label':body['label']})


def freeze(s,c,dataset):
    c.permit('admin')
    if dataset.status=='FROZEN':return dataset
    require(dataset.provenance.get('scoring',{}).get('status')!='QUEUED',409,'SCORING_BUSY','训练池评分尚未完成')
    tasks=list(s.scalars(select(AnnotationTask).where(AnnotationTask.org_id==c.org_id,AnnotationTask.dataset_id==dataset.id).order_by(AnnotationTask.id)))
    require(tasks and all(x.status=='RESOLVED' for x in tasks),409,'LABELS_INCOMPLETE','所有候选对需完成独立双人标注或裁决')
    decisions=list(s.scalars(select(AnnotationDecision).where(AnnotationDecision.org_id==c.org_id,AnnotationDecision.task_id.in_([t.id for t in tasks])).order_by(AnnotationDecision.id)))
    manifest={'provenance':dataset.provenance,'records':[{'task_id':t.id,'entity_key':t.entity_key,'partition':t.partition,'label':t.label,'snapshot':t.snapshot} for t in tasks],'decisions':[{'task_id':d.task_id,'actor_id':d.actor_id,'kind':d.kind,'label':d.label,'reason':d.reason,'seconds':d.seconds,'at':d.created_at} for d in decisions],'counts':dict(Counter(t.partition for t in tasks)),'independent_entities':len({t.entity_key for t in tasks}),'annotation_seconds':sum(d.seconds for d in decisions),'insufficient_info':sum(t.label=='INSUFFICIENT' for t in tasks),'business_admission':'Requires separate whole-policy evaluation; freezing labels never admits a learning policy.'}
    dataset.manifest,dataset.content_hash,dataset.status=manifest,digest(manifest),'FROZEN';audit(s,c,'dataset.freeze',dataset.id,{'content_hash':dataset.content_hash});return dataset


def sample(s,c,dataset,strategy,budget,seed):
    c.permit('admin')
    previous=list(s.scalars(select(SamplingRound).where(SamplingRound.org_id==c.org_id,SamplingRound.dataset_id==dataset.id,SamplingRound.strategy==strategy,SamplingRound.seed==seed)))
    used={ident for r in previous for ident in r.task_ids}
    pool=list(s.scalars(select(AnnotationTask).where(AnnotationTask.org_id==c.org_id,AnnotationTask.dataset_id==dataset.id,AnnotationTask.partition=='train').order_by(AnnotationTask.id)))
    pool=[t for t in pool if t.id not in used];rng=random.Random(seed+len(previous));rng.shuffle(pool)
    random_count=min(len(pool),max(1,budget//5));picked=pool[:random_count];rest=pool[random_count:]
    if strategy=='uncertainty':
        require(all('model_score' in t.snapshot for t in rest),409,'MODEL_EVIDENCE_REQUIRED','模型不确定性选样需要先完成训练池冻结模型评分')
        rest.sort(key=lambda t:abs(t.snapshot['model_score']-.5))
    elif strategy=='disagreement':
        require(all('model_score' in t.snapshot for t in rest),409,'SHADOW_EVIDENCE_REQUIRED','规则与模型分歧选样需要冻结的影子模型分数，不能用冲突数量冒充分歧')
        rest.sort(key=lambda t:abs((t.snapshot.get('score') or 0)-t.snapshot['model_score']),reverse=True)
    picked+=rest[:max(0,budget-len(picked))]
    require(picked,409,'POOL_EXHAUSTED','训练池已无可选样本')
    obj=SamplingRound(org_id=c.org_id,dataset_id=dataset.id,strategy=strategy,number=len(previous)+1,seed=seed,task_ids=[t.id for t in picked],budget={'requested_pairs':budget,'pairs':len(picked),'entities':len({t.entity_key for t in picked}),'random_pairs':random_count,'actual_annotation_seconds':None,'partition':'train'})
    s.add(obj);s.flush();return obj
