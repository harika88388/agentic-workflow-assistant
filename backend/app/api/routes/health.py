import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.database.session import get_db

router = APIRouter(prefix="/health", tags=["health"])
log = logging.getLogger("app.health")
# The newest Alembic revision this code expects (kept in sync with alembic/versions by a test).
EXPECTED_DB_REVISION = "0002"


@router.get("")
def liveness() -> dict:
    return {"status": "ok"}


@router.get("/ready")
def readiness(db: Session = Depends(get_db)):
    """Deployment self-check. Reports configuration state only: booleans and public URLs, never secret values."""
    s = get_settings()
    try:
        db.execute(text("SELECT 1"))
        connected = True
    except Exception as exc:  # noqa: BLE001
        log.error("readiness check: database unavailable exception_type=%s", type(exc).__name__)
        db.rollback()
        connected = False
    revision = None
    if connected:
        try:
            revision = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
        except Exception:  # noqa: BLE001 - table missing: migrations never ran (or a test database)
            db.rollback()
    if connected and revision != EXPECTED_DB_REVISION:
        log.warning("database migrations not up to date revision=%s expected=%s", revision, EXPECTED_DB_REVISION)
    key = s.openrouter_api_key if s.llm_provider == "openrouter" else s.gemini_api_key
    problems = s.oauth_config_problems()
    body = {
        "status": "ready" if connected else "unavailable",
        "environment": s.app_env,
        "database": {"connected": connected, "revision": revision, "expected_revision": EXPECTED_DB_REVISION,
                     "migrations_current": revision == EXPECTED_DB_REVISION},
        "oauth": {"configured": bool(s.google_client_id and s.google_client_secret),
                  "redirect_uri": s.google_redirect_uri, "javascript_origin": s.oauth_javascript_origin,
                  "problems": problems},
        "security": {"jwt_secret_configured": bool(s.jwt_secret), "token_encryption_key_configured":
                     bool(s.token_encryption_key)},
        "llm": {"provider": s.llm_provider, "model": s.llm_model, "api_key_configured": bool(key)},
        "mcp_transport": s.mcp_transport,
    }
    return JSONResponse(body, status_code=200 if connected else 503)
