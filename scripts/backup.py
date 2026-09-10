"""Consistent local SQLite + immutable object + model backup, and non-destructive restore."""
import argparse
import json
import shutil
import sqlite3
from pathlib import Path
from sqlalchemy import select,inspect
from packages.domain.config import DATA_DIR,DATABASE_URL,ROOT,STORAGE_BACKEND,MODEL_DIR
from packages.domain.db import transaction,engine
from packages.domain.models import File,Export,ImportJob,ArtifactDeletion,ReleaseArtifact,QuotaReservation
from packages.matching.normalize import digest


def create(destination):
    destination=Path(destination).resolve()
    if destination.exists():raise ValueError('Backup destination must not exist')
    if not DATABASE_URL.startswith('sqlite') or STORAGE_BACKEND!='local':raise ValueError('Use the documented PostgreSQL/MinIO backup procedure for Compose')
    destination.mkdir(parents=True,mode=0o700)
    with transaction(write=True) as s:
        src=sqlite3.connect(str(engine.url.database));dst=sqlite3.connect(str(destination/'app.db'))
        try:src.backup(dst)
        finally:src.close();dst.close()
        shutil.copytree(DATA_DIR/'objects',destination/'objects',dirs_exist_ok=True)
        if (DATA_DIR/'session.key').exists():shutil.copy2(DATA_DIR/'session.key',destination/'session.key')
        shutil.copytree(MODEL_DIR,destination/'models',dirs_exist_ok=True)
        if (ROOT/'ml/datasets').exists():shutil.copytree(ROOT/'ml/datasets',destination/'datasets')
        import os
        recovery_keys={name:os.environ[name] for name in ('JWT_SECRET','INTEGRATION_MASTER_KEY') if os.getenv(name)}
        if recovery_keys:
            keys=destination/'recovery-keys.json';keys.write_text(json.dumps(recovery_keys));keys.chmod(0o600)
        from packages.domain.services import release_hashes
        from packages.matching.normalize import RULE_VERSION
        (destination/'runtime-config.json').write_text(json.dumps({'storage_backend':'local','database_backend':'sqlite','model_dir':'models','rule_version':RULE_VERSION,'release':release_hashes()},indent=2))
        refs={f.object_key:f.sha256 for f in s.scalars(select(File))}
        tables=set(inspect(engine).get_table_names())
        deleted={r.export_id for r in s.scalars(select(ArtifactDeletion).where(ArtifactDeletion.deleted_at.is_not(None)))} if 'artifact_deletions' in tables else set()
        refs.update({e.object_key:e.file_hash for e in s.scalars(select(Export).where(Export.status=='SUCCEEDED')) if e.id not in deleted})
        if 'release_artifacts' in tables:refs.update({a.object_key:a.file_hash for a in s.scalars(select(ReleaseArtifact).where(ReleaseArtifact.status=='SUCCEEDED'))})
        if 'import_jobs' in tables:refs.update({j.result_key:j.result_hash for j in s.scalars(select(ImportJob).where(ImportJob.result_key.is_not(None)))})
    files={str(p.relative_to(destination)):digest(p.read_bytes()) for p in destination.rglob('*') if p.is_file()}
    for key,expected in refs.items():
        if files.get('objects/'+key)!=expected:raise ValueError('Referenced object verification failed')
    manifest={'files':files,'referenced_objects':len(refs),'database':'SQLite consistent online backup','restore':'Restore to a fresh directory, point DATA_DIR at it; set MODEL_DIR to <restored>/models; artifacts use stable relative IDs.'}
    (destination/'manifest.json').write_text(json.dumps(manifest,indent=2));return manifest


def verify(source):
    source=Path(source).resolve();manifest=json.loads((source/'manifest.json').read_text())
    for name,expected in manifest['files'].items():
        p=(source/name).resolve()
        if not p.is_relative_to(source) or digest(p.read_bytes())!=expected:raise ValueError('Backup hash mismatch: '+name)
    conn=sqlite3.connect(f'file:{source}/app.db?mode=ro',uri=True)
    try:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:conn.close()
    return manifest


def restore(source,destination):
    verify(source);destination=Path(destination)
    if destination.exists():raise ValueError('Restore destination must not exist; no live data is overwritten')
    shutil.copytree(source,destination)
    result=verify(destination)
    conn=sqlite3.connect(destination/'app.db')
    try:
        tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        pending=conn.execute("SELECT COUNT(*) FROM deliveries WHERE status!='APPLIED'").fetchone()[0] if 'deliveries' in tables else 0
        if pending:(destination/'delivery-restore-hold.json').write_text(json.dumps({'pending_deliveries':pending,'instruction':'Reconcile downstream application state before running scripts.reconcile_restore'}))
    finally:conn.close()
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='action',required=True)
    c=sub.add_parser('create');c.add_argument('destination')
    v=sub.add_parser('verify');v.add_argument('source')
    r=sub.add_parser('restore');r.add_argument('source');r.add_argument('destination')
    a=p.parse_args();result=create(a.destination) if a.action=='create' else verify(a.source) if a.action=='verify' else restore(a.source,a.destination)
    print(json.dumps({'verified_files':len(result['files']),'referenced_objects':result['referenced_objects']},indent=2))
