"""Centralised, validated configuration loaded from environment / .env."""
from functools import lru_cache

from cryptography.fernet import Fernet
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

GOOGLE_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.modify",  # read, send, drafts, threads
    "https://www.googleapis.com/auth/calendar",  # events + free/busy
]
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

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

    # --- LLM (Phase 6) ---
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_json_mode: bool = False  # DeepSeek R1 does not reliably support response_format
    gemini_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    llm_provider: str = "openrouter"
    llm_model: str = "deepseek/deepseek-r1"
    # Per-responsibility overrides. Empty = use the default above.
    intent_llm_provider: str = ""
    intent_llm_model: str = ""
    summary_llm_provider: str = ""
    summary_llm_model: str = ""
    task_llm_provider: str = ""
    task_llm_model: str = ""
    llm_timeout_seconds: float = 120.0
    llm_max_retries: int = 2
    llm_max_tokens: int = 4096  # R1 "thinking" tokens count against this

    # --- MCP ---
    # stdio: each Google MCP server runs as a subprocess speaking MCP over stdin/stdout.
    # inprocess: same servers and protocol over an in-memory transport (tests / single-process deployments).
    mcp_transport: str = "stdio"
    mcp_timeout_seconds: float = 60.0

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

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
