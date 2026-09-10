from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import DATABASE_URL


def uid():
    return str(uuid4())


def now():
    return datetime.now(timezone.utc).isoformat()


class Base(DeclarativeBase):
    pass


engine = create_engine(DATABASE_URL, pool_pre_ping=True, **(
    {"connect_args": {"check_same_thread": False, "timeout": 30}}
    if DATABASE_URL.startswith("sqlite") else {"pool_size": 5, "max_overflow": 0}
))
if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def configure_sqlite(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")

Session = sessionmaker(engine, expire_on_commit=False)


@contextmanager
def transaction(write=False):
    with Session() as s:
        try:
            if write and engine.dialect.name == "sqlite":
                s.execute(text("BEGIN IMMEDIATE"))
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise


def initialize():
    from . import models
    Base.metadata.create_all(engine)
    with transaction(write=True) as s:
        if engine.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        s.execute(insert(models.Scheduler).values(id="global").on_conflict_do_nothing(index_elements=["id"]))
