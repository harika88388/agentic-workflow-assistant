import logging
import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import get_settings
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
    resp = RedirectResponse(f"{get_settings().frontend_url}/login?{urlencode({'error': reason})}", status_code=302)
    resp.delete_cookie(STATE_COOKIE, path="/api/auth")
    return resp


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
        return _login_error("access_denied")
    if not code or not state:
        return _login_error("invalid_request")

    # CSRF: signed state must match the nonce in the httpOnly cookie set at /login.
    cookie_nonce = request.cookies.get(STATE_COOKIE)
    try:
        nonce = decode_state_token(state)
    except TokenError:
        return _login_error("invalid_state")
    if not cookie_nonce or not secrets.compare_digest(cookie_nonce, nonce):
        return _login_error("invalid_state")

    try:
        tokens = google_oauth.exchange_code(code)
        identity = google_oauth.verify_id_token(tokens.id_token)
    except google_oauth.GoogleOAuthError:
        log.warning("Google OAuth callback exchange or identity verification failed")
        return _login_error("oauth_failed")

    try:
        user = auth_service.upsert_user_with_credentials(db, identity, tokens)
        db.commit()
        jwt_token = create_access_token(user.id)
    except Exception as exc:  # noqa: BLE001 - never expose database or encryption details
        db.rollback()
        log.warning("Google OAuth callback persistence or session setup failed: %s", type(exc).__name__)
        return _login_error("oauth_failed")

    # Only our own short-lived JWT goes to the browser (URL fragment: never sent to servers/logs).
    jwt_token = create_access_token(user.id)
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
