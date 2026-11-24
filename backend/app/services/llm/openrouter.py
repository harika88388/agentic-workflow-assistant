import httpx

from app.services.llm.base import ChatMessage, LLMConfigError, LLMProviderError, LLMResponseError, post_json


class OpenRouterProvider:
    name = "openrouter"

    def __init__(self, api_key: str, base_url: str, timeout: float, max_retries: int,
                 referer: str = "", transport: httpx.BaseTransport | None = None):
        self._key, self._base, self._timeout, self._retries = api_key, base_url.rstrip("/"), timeout, max_retries
        self._referer, self._transport = referer, transport

    def complete(self, messages: list[ChatMessage], *, model: str, temperature: float, max_tokens: int,
                 json_mode: bool = False) -> str:
        if not self._key:
            raise LLMConfigError("OPENROUTER_API_KEY is not set")
        payload: dict = {
            "model": model, "temperature": temperature, "max_tokens": max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json", "X-Title": "Agentic Workflow Assistant"}
        if self._referer:
            headers["HTTP-Referer"] = self._referer
        data = post_json(f"{self._base}/chat/completions", headers=headers, payload=payload,
                         timeout=self._timeout, max_retries=self._retries, transport=self._transport)
        if isinstance(data.get("error"), dict):  # OpenRouter can return 200 with an error body
            raise LLMProviderError(f"provider error: {data['error'].get('message', 'unknown')}"[:300])
        try:
            content = data["choices"][0]["message"].get("content")
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise LLMResponseError("unexpected response shape from OpenRouter") from exc
        if not content or not content.strip():
            raise LLMResponseError("model returned empty content (possibly ran out of tokens while reasoning)")
        return content
