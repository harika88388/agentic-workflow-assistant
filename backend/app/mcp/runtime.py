"""Per-user service scopes for the MCP servers.

Tokens are loaded (and refreshed) from the encrypted credential store inside the server process; they never travel
through MCP messages. A stdio server learns WHICH user it serves from the MCP_USER_ID environment variable set by
the backend when it spawns the process.
"""
import logging
import os
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from mcp.server.mcpserver import MCPServer
from sqlalchemy.orm import Session

from app.integrations.calendar.client import CalendarClient
from app.integrations.calendar.service import CalendarService
from app.integrations.calendar.timeutil import get_zone
from app.integrations.gmail.client import GmailClient
from app.integrations.gmail.service import GmailService
from app.mcp.calendar_server import CalendarScope
from app.mcp.gmail_server import GmailScope
from app.services.auth_service import connection_status, get_google_credential
from app.services.token_service import TokenUnavailableError, get_valid_access_token

USER_ENV = "MCP_USER_ID"


def require_connected(db: Session, user_id: uuid.UUID, which: str) -> None:
    if not connection_status(get_google_credential(db, user_id))[which]:
        raise TokenUnavailableError(f"{'Gmail' if which == 'gmail' else 'Google Calendar'} is not connected; "
                                    "please reconnect your Google account")


def _zone_for(db: Session, user_id: uuid.UUID):
    from app.database.models import User
    user = db.get(User, user_id)
    try:
        return get_zone(user.timezone if user else "UTC")
    except ValueError:
        return get_zone("UTC")


# ---------- in-process (request-scoped DB session) ----------
def inprocess_gmail_scope(db: Session, user_id: uuid.UUID) -> GmailScope:
    @contextmanager
    def scope() -> Iterator[GmailService]:
        require_connected(db, user_id, "gmail")
        yield GmailService(GmailClient(lambda: get_valid_access_token(db, user_id)))
    return scope


def inprocess_calendar_scope(db: Session, user_id: uuid.UUID) -> CalendarScope:
    @contextmanager
    def scope() -> Iterator[CalendarService]:
        require_connected(db, user_id, "calendar")
        yield CalendarService(CalendarClient(lambda: get_valid_access_token(db, user_id)), _zone_for(db, user_id))
    return scope


# ---------- stdio subprocess (own short-lived DB session per tool call) ----------
@contextmanager
def _session() -> Iterator[Session]:
    from app.database.session import get_session_factory
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()


def stdio_gmail_scope(user_id: uuid.UUID) -> GmailScope:
    @contextmanager
    def scope() -> Iterator[GmailService]:
        with _session() as db:
            require_connected(db, user_id, "gmail")
            yield GmailService(GmailClient(lambda: get_valid_access_token(db, user_id)))
    return scope


def stdio_calendar_scope(user_id: uuid.UUID) -> CalendarScope:
    @contextmanager
    def scope() -> Iterator[CalendarService]:
        with _session() as db:
            require_connected(db, user_id, "calendar")
            yield CalendarService(CalendarClient(lambda: get_valid_access_token(db, user_id)), _zone_for(db, user_id))
    return scope


def run_stdio_server(build: Callable[[uuid.UUID], MCPServer]) -> None:
    # stdout carries the MCP protocol: all logging must go to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        user_id = uuid.UUID(os.environ[USER_ENV])
    except (KeyError, ValueError):
        sys.stderr.write(f"{USER_ENV} must be set to the user's UUID\n")
        raise SystemExit(2) from None
    build(user_id).run("stdio")
