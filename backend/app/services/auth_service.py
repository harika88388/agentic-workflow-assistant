"""User + credential persistence for the OAuth callback."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import CALENDAR_SCOPE, GMAIL_SCOPE
from app.core.security import encrypt_secret
from app.database.models import OAuthCredential, User
from app.services.google_oauth import GoogleIdentity, GoogleTokens


def upsert_user_with_credentials(db: Session, identity: GoogleIdentity, tokens: GoogleTokens) -> User:
    now = datetime.now(timezone.utc)
    user = db.scalar(select(User).where(User.google_sub == identity.sub))
    if user is None:
        user = User(google_sub=identity.sub, email=identity.email)
        db.add(user)
    user.email = identity.email
    user.name = identity.name
    user.picture_url = identity.picture
    user.last_login_at = now
    db.flush()

    cred = db.scalar(
        select(OAuthCredential).where(OAuthCredential.user_id == user.id, OAuthCredential.provider == "google")
    )
    if cred is None:
        cred = OAuthCredential(user_id=user.id, provider="google")
        db.add(cred)
    cred.access_token_enc = encrypt_secret(tokens.access_token)
    # Google omits refresh_token on re-consent in some cases: keep the existing one.
    if tokens.refresh_token:
        cred.refresh_token_enc = encrypt_secret(tokens.refresh_token)
    cred.expires_at = now + timedelta(seconds=tokens.expires_in)
    cred.scopes = tokens.scope
    cred.status = "active"
    db.flush()
    return user


def connection_status(cred: OAuthCredential | None) -> dict[str, bool]:
    if cred is None or cred.status != "active":
        return {"gmail": False, "calendar": False}
    scopes = cred.scope_set
    return {"gmail": GMAIL_SCOPE in scopes, "calendar": CALENDAR_SCOPE in scopes}


def get_google_credential(db: Session, user_id) -> OAuthCredential | None:
    return db.scalar(
        select(OAuthCredential).where(OAuthCredential.user_id == user_id, OAuthCredential.provider == "google")
    )
