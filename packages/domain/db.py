from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
import os

from .config import DATABASE_URL


def uid():
    return str(uuid4())


def now():
    return datetime.now(timezone.utc).isoformat()


class Base(DeclarativeBase):
    pass


if DATABASE_URL.startswith("sqlite"):
    engine_options = {"connect_args": {"check_same_thread": False, "timeout": 30}}
else:
    statement_timeout = int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "30000"))
    lock_timeout = int(os.getenv("DB_LOCK_TIMEOUT_MS", "5000"))
    idle_timeout = int(os.getenv("DB_IDLE_TRANSACTION_TIMEOUT_MS", "60000"))
    application_name = os.getenv("DB_APPLICATION_NAME", "product-match")
    engine_options = {
        "pool_size": int(os.getenv("DB_POOL_SIZE", "5")),
        "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "0")),
        "pool_timeout": int(os.getenv("DB_POOL_TIMEOUT_SECONDS", "5")),
        "connect_args": {
            "application_name": application_name,
            "options": (
                f"-c statement_timeout={statement_timeout} "
                f"-c lock_timeout={lock_timeout} "
                f"-c idle_in_transaction_session_timeout={idle_timeout} "
                "-c timezone=UTC"
            ),
        },
    }

engine = create_engine(DATABASE_URL, pool_pre_ping=True, **engine_options)
if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def configure_sqlite(conn, _):
        import os
        cache_mib=max(1,min(256,int(os.getenv('SQLITE_CACHE_MIB','64'))))
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(f"PRAGMA cache_size={-cache_mib*1024}")

from .request_trace import install as install_trace,add as trace_add
install_trace(engine)

Session = sessionmaker(engine, expire_on_commit=False)


@contextmanager
def transaction(write=False):
    with Session() as s:
        try:
            from time import perf_counter
            started=perf_counter();s.connection();trace_add("connection_acquire_seconds",perf_counter()-started)
            if write and engine.dialect.name == "sqlite":
                s.execute(text("BEGIN IMMEDIATE"))
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise


def initialize():
    from . import models
    import os
    if os.getenv("DB_AUTO_CREATE", "true")=="true":
        Base.metadata.create_all(engine)
    with transaction(write=True) as s:
        if engine.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        s.execute(insert(models.Scheduler).values(id="global").on_conflict_do_nothing(index_elements=["id"]))
