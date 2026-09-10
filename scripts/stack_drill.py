"""Run an isolated PostgreSQL/Redis/Celery/MinIO acceptance stack and collect evidence."""
import argparse
import base64
import hashlib
import json
import secrets
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from scripts.acceptance_dataset import generate
from scripts.database_acceptance import preflight, write_json


class AcceptanceStack:
    def __init__(self, output, port):
        self.output = Path(output).resolve()
        if self.output.exists():
            raise ValueError("Acceptance output already exists; use a fresh run directory")
        self.output.mkdir(parents=True)
        self.port = port
        suffix = hashlib.sha256(str(self.output).encode()).hexdigest()[:10]
        self.project = f"product-match-accept-{suffix}"
        self.log = self.output / "commands.log"
        self.secrets = self.output.parent / (self.output.name + "-private")
        if self.secrets.exists():
            raise ValueError("Acceptance private directory already exists; use a fresh run directory")
        self.secrets.mkdir(mode=0o700)
        values = {
            "db_bootstrap_password": secrets.token_hex(24),
            "db_owner_password": secrets.token_hex(24),
            "db_app_password": secrets.token_hex(24),
            "db_readonly_password": secrets.token_hex(24),
            "db_backup_password": secrets.token_hex(24),
            "jwt_secret": secrets.token_hex(48),
            "s3_root_secret": secrets.token_hex(32),
            "s3_app_secret": secrets.token_hex(32),
            "metrics_token": secrets.token_hex(32),
            "integration_master_key": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        }
        for name, value in values.items():
            path = self.secrets / name
            path.write_text(value)
            path.chmod(0o600)
        self.envfile = self.output / ".env.acceptance"
        self.envfile.write_text(
            f'SECRETS_DIR="{self.secrets}"\n'
            f'ACCEPTANCE_OUTPUT="{self.output}"\n'
            "S3_ROOT_ACCESS_KEY=productmatch-root\n"
            "S3_ACCESS_KEY=productmatch-app\n"
            f"WEB_PORT={port}\n"
            f"ALLOWED_ORIGINS=http://127.0.0.1:{port},http://localhost:{port}\n"
            "COOKIE_SECURE=false\n"
            "WEBHOOK_ALLOWED_HOSTS=\n"
        )
        self.envfile.chmod(0o600)
        self.command = [
            "docker", "compose", "--env-file", str(self.envfile), "-p", self.project,
            "-f", "compose.yaml", "-f", "deploy/compose.acceptance.yaml",
        ]

    def compose(self, *arguments):
        result = subprocess.run(self.command + list(arguments), capture_output=True, text=True)
        with self.log.open("a") as stream:
            stream.write("compose " + " ".join(arguments) + "\n")
            stream.write(result.stdout + "\n" + result.stderr + "\n")
        result.check_returncode()
        return result.stdout

    def wait_ready(self, expected=200, timeout=360):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                response = httpx.get(f"http://127.0.0.1:{self.port}/api/v1/readiness", timeout=3)
                last = {"status": response.status_code, "body": response.json()}
                if response.status_code == expected:
                    return last
            except (httpx.HTTPError, ValueError) as exc:
                last = {"error": type(exc).__name__}
            time.sleep(2)
        raise RuntimeError(f"Readiness did not reach HTTP {expected}: {last}")

    def down(self):
        self.compose("down", "--volumes", "--remove-orphans")


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def object_inventory(path):
    rows = {}
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        key = item.get("key")
        if key:
            rows[key] = item.get("size")
    return rows


def freeze_evidence(path):
    root = Path(path)
    for item in sorted(root.rglob("*"), key=lambda row: len(row.parts), reverse=True):
        item.chmod(0o555 if item.is_dir() else 0o444)
    root.chmod(0o555)


