from datetime import datetime, timedelta, timezone

import pytest

from app.core.security import decrypt_secret, encrypt_secret
from app.database.models import OAuthCredential
from app.services import google_oauth, token_service


def _cred(db, user, expires_in, refresh="r1"):
    c = OAuthCredential(
        user_id=user.id, provider="google", access_token_enc=encrypt_secret("old"),
        refresh_token_enc=encrypt_secret(refresh) if refresh else None,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in), scopes="openid",
    )
    db.add(c)
    db.commit()
    return c


def test_returns_existing_token_when_fresh(db_session, make_user):
    u, _ = make_user()
    _cred(db_session, u, 3600)
    assert token_service.get_valid_access_token(db_session, u.id) == "old"


def test_refreshes_when_expired(db_session, make_user, monkeypatch):
    u, _ = make_user()
    c = _cred(db_session, u, -10)
    monkeypatch.setattr(
        google_oauth, "refresh_access_token", lambda rt: google_oauth.GoogleTokens("new", None, 3600, "", None)
    )
    assert token_service.get_valid_access_token(db_session, u.id) == "new"
    assert decrypt_secret(c.access_token_enc) == "new"
    assert decrypt_secret(c.refresh_token_enc) == "r1"


def test_revoked_refresh_token_marks_credential_and_raises(db_session, make_user, monkeypatch):
    u, _ = make_user()
    c = _cred(db_session, u, -10)

    def bad(rt):
        raise google_oauth.InvalidGrantError("invalid_grant")

    monkeypatch.setattr(google_oauth, "refresh_access_token", bad)
    with pytest.raises(token_service.TokenUnavailableError):
        token_service.get_valid_access_token(db_session, u.id)
    assert c.status == "revoked"
    with pytest.raises(token_service.TokenUnavailableError):
        token_service.get_valid_access_token(db_session, u.id)


def test_no_credential_raises(db_session, make_user):
    u, _ = make_user()
    with pytest.raises(token_service.TokenUnavailableError):
        token_service.get_valid_access_token(db_session, u.id)
