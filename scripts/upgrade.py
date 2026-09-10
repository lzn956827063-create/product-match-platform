"""Back up and migrate SQLite, stamping only a verified known schema."""
import argparse
import importlib.util
import json
import tempfile
from pathlib import Path
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.autogenerate import compare_metadata
from alembic.operations import Operations
from sqlalchemy import MetaData,create_engine,inspect
from packages.domain.db import Base,engine
from packages.domain import models
from packages.domain.config import ROOT
from scripts.backup import create


def main(backup):
    if engine.dialect.name!='sqlite':raise ValueError('Use the documented PostgreSQL backup, then alembic upgrade head')
    config=Config(str(ROOT/'alembic.ini'))
    tables=set(inspect(engine).get_table_names())
    if not tables:command.upgrade(config,'head');return {'migration':'fresh'}
    create(backup)
    if 'alembic_version' not in tables:
        with engine.connect() as connection:
            current=compare_metadata(MigrationContext.configure(connection),Base.metadata)
        if not current:
            command.stamp(config,'head')
        else:
            with tempfile.TemporaryDirectory(prefix='product-match-schema-') as folder:
                reference=create_engine('sqlite:///'+str(Path(folder)/'initial.db'))
                matched=None
                for revision in ('d8c4c3aad74d','f484e73f04f0','9b7798dcda5b'):
                    module_path=next((ROOT/'db/migrations/versions').glob(revision+'_*.py'));spec=importlib.util.spec_from_file_location('known_migration',module_path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
                    with reference.begin() as connection:
                        with Operations.context(MigrationContext.configure(connection)):module.upgrade()
                    expected=MetaData();expected.reflect(reference)
                    with engine.connect() as connection:changes=compare_metadata(MigrationContext.configure(connection),expected)
                    if not changes:matched=revision;break
                reference.dispose()
                if not matched:raise ValueError('Unknown legacy schema; backup preserved. Inspect migration differences before stamping: '+repr(changes)[:500])
            command.stamp(config,matched)
    command.upgrade(config,'head')
    with engine.connect() as connection:
        differences=compare_metadata(MigrationContext.configure(connection),Base.metadata)
        if differences:raise ValueError('Schema drift after migration: '+repr(differences))
        head=MigrationContext.configure(connection).get_current_revision()
    return {'migration':'verified','backup':str(Path(backup).resolve()),'head':str(head)}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--backup',required=True);a=p.parse_args();print(json.dumps(main(a.backup),ensure_ascii=False))