def write_acceptance_summary(output, faults_executed, restoration, remaining):
    rows = [
        ("PostgreSQL migration, application-role regression and DDL denial", "passed"),
        ("Fixed 2 x 50k/10k synthetic dataset and independent truth set", "passed"),
        ("PostgreSQL query plans and database statistics", "passed"),
        ("Redis/PostgreSQL/MinIO baseline interruption drills", "passed" if faults_executed else "skipped by option"),
        ("Independent PostgreSQL plus MinIO restoration", "passed" if restoration and restoration["passed"] else "skipped by option"),
        ("Real ERP, customer-data accuracy and human trial", "outside this acceptance scope"),
    ]
    lines = [
        "# Database acceptance result",
        "",
        "This result covers an isolated Compose stack with fixed synthetic data. Its conclusion is limited to database engineering for enterprise-trial admission.",
        "",
        "| Item | Result |",
        "| --- | --- |",
        *(f"| {item} | {status} |" for item, status in rows),
        "",
        "## Remaining acceptance",
        "",
        *(f"- {item}" for item in remaining),
        "",
        "This evidence does not prove real ERP integration, customer-data matching accuracy, production capacity, high availability, or production acceptance.",
        "",
    ]
    (Path(output) / "acceptance.md").write_text("\n".join(lines))


def login(client):
    response = client.post("/auth/login", json={"email": "operator@demo.local", "password": "Demo2026!match"})
    response.raise_for_status()
    client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
    membership = client.get("/memberships")
    membership.raise_for_status()
    client.headers["X-Organization-ID"] = membership.json()["items"][0]["org_id"]


