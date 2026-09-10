"""Consistent local SQLite + immutable object + model backup, and non-destructive restore."""
import argparse
import json
import shutil
import sqlite3
from pathlib import Path
from sqlalchemy import select
from packages.domain.config import DATA_DIR,DATABASE_URL,ROOT,STORAGE_BACKEND
from packages.domain.db import transaction
from packages.domain.models import File,Export
from packages.matching.normalize import digest


def create(destination):
    destination=Path(destination).resolve()
    if destination.exists():raise ValueError('Backup destination must not exist')
    if not DATABASE_URL.startswith('sqlite') or STORAGE_BACKEND!='local':raise ValueError('Use the documented PostgreSQL/MinIO backup procedure for Compose')
    destination.mkdir(parents=True,mode=0o700)
    with transaction(write=True) as s:
        src=sqlite3.connect(str(DATA_DIR/'app.db'));dst=sqlite3.connect(str(destination/'app.db'))
        try:src.backup(dst)
        finally:src.close();dst.close()
        shutil.copytree(DATA_DIR/'objects',destination/'objects',dirs_exist_ok=True)
        if (DATA_DIR/'session.key').exists():shutil.copy2(DATA_DIR/'session.key',destination/'session.key')
        shutil.copytree(ROOT/'models',destination/'models',dirs_exist_ok=True)
        refs={f.object_key:f.sha256 for f in s.scalars(select(File))}
        refs.update({e.object_key:e.file_hash for e in s.scalars(select(Export).where(Export.status=='SUCCEEDED'))})
    files={str(p.relative_to(destination)):digest(p.read_bytes()) for p in destination.rglob('*') if p.is_file()}
    for key,expected in refs.items():
        if files.get('objects/'+key)!=expected:raise ValueError('Referenced object verification failed')
    manifest={'files':files,'referenced_objects':len(refs),'database':'SQLite consistent online backup','restore':'Restore to a fresh directory, point DATA_DIR at it; restore model bundle to the recorded release path.'}
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
    return verify(destination)


if __name__=='__main__':
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='action',required=True)
    c=sub.add_parser('create');c.add_argument('destination')
    v=sub.add_parser('verify');v.add_argument('source')
    r=sub.add_parser('restore');r.add_argument('source');r.add_argument('destination')
    a=p.parse_args();result=create(a.destination) if a.action=='create' else verify(a.source) if a.action=='verify' else restore(a.source,a.destination)
    print(json.dumps({'verified_files':len(result['files']),'referenced_objects':result['referenced_objects']},indent=2))
