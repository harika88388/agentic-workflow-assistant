"""Return a valid Google access token for a user, refreshing transparently when needed."""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.security import decrypt_secret, encrypt_secret
from app.services import google_oauth
from app.services.auth_service import get_google_credential

log = logging.getLogger(__name__)
REFRESH_MARGIN = timedelta(seconds=60)


class TokenUnavailableError(Exception):
    """User has no usable Google credential; they must reconnect."""


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # SQLite returns naive datetimes


def get_valid_access_token(db: Session, user_id: uuid.UUID) -> str:
    cred = get_google_credential(db, user_id)
    if cred is None or cred.status != "active":
        raise TokenUnavailableError("Google account is not connected")

    if _aware(cred.expires_at) - datetime.now(timezone.utc) > REFRESH_MARGIN:
        return decrypt_secret(cred.access_token_enc)

    if not cred.refresh_token_enc:
        cred.status = "revoked"
        db.commit()
        raise TokenUnavailableError("Access token expired and no refresh token is available")

    try:
        fresh = google_oauth.refresh_access_token(decrypt_secret(cred.refresh_token_enc))
    except google_oauth.InvalidGrantError as exc:
        cred.status = "revoked"
        db.commit()
        raise TokenUnavailableError("Google access was revoked; please reconnect") from exc
    except google_oauth.GoogleOAuthError as exc:
        raise TokenUnavailableError(f"Could not refresh Google token: {exc}") from exc

    cred.access_token_enc = encrypt_secret(fresh.access_token)
    if fresh.refresh_token:
        cred.refresh_token_enc = encrypt_secret(fresh.refresh_token)
    cred.expires_at = datetime.now(timezone.utc) + timedelta(seconds=fresh.expires_in)
    if fresh.scope:
        cred.scopes = fresh.scope
    db.commit()
    return fresh.access_token
