"""Create a redacted restore snapshot and verify a PostgreSQL plus S3 restoration."""
import argparse
import hashlib
import json
import time
from pathlib import Path

from sqlalchemy import inspect, select, text

from packages.domain import storage
from packages.domain.config import DATA_DIR
from packages.domain.db import engine, transaction
from packages.domain.models import Export, File, ImportJob, ReleaseArtifact
from packages.matching.normalize import digest

def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def referenced_objects(session):
    groups = (
        ((row.object_key, row.sha256) for row in session.scalars(select(File))),
        ((row.object_key, row.file_hash) for row in session.scalars(select(Export).where(Export.status == "SUCCEEDED", Export.object_key.is_not(None)))),
        ((row.object_key, row.file_hash) for row in session.scalars(select(ReleaseArtifact).where(ReleaseArtifact.status == "SUCCEEDED", ReleaseArtifact.object_key.is_not(None)))),
        ((row.result_key, row.result_hash) for row in session.scalars(select(ImportJob).where(ImportJob.result_key.is_not(None)))),
    )
    references = {}
    for group in groups:
        for key, value in group:
            if not key or not value:
                continue
            if key in references and references[key] != value:
                raise ValueError("Conflicting digests reference the same object key")
            references[key] = value
    return references


def snapshot():
    if engine.dialect.name != "postgresql":
        raise ValueError("This verifier requires an actual PostgreSQL database")
    present = sorted(inspect(engine).get_table_names())
    with transaction() as session:
        current_user = session.execute(text("SELECT current_user")).scalar_one()
        head = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        counts = {table: session.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one() for table in present}
        references = referenced_objects(session)
    verified = {}
    for key, expected in sorted(references.items()):
        content = storage.read(key)
        actual = digest(content)
        if actual != expected:
            raise ValueError("Referenced object digest mismatch")
        verified[key] = {"sha256": actual, "bytes": len(content)}
    object_manifest_hash = hashlib.sha256(json.dumps(verified, sort_keys=True).encode()).hexdigest()
    return {
        "database_backend": "postgresql",
        "database_user": current_user,
        "migration_head": head,
        "table_counts": counts,
        "referenced_objects": verified,
        "referenced_object_count": len(verified),
        "object_manifest_sha256": object_manifest_hash,
    }


def compare_snapshots(expected, current):
    tables = sorted(set(expected["table_counts"]) | set(current["table_counts"]))
    differences = {
        table: {
            "before": expected["table_counts"].get(table),
            "after": current["table_counts"].get(table),
        }
        for table in tables
        if expected["table_counts"].get(table) != current["table_counts"].get(table)
    }
    lost_rows = sum(
        max(0, (row["before"] or 0) - (row["after"] or 0))
        for row in differences.values()
    )
    count_delta = sum(
        abs((row["after"] or 0) - (row["before"] or 0))
        for row in differences.values()
    )
    return {
        "table_counts_match": not differences,
        "table_count_differences": differences,
        "referenced_objects_match": expected["referenced_objects"] == current["referenced_objects"],
        "migration_head_match": expected["migration_head"] == current["migration_head"],
        "observed_data_loss_rows": lost_rows,
        "observed_row_count_delta": count_delta,
    }


def verify(expected_path, restore_started):
    expected = json.loads(Path(expected_path).read_text())
    current = snapshot()
    comparison = compare_snapshots(expected, current)
    pending = current["table_counts"].get("deliveries", 0)
    hold = DATA_DIR / "delivery-restore-hold.json"
    hold.parent.mkdir(parents=True, exist_ok=True)
    hold.write_text(json.dumps({
        "scope": "Restored acceptance environment",
        "pending_delivery_rows": pending,
        "instruction": "Reconcile downstream state before starting dispatcher or delivery workers",
    }, ensure_ascii=False, indent=2))
    result = {
        **current,
        **comparison,
        "source_database_user": expected["database_user"],
        "delivery_hold_created": hold.is_file(),
        "restore_seconds": max(0, time.time() - restore_started),
        "rpo_statement": "Zero row-count and referenced-object loss within the quiesced synthetic acceptance recovery point" if comparison["table_counts_match"] and comparison["referenced_objects_match"] else "Restore differs from the source recovery point",
        "passed": all((
            comparison["table_counts_match"],
            comparison["referenced_objects_match"],
            comparison["migration_head_match"],
            current["database_user"] == "productmatch_app",
            hold.is_file(),
        )),
        "scope": "Independent PostgreSQL container and independent MinIO volume using fixed synthetic acceptance data",
    }
    if not result["passed"]:
        raise AssertionError(json.dumps(comparison))
    return result


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    source = commands.add_parser("snapshot")
    source.add_argument("--output", required=True)
    restored = commands.add_parser("verify")
    restored.add_argument("--expected", required=True)
    restored.add_argument("--output", required=True)
    restored.add_argument("--restore-started", required=True, type=float)
    args = parser.parse_args()
    result = snapshot() if args.command == "snapshot" else verify(args.expected, args.restore_started)
    write_json(args.output, result)
    print(json.dumps({"passed": result.get("passed", True), "tables": len(result["table_counts"]), "objects": result["referenced_object_count"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
