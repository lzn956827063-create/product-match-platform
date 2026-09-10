"""Evaluate and register a full frozen policy; API promotion independently rechecks its gate."""
import argparse
import json
from pathlib import Path
from sqlalchemy import select
from packages.domain.db import initialize,transaction
from packages.domain.models import Organization,PolicyEvaluation
from packages.matching.policy import validate_bundle,admission
from packages.matching.normalize import digest
from ml.eval.retrieval import evaluate


def main(bundle,dataset,org_id,name):
    config=json.loads(Path(bundle).read_text());manifest=json.loads(Path(dataset).read_text());validate_bundle(config)
    if config['evaluation_dataset_hash']!=digest(manifest) or config['evaluation_dataset_version']!=manifest.get('version'):raise ValueError('DATASET_BUNDLE_MISMATCH')
    report=evaluate(manifest,config,'test');report['admission']=admission(report,config)
    initialize()
    with transaction(write=True) as s:
        if not s.get(Organization,org_id):raise ValueError('ORGANIZATION_NOT_FOUND')
        old=s.scalar(select(PolicyEvaluation).where(PolicyEvaluation.org_id==org_id,PolicyEvaluation.report_hash==digest(report)))
        if old:return old.id
        evaluation=PolicyEvaluation(org_id=org_id,name=name,config=config,report=report,report_hash=digest(report));s.add(evaluation);s.flush();ident=evaluation.id
    print(json.dumps({'id':ident,'admission':report['admission']},ensure_ascii=False));return ident


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('bundle');p.add_argument('dataset');p.add_argument('--org-id',required=True);p.add_argument('--name',required=True);a=p.parse_args();main(a.bundle,a.dataset,a.org_id,a.name)
