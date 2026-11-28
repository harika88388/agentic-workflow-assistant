import logging
import uuid

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.logging_context import bind
from app.core.security import TokenError, decode_access_token
from app.database.models import User
from app.database.session import get_db

bearer = HTTPBearer(auto_error=False)
log = logging.getLogger("app.auth")


def _unauthorized(reason: str, detail: str | dict = "Not authenticated") -> HTTPException:
    log.warning("authentication failed reason=%s", reason)
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"})


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = Depends(get_db)
) -> User:
    if creds is None:
        raise _unauthorized("missing_token")
    try:
        user_id: uuid.UUID = decode_access_token(creds.credentials)
    except TokenError as exc:
        if isinstance(exc.__cause__, jwt.ExpiredSignatureError):
            raise _unauthorized("session_expired", {"code": "session_expired",
                                                    "message": "Your session has expired. Please sign in again."}) from None
        raise _unauthorized("invalid_token") from None
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise _unauthorized("unknown_or_inactive_user")
    bind(user_id=str(user.id))
    log.debug("authenticated user_id=%s", user.id)
    return user


# ---------- Google service factories (Phases 4-5) ----------
from zoneinfo import ZoneInfo  # noqa: E402

from app.integrations.calendar.client import CalendarClient  # noqa: E402
from app.integrations.calendar.service import CalendarService  # noqa: E402
from app.integrations.gmail.client import GmailClient  # noqa: E402
from app.integrations.gmail.service import GmailService  # noqa: E402
from app.services.auth_service import connection_status, get_google_credential  # noqa: E402
from app.services.token_service import get_valid_access_token  # noqa: E402


def _require(db: Session, user: User, which: str) -> None:
    if not connection_status(get_google_credential(db, user.id))[which]:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"code": "google_reconnect_required", "message": f"{which.title()} is not connected."},
        )


def get_gmail_service(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> GmailService:
    _require(db, user, "gmail")
    return GmailService(GmailClient(lambda: get_valid_access_token(db, user.id)))


def user_zone(user: User) -> ZoneInfo:
    try:
        return ZoneInfo(user.timezone)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def get_calendar_service(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> CalendarService:
    _require(db, user, "calendar")
    return CalendarService(CalendarClient(lambda: get_valid_access_token(db, user.id)), user_zone(user))


# ---------- MCP ----------
def get_mcp_gateway_factory():
    """Returns `(db, user) -> MCPGateway` bound to that user's credentials (overridable in tests)."""
    from app.core.config import get_settings
    from app.mcp.client import gateway_for_user

    settings = get_settings()
    return lambda db, user: gateway_for_user(db, user.id, settings)
