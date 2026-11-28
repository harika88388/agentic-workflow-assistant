"""Centralised, validated configuration loaded from environment / .env."""
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = BACKEND_DIR / ".env"

GOOGLE_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",  # drafts
    "https://www.googleapis.com/auth/calendar",  # events + free/busy
]
GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"  # legacy credentials remain supported
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
# Must match the route in app/api/routes/auth.py (router prefix /api + /auth + /google/callback).
OAUTH_CALLBACK_PATH = "/api/auth/google/callback"


def _origin(url: str) -> str:
    parts = urlsplit(url.strip())
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    database_url: str = "postgresql+psycopg://assistant:assistant@localhost:5432/assistant"

    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 480
    token_encryption_key: str

    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/api/auth/google/callback"

    frontend_url: str = "http://localhost:5173"
    cors_origins: str = "http://localhost:5173"
    # Set by Vercel at runtime (e.g. "agentic-workflow-assistant.vercel.app"). Used only as a fallback when the
    # public URLs above were left at their localhost defaults in production.
    vercel_project_production_url: str = ""

    # --- LLM (Phase 6) ---
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_json_mode: bool = False  # DeepSeek R1 does not reliably support response_format
    gemini_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    llm_provider: str = "openrouter"
    llm_model: str = "deepseek/deepseek-r1-0528"
    # Per-responsibility overrides. Empty = use the default above.
    intent_llm_provider: str = ""
    intent_llm_model: str = ""
    summary_llm_provider: str = ""
    summary_llm_model: str = ""
    task_llm_provider: str = ""
    task_llm_model: str = ""
    llm_timeout_seconds: float = 120.0
    llm_max_retries: int = 2
    # Total LLM time per conversation turn; keeps a turn inside the 300 s Vercel function limit (vercel.json).
    llm_turn_budget_seconds: float = 240.0
    llm_max_tokens: int = 4096  # R1 "thinking" tokens count against this

    # --- MCP ---
    # stdio: each Google MCP server runs as a subprocess speaking MCP over stdin/stdout.
    # inprocess: same servers and protocol over an in-memory transport (tests / single-process deployments).
    mcp_transport: str = "stdio"
    mcp_timeout_seconds: float = 60.0

    @field_validator("database_url")
    @classmethod
    def _psycopg_driver(cls, v: str) -> str:
        # Hosted providers (Neon, Supabase, Vercel Marketplace) hand out plain postgres:// URLs.
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix):]
        return v

    @field_validator("mcp_transport")
    @classmethod
    def _known_transport(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"stdio", "inprocess"}:
            raise ValueError("MCP_TRANSPORT must be 'stdio' or 'inprocess'")
        return v

    @field_validator("jwt_secret")
    @classmethod
    def _jwt_secret_strong(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters")
        return v

    @field_validator("token_encryption_key")
    @classmethod
    def _valid_fernet_key(cls, v: str) -> str:
        try:
            Fernet(v.encode())
        except Exception as exc:  # noqa: BLE001
            raise ValueError("TOKEN_ENCRYPTION_KEY must be a valid Fernet key") from exc
        return v

    @field_validator("llm_provider", "intent_llm_provider", "summary_llm_provider", "task_llm_provider")
    @classmethod
    def _known_provider(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"", "openrouter", "gemini"}:
            raise ValueError("provider must be 'openrouter' or 'gemini'")
        return v

    @model_validator(mode="after")
    def _llm_overrides_consistent(self) -> "Settings":
        if not self.llm_provider:
            raise ValueError("LLM_PROVIDER must not be empty")
        for prefix in ("intent", "summary", "task"):
            prov = getattr(self, f"{prefix}_llm_provider")
            model = getattr(self, f"{prefix}_llm_model")
            if prov and prov != self.llm_provider and not model:
                raise ValueError(
                    f"{prefix.upper()}_LLM_PROVIDER differs from LLM_PROVIDER, so {prefix.upper()}_LLM_MODEL must be set"
                )
        return self

    @model_validator(mode="after")
    def _derive_public_urls(self) -> "Settings":
        host = self.vercel_project_production_url.strip().removeprefix("https://").strip("/")
        if host and self.is_production:
            base = f"https://{host}"
            if "localhost" in self.frontend_url:
                self.frontend_url = base
            if "localhost" in self.cors_origins:
                self.cors_origins = base
            if "localhost" in self.google_redirect_uri:
                self.google_redirect_uri = base + OAUTH_CALLBACK_PATH
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def oauth_javascript_origin(self) -> str:
        return _origin(self.frontend_url)

    def oauth_config_problems(self) -> list[str]:
        """Misconfigurations that make Google reject the sign-in (redirect_uri_mismatch) or break the callback."""
        problems = []
        uri = self.google_redirect_uri.strip()
        if not (self.google_client_id and self.google_client_secret):
            problems.append("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set")
        if urlsplit(uri).path != OAUTH_CALLBACK_PATH:
            problems.append(f"GOOGLE_REDIRECT_URI path must be {OAUTH_CALLBACK_PATH}")
        if self.is_production:
            if not uri.startswith("https://") or "localhost" in uri:
                problems.append("GOOGLE_REDIRECT_URI must be the public https URL in production")
            if "localhost" in self.frontend_url:
                problems.append("FRONTEND_URL points to localhost in production")
            if _origin(uri) != _origin(self.frontend_url):
                problems.append("GOOGLE_REDIRECT_URI and FRONTEND_URL are on different origins")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
