import argparse
import json
from pathlib import Path
from sqlalchemy import select
from packages.domain.db import initialize, transaction
from packages.domain.models import ModelVersion
from packages.matching.normalize import digest


def register(path):
    initialize();path=Path(path).resolve();metrics=json.loads((path/'metrics.json').read_text());artifact=path/'model.txt'
    assert digest(artifact.read_bytes())==metrics['model_sha256']
    with transaction(write=True) as s:
        old=s.scalar(select(ModelVersion).where(ModelVersion.artifact_hash==metrics['model_sha256']))
        if old:return old.id
        model=ModelVersion(name=path.name,artifact_path=str(artifact),artifact_hash=metrics['model_sha256'],schema_version=metrics['feature_schema'],metrics=metrics,status='EXPERIMENTAL')
        s.add(model);s.flush();return model.id


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('path');a=p.parse_args();print(register(a.path))
