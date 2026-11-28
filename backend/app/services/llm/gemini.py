import httpx

from app.services.llm.base import ChatMessage, LLMConfigError, LLMResponseError, post_json, record_usage


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str, base_url: str, timeout: float, max_retries: int,
                 transport: httpx.BaseTransport | None = None):
        self._key, self._base, self._timeout, self._retries = api_key, base_url.rstrip("/"), timeout, max_retries
        self._transport = transport

    def complete(self, messages: list[ChatMessage], *, model: str, temperature: float, max_tokens: int,
                 json_mode: bool = False) -> str:
        if not self._key:
            raise LLMConfigError("GEMINI_API_KEY is not set")
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        contents = [{"role": "model" if m.role == "assistant" else "user", "parts": [{"text": m.content}]}
                    for m in messages if m.role != "system"]
        gen: dict = {"temperature": temperature, "maxOutputTokens": max_tokens}
        if json_mode:
            gen["responseMimeType"] = "application/json"
        payload: dict = {"contents": contents, "generationConfig": gen}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        data = post_json(f"{self._base}/models/{model}:generateContent",
                         headers={"x-goog-api-key": self._key, "Content-Type": "application/json"},
                         payload=payload, timeout=self._timeout, max_retries=self._retries, transport=self._transport)
        if (data.get("promptFeedback") or {}).get("blockReason"):
            raise LLMResponseError("prompt was blocked by the model's safety filter")
        try:
            parts = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMResponseError("Gemini returned no candidates") from exc
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text.strip():
            raise LLMResponseError("model returned empty content")
        usage = data.get("usageMetadata") if isinstance(data.get("usageMetadata"), dict) else {}
        record_usage(usage.get("promptTokenCount"), usage.get("candidatesTokenCount"), usage.get("totalTokenCount"))
        return text
