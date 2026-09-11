"""Poll organization-scoped directory and S3 inbox sources."""
import argparse
import os
import shutil
import time
from pathlib import Path

from sqlalchemy import select

from packages.domain import storage, v14_ingestion
from packages.domain.auth import Context
from packages.domain.db import initialize, transaction
from packages.domain.models import IngestionSource, ServiceAccess


def context(source):
    return Context(source.org_id, source.created_by, ["operator"], "ingestion-poller")


def suffixes(source):
    configured = source.config.get("suffixes", [".csv", ".xlsx"])
    return {str(value).lower() if str(value).startswith(".") else "." + str(value).lower() for value in configured}


def poll_directory(s, source):
    root = v14_ingestion.directory_path(source.location)
    if not root.is_dir():
        source.consecutive_failures += 1
        return 0
    stable_seconds = max(1, min(3600, int(source.config.get("stable_seconds", 10))))
    processed = 0
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.suffix.lower() not in suffixes(source) or time.time() - path.stat().st_mtime < stable_seconds:
            continue
        profile = v14_ingestion.latest_profile(s, source)
        key = f"directory:{path.name}:{path.stat().st_size}:{path.stat().st_mtime_ns}"
        event, duplicate = v14_ingestion.receive_bytes(s, context(source), source, profile, path.name, path.read_bytes(), key, source.created_by)
        processed += 0 if duplicate else 1
        if source.config.get("archive_after_success", True):
            folder = root / ("archive" if event.status == "READY" else "rejected")
            folder.mkdir(exist_ok=True)
            destination = folder / path.name
            if destination.exists():
                destination = folder / f"{path.stem}-{event.id[:8]}{path.suffix}"
            shutil.move(path, destination)
    return processed


def poll_s3(s, source):
    client = storage.client()
    bucket = os.getenv("S3_BUCKET", "product-match")
    prefix = source.location.strip("/") + "/"
    result = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=100)
    processed = 0
    for obj in result.get("Contents", []):
        key = obj["Key"]
        relative = key.removeprefix(prefix)
        if not relative or "/" in relative or Path(relative).suffix.lower() not in suffixes(source):
            continue
        profile = v14_ingestion.latest_profile(s, source)
        content = client.get_object(Bucket=bucket, Key=key)["Body"].read(v14_ingestion.MAX_BYTES + 1)
        event, duplicate = v14_ingestion.receive_bytes(s, context(source), source, profile, relative, content, f"s3:{key}:{obj.get('ETag', '')}", source.created_by)
        processed += 0 if duplicate else 1
        folder = "archive" if event.status == "READY" else "rejected"
        destination = f"{prefix}{folder}/{relative}"
        client.copy_object(Bucket=bucket, CopySource={"Bucket": bucket, "Key": key}, Key=destination)
        client.delete_object(Bucket=bucket, Key=key)
    return processed


def poll_once():
    total = 0
    with transaction(write=True) as s:
        sources = list(s.scalars(select(IngestionSource).where(IngestionSource.status == "ACTIVE", IngestionSource.kind.in_(["DIRECTORY", "S3"]))))
        for source in sources:
            try:
                total += poll_directory(s, source) if source.kind == "DIRECTORY" else poll_s3(s, source)
            except Exception as exc:
                source.consecutive_failures += 1
                source.last_checked_at = v14_ingestion.now()
                print(f"source={source.id} status=failed error={type(exc).__name__}")
    return total


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=10)
    args = parser.parse_args()
    initialize()
    while True:
        count = poll_once()
        print(f"ingestion_events_created={count}")
        if args.once:
            break
        time.sleep(max(2, args.interval))


if __name__ == "__main__":
    main()
