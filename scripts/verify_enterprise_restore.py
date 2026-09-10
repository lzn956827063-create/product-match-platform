"""Validate an enterprise backup in a fresh process and directory; never sends notifications."""
import argparse,json
from pathlib import Path
from sqlalchemy import select,func
from fastapi.testclient import TestClient
from packages.domain.config import DATA_DIR,MODEL_DIR
from packages.domain.db import transaction
from packages.domain.models import BatchRelease,ReleaseArtifact,Delivery,Integration,ReviewEvent
from packages.domain.integrations import cipher
from packages.matching.normalize import digest
from packages.matching.policy import validate_bundle
from packages.matching.engine import load_model,load_vectorizer
from packages.matching.policy import artifact_path
from scripts.backup import verify
from apps.api.main import app
from workers.deliveries import process_delivery


def main(output,private_config):
    manifest=verify(DATA_DIR);checks={};secret=json.loads(Path(private_config).read_text())
    with TestClient(app) as api:
        def login(domain):
            r=api.post('/api/v1/auth/login',json={'email':'admin@'+domain+'.local','password':'Demo2026!match'});r.raise_for_status();h={'Authorization':'Bearer '+r.json()['access_token']};h['X-Organization-ID']=api.get('/api/v1/memberships',headers=h).json()['items'][0]['org_id'];return h
        a,b=login('demo'),login('other')
        with transaction() as s:
            artifacts=list(s.scalars(select(ReleaseArtifact).where(ReleaseArtifact.org_id==a['X-Organization-ID'],ReleaseArtifact.status=='SUCCEEDED')))
            pending=list(s.scalars(select(Delivery).where(Delivery.org_id==a['X-Organization-ID'],Delivery.status!='APPLIED')))
            history=s.scalar(select(func.count()).select_from(ReviewEvent))
            integration=s.scalar(select(Integration).where(Integration.org_id==a['X-Organization-ID'],Integration.active.is_(True)))
            checks['encrypted_signing_secret_portable']=cipher().decrypt(integration.secret_cipher.encode()).decode()==secret['signing_secret']
        checks['historical_decisions']=history>0;checks['release_files']=bool(artifacts)
        for artifact in artifacts:
            url='/api/v1/releases/'+artifact.release_id+'/artifacts/'+artifact.kind+'/download';r=api.get(url,headers=a)
            checks['release_files'] &= r.status_code==200 and digest(r.content)==artifact.file_hash
            assert api.get(url,headers=b).status_code==404
        checks['tenant_isolation']=bool(artifacts)
        checks['pending_delivery_hold']=bool(pending) and (DATA_DIR/'delivery-restore-hold.json').exists()
        for delivery in pending:
            process_delivery(delivery.id)
            with transaction() as s:
                now=s.get(Delivery,delivery.id);assert (now.status,now.attempts,now.event_id)==(delivery.status,delivery.attempts,delivery.event_id)
        checks['no_unreconciled_notification_sent']=bool(pending)
        config=json.loads((MODEL_DIR/'phone-sim-v1/bundle.json').read_text());validate_bundle(config);load_model(str(artifact_path(config['model_artifact'])),config['model_hash']);load_vectorizer(str(artifact_path(config['vectorizer_artifact'])),config['vectorizer_hash']);checks['model_and_vocabulary_portable']=True
    assert all(checks.values()),checks
    result={'passed':True,'scope':'Isolated local SQLite + local objects restore; original receiver offline and delivery hold enforced. No production RPO/RTO claim.','checks':checks,'verified_files':len(manifest['files']),'release_artifacts':len(artifacts),'history_events':history,'pending_deliveries':len(pending)}
    Path(output).write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',default='docs/enterprise/restore.json');p.add_argument('--private-config',required=True);a=p.parse_args();main(a.output,a.private_config)
