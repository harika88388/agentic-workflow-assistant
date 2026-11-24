"""Role-based model routing. One model can serve every role; each role can be overridden independently by env."""
from dataclasses import dataclass
from enum import Enum

from app.core.config import Settings
from app.services.llm.base import ChatMessage, LLMConfigError, LLMProvider


class LLMRole(str, Enum):
    INTENT = "intent"
    SUMMARIZATION = "summarization"
    TASK = "task_automation"


_PREFIX = {LLMRole.INTENT: "intent", LLMRole.SUMMARIZATION: "summary", LLMRole.TASK: "task"}
_TEMPERATURE = {LLMRole.INTENT: 0.1, LLMRole.SUMMARIZATION: 0.3, LLMRole.TASK: 0.1}


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
        roles = {r.value: {"provider": c.provider, "model": c.model, "source": "override" if c.overridden else "default"}
                 for r in LLMRole for c in [self.config_for(r)]}
        return {"roles": roles, "distinct_models": len({(v["provider"], v["model"]) for v in roles.values()})}

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
        return self._provider(cfg.provider).complete(
            messages, model=cfg.model, temperature=cfg.temperature, max_tokens=cfg.max_tokens,
            json_mode=json_mode and cfg.json_mode)
