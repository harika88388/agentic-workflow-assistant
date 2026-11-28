"""Production OAuth URLs: the redirect URI sent to Google, the one used in the token exchange, and the Console values
are all derived from the same setting, and production misconfiguration is detected instead of silently used."""
import logging
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.api.routes import auth as auth_route
from app.core.config import OAUTH_CALLBACK_PATH
from app.services import google_oauth
from app.tests.fakes import settings as make_settings

PROD = "https://agentic-workflow-assistant.vercel.app"
PROD_CALLBACK = PROD + "/api/auth/google/callback"


def prod_settings(**overrides):
    values = {"app_env": "production", "google_client_id": "cid.apps.googleusercontent.com",
              "google_client_secret": "secret", "google_redirect_uri": PROD_CALLBACK, "frontend_url": PROD,
              "cors_origins": PROD} | overrides
    return make_settings(**values)


@pytest.fixture()
def prod(monkeypatch):
    s = prod_settings()
    monkeypatch.setattr(auth_route, "get_settings", lambda: s)
    monkeypatch.setattr(google_oauth, "get_settings", lambda: s)
    return s


def test_callback_path_constant_matches_the_registered_route(client):
    from app.main import app
    assert OAUTH_CALLBACK_PATH in app.openapi()["paths"]


def start_login(client) -> str:
    """Begin sign-in and return the state. The production cookie is Secure, which the http:// test client won't
    resend, so it is copied into the jar explicitly (as a browser on https would send it)."""
    r = client.get("/api/auth/google/login")
    client.cookies.clear()
    client.cookies.set("oauth_nonce", r.cookies.get("oauth_nonce"), path="/api/auth")
    return parse_qs(urlparse(r.headers["location"]).query)["state"][0]


def test_login_sends_the_production_callback_to_google(client, prod):
    r = client.get("/api/auth/google/login")
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["redirect_uri"] == [PROD_CALLBACK] and q["client_id"] == ["cid.apps.googleusercontent.com"]
    assert "Secure" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]


def test_token_exchange_uses_the_same_redirect_uri(prod, monkeypatch):
    sent = {}

    def fake_post(url, data, timeout):
        sent.update(url=url, **data)
        return httpx.Response(200, json={"access_token": "a", "refresh_token": "r", "expires_in": 3600,
                                         "scope": "openid", "id_token": "i"})
    monkeypatch.setattr(google_oauth.httpx, "post", fake_post)
    tokens = google_oauth.exchange_code("the-code")
    assert sent["url"] == google_oauth.TOKEN_URL and sent["redirect_uri"] == PROD_CALLBACK
    assert sent["grant_type"] == "authorization_code" and sent["code"] == "the-code" and tokens.refresh_token == "r"


def test_google_error_codes_are_surfaced_and_logged_without_secrets(client, prod, monkeypatch, caplog):
    monkeypatch.setattr(google_oauth.httpx, "post", lambda url, data, timeout: httpx.Response(
        400, json={"error": "redirect_uri_mismatch"}))
    state = start_login(client)
    with caplog.at_level(logging.INFO):
        r = client.get("/api/auth/google/callback", params={"code": "4/SECRETCODE", "state": state})
    assert r.status_code == 302 and r.headers["location"] == f"{PROD}/login?error=oauth_failed"
    assert "redirect_uri_mismatch" in caplog.text and PROD_CALLBACK in caplog.text
    assert "4/SECRETCODE" not in caplog.text and "secret" not in caplog.text.replace("SECRETCODE", "")


def test_successful_production_callback_creates_a_session(client, prod, mock_google, caplog):
    state = start_login(client)
    with caplog.at_level(logging.INFO):
        r = client.get("/api/auth/google/callback", params={"code": "abc", "state": state})
    assert r.headers["location"].startswith(f"{PROD}/auth/callback#token=")
    token = r.headers["location"].split("#token=", 1)[1]
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert "google oauth sign-in completed" in caplog.text and token not in caplog.text


def test_config_endpoint_reports_console_values(client, prod):
    body = client.get("/api/auth/google/config").json()
    assert body == {"configured": True, "client_id": "cid.apps.googleusercontent.com", "redirect_uri": PROD_CALLBACK,
                    "javascript_origin": PROD, "scopes": google_oauth.GOOGLE_SCOPES, "problems": []}
    assert "secret" not in str(body)


def test_production_urls_fall_back_to_the_vercel_domain_never_localhost():
    s = prod_settings(google_redirect_uri="http://localhost:8000/api/auth/google/callback",
                      frontend_url="http://localhost:5173", cors_origins="http://localhost:5173",
                      vercel_project_production_url="agentic-workflow-assistant.vercel.app")
    assert s.google_redirect_uri == PROD_CALLBACK and s.frontend_url == PROD and s.cors_origin_list == [PROD]
    assert s.oauth_config_problems() == []


def test_production_misconfiguration_is_detected():
    s = prod_settings(google_redirect_uri="http://localhost:8000/api/auth/google/callback")
    problems = s.oauth_config_problems()
    assert any("public https URL" in p for p in problems) and any("different origins" in p for p in problems)
    assert any("path must be" in p for p in prod_settings(google_redirect_uri=PROD + "/callback").oauth_config_problems())
    dev = make_settings(google_client_id="x", google_client_secret="y")
    assert dev.oauth_config_problems() == []  # localhost is correct for local development


def test_readiness_reports_configuration_without_secrets(client):
    body = client.get("/api/health/ready").json()
    assert body["database"]["connected"] is True and body["database"]["expected_revision"]
    assert set(body["oauth"]) == {"configured", "redirect_uri", "javascript_origin", "problems"}
    assert body["security"] == {"jwt_secret_configured": True, "token_encryption_key_configured": True}
    assert "xxxxxxxx" not in str(body)  # the test JWT secret
