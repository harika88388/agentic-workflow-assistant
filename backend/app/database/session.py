"""Lazy engine/session factory so importing the app never opens a DB connection."""
import logging
import time
from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

log = logging.getLogger("app.database")


def _instrument(engine: Engine) -> None:
    """Connection and query-failure logging. Statements are logged without bound parameters (they hold user data)."""
    state = {"first": True}

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, record):  # noqa: ANN001
        record.info["connected_at"] = time.perf_counter()
        lvl = logging.INFO if state["first"] else logging.DEBUG
        state["first"] = False
        log.log(lvl, "database connection opened dialect=%s", engine.dialect.name)

    @event.listens_for(engine, "handle_error")
    def _on_error(ctx):  # noqa: ANN001
        statement = " ".join((ctx.statement or "").split())[:200]
        log.error("database query failed exception_type=%s disconnect=%s statement=%s",
                  type(ctx.original_exception).__name__, ctx.is_disconnect, statement)


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    engine = create_engine(settings.database_url, pool_pre_ping=True, pool_recycle=300)
    _instrument(engine)
    return engine


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """Request-scoped session. Callers commit explicitly; any exception rolls back."""
    db = get_session_factory()()
    try:
        yield db
    except Exception as exc:
        # HTTP 4xx errors (auth, not found, validation) are expected control flow, not transaction failures.
        expected = getattr(exc, "status_code", 500) < 500
        log.log(logging.DEBUG if expected else logging.WARNING,
                "database transaction rolled back exception_type=%s", type(exc).__name__)
        db.rollback()
        raise
    finally:
        db.close()
