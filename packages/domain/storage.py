import os
from pathlib import Path
from uuid import uuid4
from .config import DATA_DIR, STORAGE_BACKEND, secret_value


def observed_storage(operation):
    from functools import wraps
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            try:return fn(*args, **kwargs)
            except Exception as exc:
                import logging
                from .telemetry import increment
                increment('storage_'+operation+'_errors')
                logging.getLogger('storage').error('storage_error operation=%s error_type=%s',operation,type(exc).__name__)
                raise
        return wrapped
    return decorate


@observed_storage('put')
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
    from botocore.config import Config
    return boto3.client("s3", endpoint_url=os.getenv("S3_ENDPOINT"), aws_access_key_id=os.getenv("S3_ACCESS_KEY"), aws_secret_access_key=secret_value("S3_SECRET_KEY"), config=Config(connect_timeout=2, read_timeout=10, retries={"max_attempts":2}))


@observed_storage('read')
def read(key):
    if STORAGE_BACKEND == "s3":
        return client().get_object(Bucket=os.getenv("S3_BUCKET", "product-match"), Key=key)["Body"].read()
    root = (DATA_DIR / "objects").resolve()
    path = (root / key).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Invalid object key")
    return path.read_bytes()


@observed_storage('delete')
def delete(key):
    if STORAGE_BACKEND == 's3':
        client().delete_object(Bucket=os.getenv('S3_BUCKET', 'product-match'), Key=key)
    else:
        root = (DATA_DIR/'objects').resolve()
        path = (root/key).resolve()
        if not path.is_relative_to(root): raise ValueError('Invalid object key')
        path.unlink(missing_ok=True)


@observed_storage('put')
def quota_put(org_id,content,suffix,s=None):
    """Deterministic immutable object key; retries reserve bytes once."""
    from .quotas import reserve
    from .db import transaction
    from ..matching.normalize import digest
    key=f'{org_id}/{digest(content)}.{suffix}'
    def write(session):
        reserve(session,org_id,key,len(content))
        if STORAGE_BACKEND=='s3':
            client().put_object(Bucket=os.getenv('S3_BUCKET','product-match'),Key=key,Body=content)
        else:
            from tempfile import NamedTemporaryFile
            dest=DATA_DIR/'objects'/key;dest.parent.mkdir(parents=True,exist_ok=True)
            with NamedTemporaryFile(dir=dest.parent,delete=False) as f:f.write(content);temp=Path(f.name)
            temp.replace(dest)
        return key
    if s is not None:return write(s)
    with transaction(write=True) as session:return write(session)
