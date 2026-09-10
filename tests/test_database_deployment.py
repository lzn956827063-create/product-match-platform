import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


def test_acceptance_dataset_is_repeatable_and_truth_is_independent(tmp_path):
    from scripts.acceptance_dataset import generate, verify

    first = tmp_path / "first"
    second = tmp_path / "second"
    changed = tmp_path / "changed"
    a = generate(first, 40, 40, 2, 10, 1309)
    b = generate(second, 40, 40, 2, 10, 1309)
    c = generate(changed, 40, 40, 2, 10, 1310)
    assert a["files"] == b["files"]
    assert a["truth_distribution"] == {
        "unique_or_normalized": {"rows": 56, "ratio": 0.7},
        "composite": {"rows": 12, "ratio": 0.15},
        "insufficient": {"rows": 8, "ratio": 0.1},
        "conflict": {"rows": 4, "ratio": 0.05},
    }
    assert a["files"]["org-1/catalog.csv"]["sha256"] != c["files"]["org-1/catalog.csv"]["sha256"]
    truths = [json.loads(line) for line in (first / "org-1/truth.jsonl").read_text().splitlines()]
    assert len(truths) == 40 and all("expected_outcome" in row and "reason" in row for row in truths)
    assert verify(first)["generator_version"] == "acceptance-dataset-v1"


def test_acceptance_dataset_verifier_rejects_changed_bytes(tmp_path):
    from scripts.acceptance_dataset import generate, verify

    folder = tmp_path / "dataset"
    generate(folder, 20, 20, 2, 2, 1309)
    with (folder / "org-1/source.csv").open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify(folder)


