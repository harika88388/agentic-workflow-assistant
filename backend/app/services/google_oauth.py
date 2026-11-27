"""Thin client for Google's OAuth 2.0 endpoints (Authorization Code flow, offline access)."""
import logging
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from google.auth.transport import requests as g_requests
from google.oauth2 import id_token as g_id_token

from app.core.config import GOOGLE_SCOPES, get_settings

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
TIMEOUT = 15.0


class GoogleOAuthError(Exception):
    pass


class InvalidGrantError(GoogleOAuthError):
    """Refresh token revoked/expired; user must re-consent."""


@dataclass(frozen=True)
class GoogleTokens:
    access_token: str
    refresh_token: str | None
    expires_in: int
    scope: str
    id_token: str | None


@dataclass(frozen=True)
class GoogleIdentity:
    sub: str
    email: str
    name: str | None
    picture: str | None


def build_authorization_url(state: str) -> str:
    s = get_settings()
    params = {
        "client_id": s.google_client_id,
        "redirect_uri": s.google_redirect_uri,
        "response_type": "code",
        "scope": " ".join(GOOGLE_SCOPES),
        "access_type": "offline",  # request a refresh token
        "prompt": "consent",  # ensure refresh token is re-issued
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _post(url: str, data: dict) -> dict:
    try:
        resp = httpx.post(url, data=data, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise GoogleOAuthError(f"network error contacting Google: {exc}") from exc
    if resp.status_code != 200:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if body.get("error") == "invalid_grant":
            raise InvalidGrantError("invalid_grant")
        raise GoogleOAuthError(f"Google returned {resp.status_code}: {body.get('error', 'unknown')}")
    return resp.json()


def exchange_code(code: str) -> GoogleTokens:
    s = get_settings()
    body = _post(
        TOKEN_URL,
        {
            "code": code,
            "client_id": s.google_client_id,
            "client_secret": s.google_client_secret,
            "redirect_uri": s.google_redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    try:
        access_token = body["access_token"]
    except (KeyError, TypeError) as exc:
        raise GoogleOAuthError("Google token response did not include access_token") from exc
    return GoogleTokens(
        access_token=access_token,
        refresh_token=body.get("refresh_token"),
        expires_in=int(body.get("expires_in", 3600)),
        scope=body.get("scope", ""),
        id_token=body.get("id_token"),
    )


def refresh_access_token(refresh_token: str) -> GoogleTokens:
    s = get_settings()
    body = _post(
        TOKEN_URL,
        {
            "refresh_token": refresh_token,
            "client_id": s.google_client_id,
            "client_secret": s.google_client_secret,
            "grant_type": "refresh_token",
        },
    )
    try:
        access_token = body["access_token"]
    except (KeyError, TypeError) as exc:
        raise GoogleOAuthError("Google refresh response did not include access_token") from exc
    return GoogleTokens(
        access_token=access_token,
        refresh_token=body.get("refresh_token"),
        expires_in=int(body.get("expires_in", 3600)),
        scope=body.get("scope", ""),
        id_token=body.get("id_token"),
    )


def verify_id_token(raw_id_token: str | None) -> GoogleIdentity:
    """Verify signature, audience and expiry of Google's ID token."""
    if not raw_id_token:
        raise GoogleOAuthError("missing id_token")
    try:
        claims = g_id_token.verify_oauth2_token(
            raw_id_token, g_requests.Request(), get_settings().google_client_id
        )
    except ValueError as exc:
        raise GoogleOAuthError(f"invalid id_token: {exc}") from exc
    if not claims.get("email_verified"):
        raise GoogleOAuthError("Google account email is not verified")
    try:
        sub = claims["sub"]
        email = claims["email"]
    except (KeyError, TypeError) as exc:
        raise GoogleOAuthError("Google identity response missing required claims") from exc
    if not isinstance(email, str) or not email.strip():
        raise GoogleOAuthError("Google identity response missing a valid email")
    return GoogleIdentity(
        sub=sub, email=email.lower(), name=claims.get("name"), picture=claims.get("picture")
    )


def revoke_token(token: str) -> None:
    """Best-effort revocation; failures are logged, not raised."""
    try:
        httpx.post(REVOKE_URL, data={"token": token}, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        log.warning("Google token revocation failed exception_type=%s", type(exc).__name__)
