import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import register_exception_handlers
from app.api.router import api_router
from app.core.config import get_settings
from app.core.log_config import RequestContextMiddleware, configure_logging

configure_logging()
log = logging.getLogger("app.startup")


def _log_configuration(settings) -> None:
    """One non-secret summary per cold start: which URLs and providers are in effect, and whether secrets are set."""
    log.info("application configured env=%s redirect_uri=%s frontend_url=%s mcp_transport=%s llm_provider=%s "
             "llm_model=%s google_oauth_configured=%s llm_key_configured=%s",
             settings.app_env, settings.google_redirect_uri, settings.frontend_url, settings.mcp_transport,
             settings.llm_provider, settings.llm_model, bool(settings.google_client_id and settings.google_client_secret),
             bool(settings.openrouter_api_key if settings.llm_provider == "openrouter" else settings.gemini_api_key))
    for problem in settings.oauth_config_problems():
        log.critical("OAuth configuration problem: %s", problem)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Agentic Workflow Assistant", version="0.2.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "PATCH"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(RequestContextMiddleware)  # outermost: times and correlates everything, incl. CORS
    register_exception_handlers(app)
    app.include_router(api_router)
    _log_configuration(settings)
    return app


app = create_app()
