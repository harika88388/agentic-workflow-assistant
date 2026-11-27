"""Role-based model routing. One model can serve every role; each role can be overridden independently by env."""
import logging
import time
from dataclasses import dataclass
from enum import Enum

from app.core.config import Settings
from app.core.logging_context import current_correlation_id
from app.services.llm.base import ChatMessage, LLMConfigError, LLMProvider


class LLMRole(str, Enum):
    INTENT = "intent"
    SUMMARIZATION = "summarization"
    EMAIL_UNDERSTANDING = "email_understanding"
    TASK = "task_automation"


_PREFIX = {LLMRole.INTENT: "intent", LLMRole.SUMMARIZATION: "summary",
           LLMRole.EMAIL_UNDERSTANDING: "summary", LLMRole.TASK: "task"}
_TEMPERATURE = {LLMRole.INTENT: 0.1, LLMRole.SUMMARIZATION: 0.3,
                LLMRole.EMAIL_UNDERSTANDING: 0.2, LLMRole.TASK: 0.1}
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    model: str
    temperature: float
    max_tokens: int
    json_mode: bool
    overridden: bool


class LLMRouter:
    def __init__(self, settings: Settings, providers: dict[str, LLMProvider] | None = None):
        self._s = settings
        self._providers: dict[str, LLMProvider] = dict(providers or {})

    def config_for(self, role: LLMRole) -> ModelConfig:
        s, p = self._s, _PREFIX[role]
        prov_o, model_o = getattr(s, f"{p}_llm_provider"), getattr(s, f"{p}_llm_model")
        provider = prov_o or s.llm_provider
        return ModelConfig(
            provider=provider,
            model=model_o or s.llm_model,
            temperature=_TEMPERATURE[role],
            max_tokens=s.llm_max_tokens,
            json_mode=provider == "gemini" or s.openrouter_json_mode,
            overridden=bool(prov_o or model_o),
        )

    def describe(self) -> dict:
        """What is configured for each role (no secrets). `distinct_models` reflects reality, not aspiration."""
        keys = {"openrouter": bool(self._s.openrouter_api_key), "gemini": bool(self._s.gemini_api_key)}
        roles = {r.value: {"provider": c.provider, "model": c.model, "source": "override" if c.overridden else "default",
                           "provider_ready": keys.get(c.provider, False) or c.provider in self._providers}
                 for r in LLMRole for c in [self.config_for(r)]}
        return {"roles": roles, "distinct_models": len({(v["provider"], v["model"]) for v in roles.values()})}

    def call_record(self, role: LLMRole) -> dict:
        """Which provider/model actually serves `role`, for execution traces."""
        cfg = self.config_for(role)
        return {"role": role.value, "provider": cfg.provider, "model": cfg.model}

    def _provider(self, name: str) -> LLMProvider:
        if name not in self._providers:
            s = self._s
            if name == "openrouter":
                from app.services.llm.openrouter import OpenRouterProvider
                self._providers[name] = OpenRouterProvider(
                    s.openrouter_api_key, s.openrouter_base_url, s.llm_timeout_seconds, s.llm_max_retries, s.frontend_url)
            elif name == "gemini":
                from app.services.llm.gemini import GeminiProvider
                self._providers[name] = GeminiProvider(
                    s.gemini_api_key, s.gemini_base_url, s.llm_timeout_seconds, s.llm_max_retries)
            else:
                raise LLMConfigError(f"unknown provider {name!r}")
        return self._providers[name]

    def complete(self, role: LLMRole, messages: list[ChatMessage], *, json_mode: bool = False) -> str:
        cfg = self.config_for(role)
        request_id = current_correlation_id()
        started = time.perf_counter()
        log.info("LLM provider selected request_id=%s role=%s provider=%s model=%s",
                 request_id, role.value, cfg.provider, cfg.model)
        try:
            result = self._provider(cfg.provider).complete(
                messages, model=cfg.model, temperature=cfg.temperature, max_tokens=cfg.max_tokens,
                json_mode=json_mode and cfg.json_mode)
        except Exception as exc:
            log.error("LLM execution failed request_id=%s role=%s provider=%s model=%s exception_type=%s "
                      "duration_ms=%d", request_id, role.value, cfg.provider, cfg.model, type(exc).__name__,
                      round((time.perf_counter() - started) * 1000))
            raise
        log.info("LLM execution completed request_id=%s role=%s provider=%s model=%s status=success duration_ms=%d",
                 request_id, role.value, cfg.provider, cfg.model,
                 round((time.perf_counter() - started) * 1000))
        return result
