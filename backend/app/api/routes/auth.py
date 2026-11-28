import logging
import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import GOOGLE_SCOPES, get_settings
from app.core.logging_context import bind
from app.core.security import (
    TokenError,
    create_access_token,
    create_state_token,
    decode_state_token,
    decrypt_secret,
    generate_nonce,
)
from app.database.models import User
from app.database.session import get_db
from app.schemas.user import ConnectionStatus, MeOut, TimezoneUpdate, UserOut
from app.services import auth_service, google_oauth

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])
STATE_COOKIE = "oauth_nonce"


def _login_error(reason: str) -> RedirectResponse:
    log.warning("google oauth sign-in failed reason=%s", reason)
    resp = RedirectResponse(f"{get_settings().frontend_url}/login?{urlencode({'error': reason})}", status_code=302)
    resp.delete_cookie(STATE_COOKIE, path="/api/auth")
    return resp


@router.get("/google/config")
def google_oauth_config() -> dict:
    """Public, non-secret OAuth settings in effect, for configuring the Google Cloud Console OAuth client.
    The client id is already public (it appears in the authorization URL); the secret is never returned."""
    s = get_settings()
    return {
        "configured": bool(s.google_client_id and s.google_client_secret),
        "client_id": s.google_client_id,
        "redirect_uri": s.google_redirect_uri,
        "javascript_origin": s.oauth_javascript_origin,
        "scopes": GOOGLE_SCOPES,
        "problems": s.oauth_config_problems(),
    }


@router.get("/google/login")
def google_login() -> RedirectResponse:
    """Start the Authorization Code flow. The browser is redirected to Google."""
    settings = get_settings()
    if not all((settings.google_client_id, settings.google_client_secret, settings.google_redirect_uri)):
        return _login_error("not_configured")
    nonce = generate_nonce()
    resp = RedirectResponse(google_oauth.build_authorization_url(create_state_token(nonce)), status_code=302)
    resp.set_cookie(
        STATE_COOKIE, nonce, max_age=600, httponly=True, samesite="lax",
        secure=settings.is_production, path="/api/auth",
    )
    log.info("google oauth sign-in started redirect_uri=%s scopes=%d", settings.google_redirect_uri, len(GOOGLE_SCOPES))
    return resp


@router.get("/google/callback")
def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if error:
        log.warning("google oauth callback returned an error google_error=%s", error[:64])
        return _login_error("access_denied")
    if not code or not state:
        return _login_error("invalid_request")

    # CSRF: signed state must match the nonce in the httpOnly cookie set at /login.
    cookie_nonce = request.cookies.get(STATE_COOKIE)
    try:
        nonce = decode_state_token(state)
    except TokenError:
        log.warning("google oauth state token invalid or expired")
        return _login_error("invalid_state")
    if not cookie_nonce or not secrets.compare_digest(cookie_nonce, nonce):
        log.warning("google oauth state does not match the browser cookie cookie_present=%s", bool(cookie_nonce))
        return _login_error("invalid_state")

    try:
        tokens = google_oauth.exchange_code(code)
        identity = google_oauth.verify_id_token(tokens.id_token)
    except google_oauth.GoogleOAuthError as exc:
        # The message carries Google's error code (e.g. redirect_uri_mismatch, invalid_grant), never a token.
        log.error("google oauth token exchange or identity verification failed error=%s redirect_uri=%s",
                  str(exc)[:200], get_settings().google_redirect_uri)
        return _login_error("oauth_failed")

    try:
        user = auth_service.upsert_user_with_credentials(db, identity, tokens)
        db.commit()
        # Only our own short-lived JWT goes to the browser (URL fragment: never sent to servers/logs).
        jwt_token = create_access_token(user.id)
    except Exception as exc:  # noqa: BLE001 - never expose database or encryption details
        db.rollback()
        log.error("google oauth callback persistence or session setup failed exception_type=%s",
                  type(exc).__name__, exc_info=True)
        return _login_error("oauth_failed")

    bind(user_id=str(user.id))
    log.info("google oauth sign-in completed granted_scopes=%d refresh_token_received=%s",
             len(tokens.scope.split()), bool(tokens.refresh_token))
    resp = RedirectResponse(
        f"{get_settings().frontend_url}/auth/callback#{urlencode({'token': jwt_token})}", status_code=302
    )
    resp.delete_cookie(STATE_COOKIE, path="/api/auth")
    return resp


@router.get("/me", response_model=MeOut)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> MeOut:
    cred = auth_service.get_google_credential(db, user.id)
    return MeOut(
        user=UserOut.model_validate(user),
        connections=ConnectionStatus(**auth_service.connection_status(cred)),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout() -> Response:
    """Sessions are stateless JWTs; the client discards its token. Endpoint exists for symmetry."""
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/google/connection", status_code=status.HTTP_204_NO_CONTENT)
def disconnect_google(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Response:
    """Revoke Google access and delete stored tokens."""
    cred = auth_service.get_google_credential(db, user.id)
    if cred is not None:
        token = decrypt_secret(cred.refresh_token_enc or cred.access_token_enc)
        google_oauth.revoke_token(token)
        db.delete(cred)
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/me/timezone", response_model=UserOut)
def update_timezone(body: TimezoneUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> User:
    """Used to resolve 'tomorrow', 'Friday 3 PM' etc. Must be an IANA name such as Asia/Kolkata."""
    user.timezone = body.timezone
    db.commit()
    return user
