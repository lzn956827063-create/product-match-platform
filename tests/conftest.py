import os
import tempfile
from pathlib import Path

TEST_DIR = Path(tempfile.mkdtemp(prefix="product-match-tests-"))
os.environ["DATA_DIR"] = str(TEST_DIR)
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or "sqlite:///" + str(TEST_DIR / "tests.db")
os.environ["JWT_SECRET"] = "test-only-key-with-at-least-thirty-two-bytes"

if os.getenv("TEST_MIGRATED_SCHEMA")=="true":os.environ["DB_AUTO_CREATE"]="false"

import pytest
from fastapi.testclient import TestClient
from apps.api.main import app
from packages.domain.db import Base, engine
from scripts.seed import main as seed
from workers.jobs import get_matcher


@pytest.fixture(scope='session',autouse=True)
def migrated_schema():
    if os.getenv('TEST_MIGRATED_SCHEMA')=='true':
        from alembic.config import Config
        from alembic import command
        command.upgrade(Config('alembic.ini'),'head')


@pytest.fixture
def env():
    if os.getenv('TEST_MIGRATED_SCHEMA')=='true':
        from sqlalchemy import delete
        with engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):conn.execute(delete(table))
    else:Base.metadata.drop_all(engine)
    get_matcher.cache_clear()
    seed()
    with TestClient(app) as client:
        headers = {}
        for role, domain in [("operator", "demo"), ("reviewer", "demo"), ("admin", "demo"), ("admin", "other")]:
            r = client.post("/api/v1/auth/login", json={"email": f"{role}@{domain}.local", "password": "Demo2026!match"})
            assert r.status_code == 200, r.text
            h = {"Authorization": "Bearer " + r.json()["access_token"]}
            m = client.get("/api/v1/memberships", headers=h).json()["items"][0]
            h["X-Organization-ID"] = m["org_id"]
            headers[role if domain == "demo" else "other"] = h
        run = client.get("/api/v1/runs", headers=headers["operator"]).json()["items"][0]
        items = client.get(f"/api/v1/runs/{run['id']}/items", headers=headers["operator"]).json()["items"]
        yield client, headers, run, items
