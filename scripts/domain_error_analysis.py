"""Analyze authorized, full-catalog human truth. Never turns candidate-pair labels into retrieval truth."""
import argparse,json
from collections import Counter
from pathlib import Path


def analyze(document,budget):
    if document.get('source_type') not in ('authorized','public') or not document.get('source_evidence','').strip():raise ValueError('需要可使用的目标领域数据及来源依据；构造数据不用于本报告')
    if not document.get('catalog_hash') or not document.get('dataset_hash'):raise ValueError('必须冻结标准库及数据集摘要')
    rows=document.get('records',[])
    if not rows:raise ValueError('没有已核验记录；请先采集真实标签')
    groups={};ids=set();buckets=Counter();matches=recalled=predicted=correct=0
    for row in rows:
        if row['query_id'] in ids:raise ValueError('查询编号重复')
        ids.add(row['query_id'])
        group=row['entity_or_near_duplicate_group'];partition=row['partition']
        if partition not in ('train','validation','test'):raise ValueError('分区必须为 train / validation / test')
        if group in groups and groups[group]!=partition:raise ValueError('实体或近重复组跨分区泄漏')
        groups[group]=partition
        if not row.get('full_catalog_checked') or not row.get('truth_evidence') or not row.get('reviewer'):raise ValueError('候选对标签不足以代表全库真值，请补齐独立全库核验依据')
        truth=set(row['all_equivalent_product_ids']);candidates=row['candidate_ids'][:budget];decision=row.get('decision_product_id')
        if len(candidates)!=len(set(candidates)):raise ValueError('候选包含重复商品')
        if partition!='test':continue
        matches+=bool(truth);recalled+=bool(truth&set(candidates));predicted+=bool(decision);correct+=bool(decision and decision in truth)
        diagnosis=row.get('manual_diagnosis')
        if diagnosis in ('extraction_error','missing_information','catalog_coverage'):bucket=diagnosis
        elif truth and not truth&set(candidates):bucket='recall_miss'
        elif truth and candidates and candidates[0] not in truth:bucket='ranking_error'
        elif decision and decision not in truth:bucket='wrong_confirmation'
        else:bucket='no_observed_error'
        buckets[bucket]+=1
    return {'qualification':'EXPERIMENTAL; source permissions and full-catalog truth are reviewer attestations, not independently verified by platform','catalog_hash':document['catalog_hash'],'dataset_hash':document['dataset_hash'],'candidate_budget':budget,'test_queries':sum(buckets.values()),'error_buckets':dict(buckets),'full_catalog_recall':{'numerator':recalled,'denominator':matches,'rate':recalled/matches if matches else None},'confirmation_precision':{'numerator':correct,'denominator':predicted,'rate':correct/predicted if predicted else None},'entity_groups':len(groups),'note':'比较策略时固定数据划分、全库真值、候选预算及准入精度；人工耗时另行记录。本工具不训练或自动准入模型。'}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--input',required=True);p.add_argument('--output',required=True);p.add_argument('--candidate-budget',type=int,default=20);a=p.parse_args()
    if a.candidate_budget<1:raise SystemExit('candidate-budget must be positive')
    result=analyze(json.loads(Path(a.input).read_text()),a.candidate_budget);Path(a.output).write_text(json.dumps(result,ensure_ascii=False,indent=2))
