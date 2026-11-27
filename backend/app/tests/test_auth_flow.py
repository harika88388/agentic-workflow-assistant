from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from sqlalchemy import select

from app.core.security import create_state_token, decrypt_secret
from app.services import auth_service, google_oauth
from app.database.models import OAuthCredential, User


def _set_nonce(client, nonce):
    client.cookies.clear()  # the client persists cookies; keep exactly one known value
    client.cookies.set("oauth_nonce", nonce, path="/api/auth")


def _start_login(client):
    r = client.get("/api/auth/google/login")
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    return q, r.cookies.get("oauth_nonce")


def test_login_builds_google_url_with_offline_access(client):
    q, nonce = _start_login(client)
    assert q["access_type"] == ["offline"] and q["response_type"] == ["code"]
    scopes = set(q["scope"][0].split())
    assert "https://www.googleapis.com/auth/gmail.readonly" in scopes
    assert "https://www.googleapis.com/auth/gmail.send" in scopes
    assert "https://www.googleapis.com/auth/gmail.compose" in scopes
    assert "https://www.googleapis.com/auth/calendar" in scopes
    assert q["prompt"] == ["consent"]
    assert nonce


def test_login_missing_google_credentials_is_safe(client, monkeypatch):
    from app.api.routes import auth as auth_route

    settings = auth_route.get_settings().model_copy(update={
        "google_client_id": "",
        "google_client_secret": "",
        "google_redirect_uri": "",
    })
    monkeypatch.setattr(auth_route, "get_settings", lambda: settings)

    r = client.get("/api/auth/google/login")
    assert r.status_code == 302
    assert "error=not_configured" in r.headers["location"]


def test_login_redirect_is_google_and_contains_expected_query(client):
    r = client.get("/api/auth/google/login")
    assert r.status_code == 302
    loc = r.headers["location"]
    parsed = urlparse(loc)
    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.google.com"
    query = parse_qs(parsed.query)
    assert query["redirect_uri"] and query["client_id"]
    assert set(query["scope"][0].split())


def test_callback_success_creates_user_stores_encrypted_tokens(client, db_session, mock_google):
    q, nonce = _start_login(client)
    _set_nonce(client, nonce)
    r = client.get("/api/auth/google/callback", params={"code": "abc", "state": q["state"][0]})
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("http://localhost:5173/auth/callback#token=")
    assert "access-1" not in loc and "refresh-1" not in loc  # Google tokens never reach the browser

    user = db_session.scalar(select(User))
    cred = db_session.scalar(select(OAuthCredential))
    assert user.email == "user@example.com"
    assert cred.access_token_enc != "access-1" and decrypt_secret(cred.access_token_enc) == "access-1"
    assert decrypt_secret(cred.refresh_token_enc) == "refresh-1"


def test_me_reports_connection_status_and_requires_auth(client, mock_google):
    assert client.get("/api/auth/me").status_code == 401
    q, nonce = _start_login(client)
    _set_nonce(client, nonce)
    loc = client.get("/api/auth/google/callback", params={"code": "c", "state": q["state"][0]}).headers["location"]
    token = parse_qs(loc.split("#", 1)[1])["token"][0]
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["connections"] == {"gmail": True, "calendar": True}
    assert r.json()["user"]["email"] == "user@example.com"


def test_partial_scopes_report_gmail_only(client, mock_google):
    mock_google["scope"] = "openid email https://www.googleapis.com/auth/gmail.modify"
    q, nonce = _start_login(client)
    _set_nonce(client, nonce)
    loc = client.get("/api/auth/google/callback", params={"code": "c", "state": q["state"][0]}).headers["location"]
    token = parse_qs(loc.split("#", 1)[1])["token"][0]
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.json()["connections"] == {"gmail": True, "calendar": False}


def test_new_scoped_gmail_permissions_report_connected(client, mock_google):
    mock_google["scope"] = (
        "openid email https://www.googleapis.com/auth/gmail.readonly "
        "https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/gmail.compose"
    )
    q, nonce = _start_login(client)
    _set_nonce(client, nonce)
    loc = client.get("/api/auth/google/callback", params={"code": "c", "state": q["state"][0]}).headers["location"]
    token = parse_qs(loc.split("#", 1)[1])["token"][0]
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.json()["connections"] == {"gmail": True, "calendar": False}


