"""Run in a fresh process with DATA_DIR and MODEL_DIR pointing at a restored backup."""
import json
from pathlib import Path
from fastapi.testclient import TestClient
from sqlalchemy import func,select
from apps.api.main import app
from packages.domain.config import DATA_DIR,MODEL_DIR
from packages.domain.db import transaction
from packages.domain.models import Export,ReviewEvent,PolicyEvaluation
from packages.matching.policy import validate_bundle
from packages.matching.engine import load_model,load_vectorizer
from packages.matching.policy import artifact_path
from scripts.backup import verify


def main():
    manifest=verify(DATA_DIR);checks={}
    with TestClient(app) as client:
        def login(email):
            r=client.post('/api/v1/auth/login',json={'email':email,'password':'Demo2026!match'});r.raise_for_status();headers={'Authorization':'Bearer '+r.json()['access_token']};headers['X-Organization-ID']=client.get('/api/v1/memberships',headers=headers).json()['items'][0]['org_id'];return headers
        a,b=login('admin@demo.local'),login('admin@other.local');run=client.get('/api/v1/runs',headers=a).json()['items'][0]
        checks['tenant_isolation']=client.get('/api/v1/runs/'+run['id'],headers=b).status_code==404
        checks['run_read']=client.get('/api/v1/runs/'+run['id']+'/items?details=false',headers=a).status_code==200
        with transaction() as s:
            exports=s.scalars(select(Export).where(Export.org_id==a['X-Organization-ID'],Export.status=='SUCCEEDED')).all();history=s.scalar(select(func.count()).select_from(ReviewEvent).where(ReviewEvent.org_id==a['X-Organization-ID']))
            evaluations=s.scalars(select(PolicyEvaluation).where(PolicyEvaluation.org_id==a['X-Organization-ID'])).all()
        checks['history_preserved']=history>=3
        checks['download_preserved']=bool(exports) and all(client.get('/api/v1/exports/'+e.id+'/download',headers=a).status_code==200 for e in exports)
        for e in evaluations:
            validate_bundle(e.config);load_model(str(artifact_path(e.config['model_artifact'])),e.config['model_hash']);load_vectorizer(str(artifact_path(e.config['vectorizer_artifact'])),e.config['vectorizer_hash'])
        checks['portable_model_and_vectorizer']=bool(evaluations) and str(MODEL_DIR.resolve()).startswith(str(DATA_DIR.resolve()))
    assert all(checks.values()),checks
    result={'scope':'Local SQLite restoration to a fresh directory; no production RPO/RTO claim','checks':checks,'verified_files_before_open':len(manifest['files']),'referenced_objects':manifest['referenced_objects'],'restored_history_events':history,'restored_policy_evaluations':len(evaluations),'model_resolution':'MODEL_DIR/<stable-artifact-id>','database_and_objects':'SQLite consistent online backup under application write lock'}
    Path('docs/optimization/restore-verification.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))


if __name__=='__main__':main()