def wait_run(client, ident, timeout=900):
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get("/runs/" + ident)
        response.raise_for_status()
        body = response.json()
        if body["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return body
        time.sleep(2)
    raise RuntimeError("Run did not finish within the acceptance timeout")


def wait_export(client, ident, timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get("/exports")
        response.raise_for_status()
        row = next((item for item in response.json()["items"] if item["id"] == ident), None)
        if row and row["status"] in {"SUCCEEDED", "FAILED"}:
            return row
        time.sleep(1)
    raise RuntimeError("Export did not finish within the acceptance timeout")


def fault_drills(stack, definitions):
    timeline = []
    base_url = f"http://127.0.0.1:{stack.port}/api/v1"
    with httpx.Client(base_url=base_url, timeout=40) as client:
        login(client)
        original = definitions[0]
        run_body = {name: original[name] for name in ("revision_id", "catalog_version_id", "policy_id")}

        stack.compose("stop", "redis")
        timeline.append({"event": "redis_stopped", "at": time.time(), "readiness": stack.wait_ready(503)})
        key = "redis-outage-" + secrets.token_hex(8)
        queued = client.post("/runs", headers={"Idempotency-Key": key}, json=run_body)
        queued.raise_for_status()
        queued_id = queued.json()["id"]
        assert client.get("/runs/" + queued_id).json()["status"] == "QUEUED"
        stack.compose("start", "redis")
        timeline.append({"event": "redis_started", "at": time.time(), "readiness": stack.wait_ready(200)})
        recovered = wait_run(client, queued_id)
        assert recovered["status"] == "SUCCEEDED"
        timeline.append({"event": "redis_queued_run_recovered", "at": time.time(), "run_id": queued_id})

        stack.compose("stop", "postgres")
        timeline.append({"event": "postgres_stopped", "at": time.time(), "readiness": stack.wait_ready(503)})
        key = "postgres-outage-" + secrets.token_hex(8)
        try:
            failed = client.post("/runs", headers={"Idempotency-Key": key}, json=run_body)
            status = failed.status_code
        except httpx.HTTPError:
            status = 0
        assert status < 200 or status >= 300
        stack.compose("start", "postgres")
        timeline.append({"event": "postgres_started", "at": time.time(), "readiness": stack.wait_ready(200)})
        first = client.post("/runs", headers={"Idempotency-Key": key}, json=run_body)
        first.raise_for_status()
        repeated = client.post("/runs", headers={"Idempotency-Key": key}, json=run_body)
        repeated.raise_for_status()
        assert first.json()["id"] == repeated.json()["id"]
        assert wait_run(client, first.json()["id"])["status"] == "SUCCEEDED"
        timeline.append({"event": "postgres_retry_idempotent", "at": time.time(), "run_id": first.json()["id"]})

        stack.compose("stop", "minio")
        timeline.append({"event": "minio_stopped", "at": time.time(), "readiness": stack.wait_ready(503)})
        failed_export = client.post(
            "/runs/" + first.json()["id"] + "/exports",
            headers={"Idempotency-Key": "minio-failure-" + secrets.token_hex(8)},
            json={"kind": "all", "format": "csv"},
        )
        failed_export.raise_for_status()
        failed_row = wait_export(client, failed_export.json()["id"])
        assert failed_row["status"] == "FAILED"
        assert client.get("/exports/" + failed_row["id"] + "/download").status_code == 409
        stack.compose("start", "minio")
        timeline.append({"event": "minio_started", "at": time.time(), "readiness": stack.wait_ready(200)})
        retry = client.post(
            "/runs/" + first.json()["id"] + "/exports",
            headers={"Idempotency-Key": "minio-retry-" + secrets.token_hex(8)},
            json={"kind": "all", "format": "csv"},
        )
        retry.raise_for_status()
        successful = wait_export(client, retry.json()["id"])
        assert successful["status"] == "SUCCEEDED"
        download = client.get("/exports/" + successful["id"] + "/download")
        download.raise_for_status()
        assert hashlib.sha256(download.content).hexdigest() == successful["file_hash"]
        timeline.append({"event": "minio_export_recovered", "at": time.time(), "export_id": successful["id"], "sha256": successful["file_hash"]})
    return timeline


def joint_restore(stack):
    restore = stack.output / "restore"
    restore.mkdir(parents=True, exist_ok=True)
    timeline = []

    # Remove external/API writers before taking a fixed recovery point.
    stack.compose("stop", "web", "dispatcher", "worker-short", "worker")
    timeline.append({"event": "business_writers_stopped", "at": time.time()})
    source_snapshot = "/data/restore/source-snapshot.json"
    stack.compose(
        "exec", "-T", "api", "python", "-m", "scripts.verify_postgres_restore",
        "snapshot", "--output", source_snapshot,
    )
    stack.compose("cp", f"api:{source_snapshot}", str(restore / "source-snapshot.json"))
    stack.compose("stop", "api")
    timeline.append({
        "event": "quiesced_recovery_point_created",
        "at": time.time(),
        "snapshot_sha256": sha256(restore / "source-snapshot.json"),
    })

    backup_started = time.time()
    stack.compose("--profile", "restore", "run", "--rm", "database-backup")
    database_backup_seconds = time.time() - backup_started
    storage_started = time.time()
    stack.compose("--profile", "restore", "run", "--rm", "storage-backup")
    storage_backup_seconds = time.time() - storage_started
    database_hash = sha256(restore / "database.dump")
    recorded_hash = (restore / "database.sha256").read_text().split()[0]
    if database_hash != recorded_hash:
        raise AssertionError("PostgreSQL backup digest does not match its recorded checksum")
    timeline.append({
        "event": "source_backups_completed",
        "at": time.time(),
        "database_backup_seconds": database_backup_seconds,
        "storage_backup_seconds": storage_backup_seconds,
        "database_dump_sha256": database_hash,
    })

    stack.compose("stop", "postgres", "minio")
    timeline.append({"event": "source_database_and_storage_stopped", "at": time.time()})
    restore_started = time.time()
    stack.compose("--profile", "restore", "up", "-d", "postgres-restore", "minio-restore")
    stack.compose("--profile", "restore", "run", "--rm", "db-restore-role-init")
    stack.compose("--profile", "restore", "run", "--rm", "database-restore")
    stack.compose("--profile", "restore", "run", "--rm", "storage-restore")
    stack.compose(
        "--profile", "restore", "run", "--rm", "--no-deps",
        "-e", "DB_HOST=postgres-restore",
        "-e", "DB_NAME=productmatch_restore",
        "-e", "DB_APPLICATION_NAME=product-match-restore-verifier",
        "-e", "S3_ENDPOINT=http://minio-restore:9000",
        "-e", "DATA_DIR=/evidence/restore/restored-app-data",
        "-v", f"{stack.output}:/evidence",
        "api", "python", "-m", "scripts.verify_postgres_restore", "verify",
        "--expected", "/evidence/restore/source-snapshot.json",
        "--output", "/evidence/restore/verification.json",
        "--restore-started", str(restore_started),
    )
    source_inventory = object_inventory(restore / "source-objects.jsonl")
    restored_inventory = object_inventory(restore / "restored-objects.jsonl")
    if source_inventory != restored_inventory:
        raise AssertionError("Restored MinIO object inventory differs from source backup inventory")
    verification = json.loads((restore / "verification.json").read_text())
    verification.update({
        "backup_seconds": {
            "postgresql": database_backup_seconds,
            "minio": storage_backup_seconds,
        },
        "database_dump_sha256": database_hash,
        "all_object_inventory_match": True,
        "all_object_count": len(source_inventory),
        "restore_started_at": datetime.fromtimestamp(restore_started, timezone.utc).isoformat(),
        "timeline": timeline + [{"event": "independent_restore_verified", "at": time.time()}],
    })
    write_json(restore / "verification.json", verification)
    return verification


def main(arguments):
    if not shutil.which("docker"):
        raise SystemExit("Docker executable is unavailable; no target-stack evidence was generated")
    subprocess.run(["docker", "info"], check=True, stdout=subprocess.DEVNULL)
    stack = AcceptanceStack(arguments.output, arguments.port)
    dataset = stack.output / "dataset" / "data"
    completed = False
    try:
        preflight(stack.output)
        generate(dataset, arguments.catalog_size, arguments.source_size, 2, 10, arguments.seed)
        stack.compose("pull")
        stack.compose("up", "-d", "postgres", "redis", "minio", "storage-init")
        migration_log = stack.compose("run", "--rm", "migrate")
        (stack.output / "migration" / "upgrade.log").write_text(migration_log)
        (stack.output / "migration" / "current.log").write_text(stack.compose("run", "--rm", "--no-deps", "migrate", "alembic", "current"))
        (stack.output / "migration" / "check.log").write_text(stack.compose("run", "--rm", "--no-deps", "migrate", "alembic", "check"))
        stack.compose(
            "run", "--rm", "--no-deps", "-e", "TEST_MIGRATED_SCHEMA=true", "api", "sh", "-c",
            "mkdir -p /data/acceptance-tests && pytest -q tests/test_v13.py tests/test_enterprise.py tests/test_business.py tests/test_recovery.py tests/test_database_deployment.py --junitxml=/data/acceptance-tests/postgresql-tests.xml",
        )
        stack.compose("up", "-d", "api", "web")
        stack.wait_ready(200)
        stack.compose("cp", "api:/data/acceptance-tests/postgresql-tests.xml", str(stack.output / "migration" / "postgresql-tests.xml"))
        stack.compose("cp", str(dataset), "api:/data/acceptance-input")
        stack.compose("exec", "-T", "api", "python", "-m", "scripts.acceptance_dataset", "load", "/data/acceptance-input")
        stack.compose("cp", "api:/data/acceptance-input/loaded.json", str(dataset / "loaded.json"))
        analyze_log = stack.compose(
            "run", "--rm", "--no-deps", "migrate", "python", "-c",
            "from packages.domain.db import engine; connection=engine.connect().execution_options(isolation_level='AUTOCOMMIT'); connection.exec_driver_sql('ANALYZE'); connection.close()",
        )
        (stack.output / "capacity" / "analyze.log").write_text(analyze_log)
        stack.compose("up", "-d", "worker", "worker-short", "dispatcher")
        definitions = json.loads((dataset / "loaded.json").read_text())["definitions"]
        timeline = [] if arguments.skip_faults else fault_drills(stack, definitions)
        write_json(stack.output / "faults" / "timeline.json", {
            "scope": "Actual isolated Compose dependencies with fixed synthetic data" if timeline else "Fault drills skipped by command option",
            "events": timeline,
        })
        stack.compose("exec", "-T", "api", "python", "-m", "scripts.database_acceptance", "collect", "--output", "/data/database-evidence", "--dataset-manifest", "/data/acceptance-input/manifest.json")
        stack.compose("cp", "api:/data/database-evidence/.", str(stack.output))
        stack.compose("cp", "api:/data/acceptance-tests/postgresql-tests.xml", str(stack.output / "correctness" / "postgresql-tests.xml"))
        write_json(stack.output / "correctness" / "coverage.json", {
            "scope": "PostgreSQL migrated-schema API and direct-database regression using fixed synthetic accounts",
            "junit": "postgresql-tests.xml",
            "covered": [
                "concurrent review conflict",
                "twenty-client claim conflict",
                "concurrent release publication",
                "organization isolation and composite foreign keys",
                "idempotent API and outbox retries",
                "whole-chunk rollback after unique-constraint failure",
                "stale version and stale lease fencing",
            ],
        })
        (stack.output / "images.jsonl").write_text(stack.compose("images", "--format", "json"))
        (stack.output / "faults" / "compose.log").write_text(stack.compose("logs", "--no-color", "--tail", "1000", "api", "worker", "worker-short", "dispatcher", "postgres", "redis", "minio"))
        restoration = None if arguments.skip_restore else joint_restore(stack)
        remaining = [
            "30-minute fixed-resource mixed load",
            "real Celery child SIGKILL and natural lease",
            "stage-specific PostgreSQL interruption during review, chunk commit and publication",
            "MinIO interruption during upload and publication artifact generation",
            "alert delivered to an assigned human",
        ]
        if arguments.skip_restore:
            remaining.append("independent PostgreSQL plus MinIO restore and measured RPO/RTO")
        manifest = json.loads((stack.output / "run-manifest.json").read_text())
        manifest.update({
            "container_project": stack.project,
            "container_stack_executed": True,
            "fault_drills_executed": not arguments.skip_faults,
            "joint_restore_executed": not arguments.skip_restore,
            "joint_restore_passed": restoration["passed"] if restoration else None,
            "joint_restore_evidence": "restore/verification.json" if restoration else None,
            "evidence_read_only_after_cleanup": not arguments.keep,
            "resource_profile": "deploy/compose.acceptance.yaml",
            "image_inventory": "images.jsonl",
            "remaining_acceptance": remaining,
        })
        write_json(stack.output / "run-manifest.json", manifest)
        write_acceptance_summary(stack.output, not arguments.skip_faults, restoration, remaining)
        completed = True
        print(json.dumps({"output": str(stack.output), "project": stack.project, "completed": True}, ensure_ascii=False))
    finally:
        cleanup_error = None
        if not arguments.keep and stack.log.exists():
            try:
                stack.down()
            except subprocess.CalledProcessError as exc:
                cleanup_error = exc
        if completed and not arguments.keep:
            if cleanup_error:
                raise RuntimeError("Acceptance completed but the isolated Compose project could not be cleaned up") from cleanup_error
            freeze_evidence(stack.output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="../../work/stack-acceptance")
    parser.add_argument("--port", type=int, default=18800)
    parser.add_argument("--catalog-size", type=int, default=50000)
    parser.add_argument("--source-size", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=1309)
    parser.add_argument("--skip-faults", action="store_true")
    parser.add_argument("--skip-restore", action="store_true")
    parser.add_argument("--keep", action="store_true")
    main(parser.parse_args())
