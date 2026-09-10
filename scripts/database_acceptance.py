"""Collect redacted, PostgreSQL-specific deployment and acceptance evidence."""
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.engine import make_url

EVIDENCE_DIRECTORIES = ("migration", "dataset", "correctness", "plans", "capacity", "faults", "restore", "security")
EXPECTED_ROLES = ("productmatch_owner", "productmatch_app", "productmatch_readonly", "productmatch_backup")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    temporary.replace(path)


def command_output(command):
    result = subprocess.run(command, capture_output=True, text=True)
    return {"command": command, "returncode": result.returncode, "stdout": result.stdout.rstrip(), "stderr": result.stderr.rstrip()}


def preflight(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    for name in EVIDENCE_DIRECTORIES:
        (output / name).mkdir(exist_ok=True)
    root = Path(__file__).resolve().parents[1]
    files = [root / "compose.yaml", root / "deploy/compose.acceptance.yaml", root / "requirements.lock", root / "apps/web/dist/index.html"]
    docker = shutil.which("docker")
    runtime = {"executable": docker, "engine_available": False, "server_version": None}
    if docker:
        probe = command_output([docker, "info", "--format", "{{.ServerVersion}}"])
        runtime.update(engine_available=probe["returncode"] == 0, server_version=probe["stdout"] or None, probe_error=probe["stderr"] or None)
    git = command_output(["git", "rev-parse", "HEAD"])
    migration = command_output(["git", "status", "--short"])
    manifest = {
        "run_id": "preflight-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "started_at": utc_now(),
        "scope": "Static deployment preflight only; no PostgreSQL, Redis, Celery or MinIO execution evidence",
        "code_commit": git["stdout"] if git["returncode"] == 0 else None,
        "working_tree": migration["stdout"].splitlines(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "container_runtime": runtime,
        "artifacts": {str(path.relative_to(root)): {"sha256": sha256(path), "bytes": path.stat().st_size} for path in files if path.is_file()},
        "migration_head_expected": "b86fd0144e45",
        "database_target_executed": False,
        "production_stack_executed": False,
    }
    write_json(output / "run-manifest.json", manifest)
    acceptance = "# 数据库验收状态\n\n"
    acceptance += "本目录是静态预检结果，不是 PostgreSQL 目标栈通过证明。\n\n"
    acceptance += "| 项目 | 状态 |\n| --- | --- |\n"
    acceptance += "| 角色分离、密钥文件和固定资源配置 | 已完成静态配置 |\n"
    acceptance += "| PostgreSQL 迁移、权限、SQL 计划与并发 | 待具备容器运行时后执行 |\n"
    acceptance += "| Redis/Celery/MinIO 联合流程及中断 | 待执行 |\n"
    acceptance += "| PostgreSQL + MinIO 独立恢复与 RPO/RTO | 待执行 |\n"
    acceptance += "| 真实 ERP、客户数据效果和真人试用 | 不在本轮静态预检内 |\n"
    (output / "acceptance.md").write_text(acceptance)
    return manifest


def table_shape(inspector, table):
    return {
        "columns": [{"name": row["name"], "type": str(row["type"]), "nullable": row["nullable"]} for row in inspector.get_columns(table)],
        "primary_key": inspector.get_pk_constraint(table),
        "foreign_keys": inspector.get_foreign_keys(table),
        "unique_constraints": inspector.get_unique_constraints(table),
        "indexes": inspector.get_indexes(table),
    }


def explain(connection, sql, parameters):
    statement = "EXPLAIN (ANALYZE, BUFFERS, WAL, VERBOSE, FORMAT JSON) " + sql
    result = connection.execute(text(statement), parameters).scalar_one()
    return result[0] if isinstance(result, list) else result


def representative_rows(connection):
    context = connection.execute(text("""
        SELECT o.id AS org_id, r.id AS run_id, r.catalog_version_id, i.id AS item_id,
               p.sku, p.search_model, p.search_name
        FROM organizations o
        JOIN match_runs r ON r.org_id = o.id
        JOIN match_items i ON i.org_id = r.org_id AND i.run_id = r.id
        JOIN catalog_products p ON p.org_id = r.org_id AND p.version_id = r.catalog_version_id
        ORDER BY o.id, r.id, i.id, p.id
        LIMIT 1
    """)).mappings().first()
    if not context:
        raise ValueError("Load the fixed synthetic acceptance dataset and complete at least one run before collecting plans")
    return dict(context)


def collect_plans(connection, output):
    row = representative_rows(connection)
    prefix = (row["search_model"] or "m")[:3]
    fuzzy = "旗舰" if "旗舰" in (row["search_name"] or "") else (row["search_name"] or "m")[:2]
    definitions = {
        "source-list": (
            "SELECT id, status, suggestion, version FROM match_items WHERE org_id=:org AND run_id=:run ORDER BY id LIMIT 50",
            {"org": row["org_id"], "run": row["run_id"]},
        ),
        "review-detail": (
            "SELECT id, status, current_decision_id, version FROM match_items WHERE org_id=:org AND id=:item",
            {"org": row["org_id"], "item": row["item_id"]},
        ),
        "exact-sku-hit": (
            "SELECT id, sku FROM catalog_products WHERE org_id=:org AND version_id=:version AND sku=:sku ORDER BY id LIMIT 50",
            {"org": row["org_id"], "version": row["catalog_version_id"], "sku": row["sku"]},
        ),
        "exact-sku-miss": (
            "SELECT id, sku FROM catalog_products WHERE org_id=:org AND version_id=:version AND sku=:sku ORDER BY id LIMIT 50",
            {"org": row["org_id"], "version": row["catalog_version_id"], "sku": "ABSENT-SKU-FOR-PLAN"},
        ),
        "model-prefix-hit": (
            "SELECT id, sku FROM catalog_products WHERE org_id=:org AND version_id=:version AND search_model>=:prefix AND search_model<:upper ORDER BY id LIMIT 50",
            {"org": row["org_id"], "version": row["catalog_version_id"], "prefix": prefix, "upper": prefix + chr(0x10FFFF)},
        ),
        "model-prefix-miss": (
            "SELECT id, sku FROM catalog_products WHERE org_id=:org AND version_id=:version AND search_model>=:prefix AND search_model<:upper ORDER BY id LIMIT 50",
            {"org": row["org_id"], "version": row["catalog_version_id"], "prefix": "zzzz-absent", "upper": "zzzz-absent" + chr(0x10FFFF)},
        ),
        "name-fuzzy-hit": (
            "SELECT id, sku FROM catalog_products WHERE org_id=:org AND version_id=:version AND search_name LIKE :pattern ORDER BY id LIMIT 50",
            {"org": row["org_id"], "version": row["catalog_version_id"], "pattern": "%" + fuzzy + "%"},
        ),
        "name-fuzzy-short": (
            "SELECT id, sku FROM catalog_products WHERE org_id=:org AND version_id=:version AND search_name LIKE :pattern ORDER BY id LIMIT 50",
            {"org": row["org_id"], "version": row["catalog_version_id"], "pattern": "%旗舰%"},
        ),
        "name-fuzzy-miss": (
            "SELECT id, sku FROM catalog_products WHERE org_id=:org AND version_id=:version AND search_name LIKE :pattern ORDER BY id LIMIT 50",
            {"org": row["org_id"], "version": row["catalog_version_id"], "pattern": "%不存在的验收词%"},
        ),
        "release-history": (
            "SELECT id, number, status, published_at FROM batch_releases WHERE org_id=:org ORDER BY id LIMIT 50",
            {"org": row["org_id"]},
        ),
        "receipt-todo": (
            "SELECT id, status, receipt_due_at FROM deliveries WHERE org_id=:org AND status IN ('RECONCILE','PARTIAL','REJECTED','MANUAL') ORDER BY receipt_due_at, id LIMIT 50",
            {"org": row["org_id"]},
        ),
    }
    results = {}
    for name, (sql, parameters) in definitions.items():
        started = time.perf_counter()
        plan = explain(connection, sql, parameters)
        evidence = {
            "query_id": name,
            "parameter_class": "fixed synthetic acceptance data",
            "sql_shape": sql,
            "plan": plan,
            "collector_seconds": time.perf_counter() - started,
        }
        write_json(output / "plans" / f"{name}.json", evidence)
        results[name] = {"execution_time_ms": plan.get("Execution Time"), "planning_time_ms": plan.get("Planning Time")}
    return results


def collect(output, dataset_manifest=None):
    from packages.domain.config import DATABASE_URL
    from packages.domain.db import engine

    if engine.dialect.name != "postgresql":
        raise ValueError("PostgreSQL evidence collection requires an actual postgresql+psycopg DATABASE_URL")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    for name in EVIDENCE_DIRECTORIES:
        (output / name).mkdir(exist_ok=True)
    started = utc_now()
    inspector = inspect(engine)
    with engine.connect() as connection:
        settings = connection.execute(text("""
            SELECT current_user AS current_user,
                   current_setting('server_version') AS server_version,
                   current_setting('timezone') AS timezone,
                   current_setting('application_name') AS application_name,
                   current_setting('statement_timeout') AS statement_timeout,
                   current_setting('lock_timeout') AS lock_timeout,
                   current_setting('idle_in_transaction_session_timeout') AS idle_in_transaction_session_timeout
        """)).mappings().one()
        head = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        tables = sorted(inspector.get_table_names())
        schema = {"settings": dict(settings), "migration_head": head, "tables": {table: table_shape(inspector, table) for table in tables}}
        write_json(output / "migration" / "schema.json", schema)

        role_rows = connection.execute(text("""
            SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls
            FROM pg_roles WHERE rolname = ANY(:roles) ORDER BY rolname
        """), {"roles": list(EXPECTED_ROLES)}).mappings().all()
        privileges = connection.execute(text("""
            SELECT current_user AS current_user,
                   has_schema_privilege(current_user, 'public', 'CREATE') AS can_create_in_public,
                   has_table_privilege(current_user, 'organizations', 'SELECT') AS can_select,
                   has_table_privilege(current_user, 'organizations', 'INSERT') AS can_insert,
                   has_table_privilege(current_user, 'organizations', 'UPDATE') AS can_update,
                   has_table_privilege(current_user, 'organizations', 'DELETE') AS can_delete
        """)).mappings().one()
    ddl_denied = False
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("CREATE TABLE acceptance_must_be_denied (id integer)"))
        except DBAPIError:
            ddl_denied = True
        finally:
            transaction.rollback()
    security = {
        "roles": [dict(row) for row in role_rows],
        "application_privileges": dict(privileges),
        "application_ddl_denied": ddl_denied,
        "passed": (
            settings["current_user"] == "productmatch_app"
            and {row["rolname"] for row in role_rows} == set(EXPECTED_ROLES)
            and all(not any(row[key] for key in ("rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls")) for row in role_rows)
            and not privileges["can_create_in_public"]
            and all(privileges[key] for key in ("can_select", "can_insert", "can_update", "can_delete"))
            and ddl_denied
        ),
    }
    write_json(output / "security" / "roles.json", security)
    if not security["passed"]:
        raise AssertionError("Application database role does not meet the least-privilege acceptance checks")

    with engine.connect() as connection:
        plans = collect_plans(connection, output)
        activity = [dict(row) for row in connection.execute(text("""
            SELECT application_name, state, wait_event_type, wait_event, count(*) AS sessions
            FROM pg_stat_activity WHERE datname = current_database()
            GROUP BY application_name, state, wait_event_type, wait_event
            ORDER BY application_name, state
        """)).mappings()]
        locks = [dict(row) for row in connection.execute(text("""
            SELECT mode, granted, count(*) AS locks
            FROM pg_locks WHERE database = (SELECT oid FROM pg_database WHERE datname = current_database())
            GROUP BY mode, granted ORDER BY mode, granted
        """)).mappings()]
        database = dict(connection.execute(text("""
            SELECT pg_database_size(current_database()) AS database_bytes,
                   xact_commit, xact_rollback, blks_read, blks_hit, temp_files, temp_bytes, deadlocks,
                   stats_reset
            FROM pg_stat_database WHERE datname = current_database()
        """)).mappings().one())
        relations = [dict(row) for row in connection.execute(text("""
            SELECT c.relname AS table_name,
                   pg_relation_size(c.oid) AS table_bytes,
                   pg_indexes_size(c.oid) AS index_bytes,
                   pg_total_relation_size(c.oid) AS total_bytes
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
            ORDER BY pg_total_relation_size(c.oid) DESC, c.relname
        """)).mappings()]
        blocking = [dict(row) for row in connection.execute(text("""
            SELECT application_name, state, wait_event_type, wait_event,
                   cardinality(pg_blocking_pids(pid)) AS blocking_sessions
            FROM pg_stat_activity
            WHERE datname = current_database() AND cardinality(pg_blocking_pids(pid)) > 0
            ORDER BY application_name
        """)).mappings()]
        wal = dict(connection.execute(text("""
            SELECT wal_records, wal_fpi, wal_bytes, stats_reset FROM pg_stat_wal
        """)).mappings().one())
        checkpoints = dict(connection.execute(text("""
            SELECT checkpoints_timed, checkpoints_req, checkpoint_write_time,
                   checkpoint_sync_time, buffers_checkpoint, stats_reset
            FROM pg_stat_bgwriter
        """)).mappings().one())
        extension = connection.execute(text("""
            SELECT extversion FROM pg_extension WHERE extname = 'pg_stat_statements'
        """)).scalar_one_or_none()
        try:
            statements = [dict(row) for row in connection.execute(text("""
                SELECT queryid, calls, total_exec_time, mean_exec_time, rows,
                       shared_blks_hit, shared_blks_read, temp_blks_written, wal_bytes
                FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
                ORDER BY total_exec_time DESC LIMIT 100
            """)).mappings()]
            statements_status = {"available": True, "rows": len(statements)}
        except DBAPIError as exc:
            statements = []
            statements_status = {"available": False, "error_type": type(exc.orig).__name__}
    write_json(output / "capacity" / "postgres-stats.json", {
        "activity": activity,
        "locks": locks,
        "database": database,
        "relations": relations,
        "blocking_sessions": blocking,
        "wal": wal,
        "checkpoints": checkpoints,
        "pg_stat_statements_version": extension,
        "statements_without_sql_text": statements,
        "statements_status": statements_status,
    })

    root = Path(__file__).resolve().parents[1]
    git = command_output(["git", "rev-parse", "HEAD"])
    dataset = None
    if dataset_manifest:
        dataset_path = Path(dataset_manifest).resolve()
        dataset = {"path": dataset_path.name, "sha256": sha256(dataset_path), "manifest": json.loads(dataset_path.read_text())}
        write_json(output / "dataset" / "manifest.json", dataset["manifest"])
    manifest = {
        "run_id": "postgresql-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "started_at": started,
        "finished_at": utc_now(),
        "scope": "Actual PostgreSQL migration, role, structure and synthetic-query plan evidence",
        "code_commit": git["stdout"] if git["returncode"] == 0 else None,
        "migration_head": head,
        "database_target": make_url(DATABASE_URL).render_as_string(hide_password=True),
        "compose_sha256": sha256(root / "compose.yaml"),
        "requirements_sha256": sha256(root / "requirements.lock"),
        "dataset": dataset,
        "security_passed": True,
        "plans": plans,
        "real_erp_connected": False,
        "customer_data_used": False,
    }
    write_json(output / "run-manifest.json", manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    static = commands.add_parser("preflight")
    static.add_argument("--output", required=True)
    database = commands.add_parser("collect")
    database.add_argument("--output", required=True)
    database.add_argument("--dataset-manifest")
    args = parser.parse_args()
    result = preflight(args.output) if args.command == "preflight" else collect(args.output, args.dataset_manifest)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