def test_secret_file_database_configuration_is_url_encoded(tmp_path):
    password = tmp_path / "database-password"
    password.write_text("test-only@password:/value")
    environment = os.environ.copy()
    environment.pop("DATABASE_URL", None)
    environment.update({
        "DATA_DIR": str(tmp_path / "data"),
        "DB_PASSWORD_FILE": str(password),
        "DB_USER": "productmatch app",
        "DB_HOST": "db.internal",
        "DB_NAME": "product match",
    })
    result = subprocess.run(
        [sys.executable, "-c", "from packages.domain.config import DATABASE_URL; print(DATABASE_URL)"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.stdout.strip() == "postgresql+psycopg://productmatch+app:test-only%40password%3A%2Fvalue@db.internal:5432/product+match"


def test_init_env_preserves_existing_secrets(tmp_path, monkeypatch):
    from scripts import init_env

    monkeypatch.chdir(tmp_path)
    init_env.main()
    before = {path.name: path.read_bytes() for path in (tmp_path / "var/secrets").iterdir()}
    init_env.main()
    after = {path.name: path.read_bytes() for path in (tmp_path / "var/secrets").iterdir()}
    assert before == after
    assert len(before) == 11
    assert all((tmp_path / "var/secrets" / name).stat().st_mode & 0o077 == 0 for name in before)


def test_readiness_fails_when_required_queue_is_unavailable(env, monkeypatch):
    from apps.api import main
    import redis

    class BrokenRedis:
        def ping(self):
            raise ConnectionError("controlled unavailable queue")

    client, _, _, _ = env
    monkeypatch.setattr(main, "QUEUE_MODE", "celery")
    monkeypatch.setattr(redis.Redis, "from_url", lambda *args, **kwargs: BrokenRedis())
    response = client.get("/api/v1/readiness")
    assert response.status_code == 503
    assert response.json() == {"database": "ok", "storage": "ok", "queue": "waiting_for_redis", "status": "unavailable"}


def test_compose_declares_separate_roles_and_file_secrets():
    compose = Path("compose.yaml").read_text()
    role_script = Path("deploy/postgres/ensure-roles.sh").read_text()
    for role in ("productmatch_owner", "productmatch_app", "productmatch_readonly", "productmatch_backup"):
        assert role in compose + role_script
    for secret in ("DB_PASSWORD_FILE", "JWT_SECRET_FILE", "S3_SECRET_KEY_FILE", "INTEGRATION_MASTER_KEY_FILE"):
        assert secret in compose
    assert "DB_AUTO_CREATE: 'false'" in compose
    assert "${JWT_SECRET" not in compose and "${S3_SECRET_KEY" not in compose


def test_restore_snapshot_comparison_detects_rows_and_objects():
    from scripts.verify_postgres_restore import compare_snapshots

    expected = {
        "migration_head": "head-1",
        "table_counts": {"files": 2, "organizations": 2},
        "referenced_objects": {"org/file.csv": {"sha256": "a", "bytes": 10}},
    }
    assert compare_snapshots(expected, expected) == {
        "table_counts_match": True,
        "table_count_differences": {},
        "referenced_objects_match": True,
        "migration_head_match": True,
        "observed_data_loss_rows": 0,
        "observed_row_count_delta": 0,
    }
    changed = {
        "migration_head": "head-2",
        "table_counts": {"files": 1, "organizations": 2, "new_table": 3},
        "referenced_objects": {},
    }
    result = compare_snapshots(expected, changed)
    assert not result["table_counts_match"]
    assert result["table_count_differences"] == {
        "files": {"before": 2, "after": 1},
        "new_table": {"before": None, "after": 3},
    }
    assert result["observed_data_loss_rows"] == 1
    assert result["observed_row_count_delta"] == 4
    assert not result["referenced_objects_match"]
    assert not result["migration_head_match"]


def test_acceptance_overlay_contains_independent_joint_restore():
    overlay = Path("deploy/compose.acceptance.yaml").read_text()
    drill = Path("scripts/stack_drill.py").read_text()
    for service in ("database-backup", "storage-backup", "postgres-restore", "minio-restore", "database-restore", "storage-restore"):
        assert service in overlay
    assert "source_database_and_storage_stopped" in drill
    assert "product-match-restore-verifier" in drill
    assert "freeze_evidence" in drill


def test_fixed_dataset_loader_creates_ten_total_users_per_organization(tmp_path):
    dataset = tmp_path / "dataset"
    data = tmp_path / "runtime"
    environment = os.environ.copy()
    environment.update({
        "DATA_DIR": str(data),
        "DATABASE_URL": "sqlite:///" + str(data / "app.db"),
        "JWT_SECRET": "acceptance-test-key-with-at-least-thirty-two-bytes",
        "QUEUE_MODE": "database",
    })
    script = f"""
import json
from sqlalchemy import func, select
from scripts.acceptance_dataset import generate, load
from packages.domain.db import transaction
from packages.domain.models import BatchRelease, Membership, Organization
generate({str(dataset)!r}, 20, 20, 2, 10, 1309)
load({str(dataset)!r})
with transaction() as session:
    counts = [session.scalar(select(func.count()).select_from(Membership).where(Membership.org_id == org.id)) for org in session.scalars(select(Organization).order_by(Organization.id))]
    releases = [session.scalar(select(func.count()).select_from(BatchRelease).where(BatchRelease.org_id == org.id)) for org in session.scalars(select(Organization).order_by(Organization.id))]
print("ACCEPTANCE_COUNTS=" + json.dumps({{"users": counts, "releases": releases}}))
"""
    result = subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True, env=environment)
    line = next(row for row in result.stdout.splitlines() if row.startswith("ACCEPTANCE_COUNTS="))
    assert json.loads(line.split("=", 1)[1]) == {"users": [10, 10], "releases": [10, 10]}


@pytest.mark.skipif(os.getenv("TEST_MIGRATED_SCHEMA") != "true", reason="PostgreSQL migration acceptance only")
def test_postgresql_application_role_cannot_create_schema_objects():
    from packages.domain.db import engine

    assert engine.dialect.name == "postgresql"
    with engine.connect() as connection:
        assert connection.execute(text("SELECT current_user")).scalar_one() == "productmatch_app"
        assert connection.execute(text("SELECT current_setting('timezone')")).scalar_one() == "UTC"
        assert not connection.execute(text("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")).scalar_one()
        transaction = connection.begin_nested()
        with pytest.raises(DBAPIError):
            connection.execute(text("CREATE TABLE application_role_must_not_create_this (id integer)"))
        transaction.rollback()