def test_callback_rejects_missing_cookie_and_forged_state(client, mock_google):
    q, _ = _start_login(client)
    client.cookies.clear()  # no nonce cookie sent
    r = client.get("/api/auth/google/callback", params={"code": "c", "state": q["state"][0]})
    assert "error=invalid_state" in r.headers["location"]
    _set_nonce(client, "other")
    r = client.get("/api/auth/google/callback", params={"code": "c", "state": create_state_token("different")})
    assert "error=invalid_state" in r.headers["location"]
    r = client.get("/api/auth/google/callback", params={"code": "c", "state": "garbage"})
    assert "error=invalid_state" in r.headers["location"]


def test_callback_handles_user_denial_and_google_failure(client, monkeypatch):
    r = client.get("/api/auth/google/callback", params={"error": "access_denied"})
    assert "error=access_denied" in r.headers["location"]

    def boom(code):
        raise google_oauth.GoogleOAuthError("nope")

    monkeypatch.setattr(google_oauth, "exchange_code", boom)
    q, nonce = _start_login(client)
    _set_nonce(client, nonce)
    r = client.get("/api/auth/google/callback", params={"code": "c", "state": q["state"][0]})
    assert "error=oauth_failed" in r.headers["location"]


def test_callback_missing_code_or_state_redirects_safely(client):
    r = client.get("/api/auth/google/callback")
    assert r.status_code == 302
    assert "error=invalid_request" in r.headers["location"]


def test_callback_identity_failure_redirects_safely(client, monkeypatch):
    monkeypatch.setattr(
        google_oauth,
        "verify_id_token",
        lambda token: (_ for _ in ()).throw(google_oauth.GoogleOAuthError("identity failed")),
    )
    q, nonce = _start_login(client)
    _set_nonce(client, nonce)
    r = client.get("/api/auth/google/callback", params={"code": "c", "state": q["state"][0]})
    assert r.status_code == 302
    assert "error=oauth_failed" in r.headers["location"]


def test_callback_database_persistence_failure_is_safe(client, monkeypatch):
    def persist_failure(db, identity, tokens):
        raise RuntimeError("database failure")

    monkeypatch.setattr(auth_service, "upsert_user_with_credentials", persist_failure)
    q, nonce = _start_login(client)
    _set_nonce(client, nonce)
    r = client.get("/api/auth/google/callback", params={"code": "c", "state": q["state"][0]})
    assert r.status_code == 302
    assert "error=oauth_failed" in r.headers["location"]


def test_callback_token_encryption_failure_is_safe(client, monkeypatch, mock_google):
    monkeypatch.setattr(auth_service, "encrypt_secret", lambda secret: (_ for _ in ()).throw(RuntimeError("encryption failed")))
    q, nonce = _start_login(client)
    _set_nonce(client, nonce)
    r = client.get("/api/auth/google/callback", params={"code": "c", "state": q["state"][0]})
    assert r.status_code == 302
    assert "error=oauth_failed" in r.headers["location"]


def test_callback_jwt_generation_failure_is_safe(client, monkeypatch, mock_google):
    from app.api.routes import auth as auth_route

    monkeypatch.setattr(auth_route, "create_access_token", lambda user_id: (_ for _ in ()).throw(RuntimeError("JWT failed")))
    q, nonce = _start_login(client)
    _set_nonce(client, nonce)
    r = client.get("/api/auth/google/callback", params={"code": "c", "state": q["state"][0]})
    assert r.status_code == 302
    assert "error=oauth_failed" in r.headers["location"]


def test_relogin_without_refresh_token_keeps_existing_one(client, db_session, mock_google):
    for _ in range(2):
        q, nonce = _start_login(client)
        _set_nonce(client, nonce)
        client.get("/api/auth/google/callback", params={"code": "c", "state": q["state"][0]})
        mock_google["refresh_token"] = None
    creds = db_session.scalars(select(OAuthCredential)).all()
    assert len(creds) == 1 and decrypt_secret(creds[0].refresh_token_enc) == "refresh-1"


def test_disconnect_revokes_and_deletes_credentials(client, db_session, mock_google):
    q, nonce = _start_login(client)
    _set_nonce(client, nonce)
    loc = client.get("/api/auth/google/callback", params={"code": "c", "state": q["state"][0]}).headers["location"]
    h = {"Authorization": f"Bearer {parse_qs(loc.split('#', 1)[1])['token'][0]}"}
    assert client.delete("/api/auth/google/connection", headers=h).status_code == 204
    assert mock_google["revoked"] == "refresh-1"
    assert db_session.scalar(select(OAuthCredential)) is None
    assert client.get("/api/auth/me", headers=h).json()["connections"] == {"gmail": False, "calendar": False}
