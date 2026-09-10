import os
from pathlib import Path
from uuid import uuid4
from .config import DATA_DIR, STORAGE_BACKEND


def put(org_id, content, suffix):
    key = f"{org_id}/{uuid4()}.{suffix}"
    if STORAGE_BACKEND == "s3":
        client().put_object(Bucket=os.getenv("S3_BUCKET", "product-match"), Key=key, Body=content)
    else:
        dest = DATA_DIR / "objects" / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp = dest.with_suffix(dest.suffix + ".tmp")
        temp.write_bytes(content)
        temp.replace(dest)
    return key


def client():
    import boto3
    return boto3.client("s3", endpoint_url=os.getenv("S3_ENDPOINT"), aws_access_key_id=os.getenv("S3_ACCESS_KEY"), aws_secret_access_key=os.getenv("S3_SECRET_KEY"))


def read(key):
    if STORAGE_BACKEND == "s3":
        return client().get_object(Bucket=os.getenv("S3_BUCKET", "product-match"), Key=key)["Body"].read()
    root = (DATA_DIR / "objects").resolve()
    path = (root / key).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Invalid object key")
    return path.read_bytes()
