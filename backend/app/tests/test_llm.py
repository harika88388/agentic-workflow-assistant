import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.services.llm.base import (
    ChatMessage, LLMConfigError, LLMProviderError, LLMResponseError, llm_time_budget, post_json,
)
from app.services.llm.gemini import GeminiProvider
from app.services.llm.openrouter import OpenRouterProvider
from app.services.llm.router import LLMRole, LLMRouter
from app.services.llm.structured import complete_structured

BASE = dict(jwt_secret="x" * 48, token_encryption_key=Fernet.generate_key().decode(), _env_file=None)


def settings(**kw):
    return Settings(**{**BASE, **kw})


class FakeProvider:
    def __init__(self, name="fake", replies=()):
        self.name, self.replies, self.calls = name, list(replies), []

    def complete(self, messages, *, model, temperature, max_tokens, json_mode=False):
        self.calls.append(dict(messages=messages, model=model, json_mode=json_mode, temperature=temperature))
        return self.replies.pop(0)


# ---- router / config ----
def test_single_model_config_is_reported_honestly():
    d = LLMRouter(settings()).describe()
    assert d["distinct_models"] == 1
    assert {v["source"] for v in d["roles"].values()} == {"default"}
    assert all(v["model"] == "deepseek/deepseek-r1-0528" for v in d["roles"].values())


def test_independent_overrides_per_role():
    d = LLMRouter(settings(summary_llm_model="deepseek/deepseek-chat", task_llm_provider="gemini", task_llm_model="gemini-x")).describe()
    assert d["distinct_models"] == 3
    assert d["roles"]["intent"]["source"] == "default"
    assert d["roles"]["summarization"]["model"] == "deepseek/deepseek-chat"
    assert d["roles"]["task_automation"]["provider"] == "gemini"


def test_provider_override_without_model_is_rejected_at_startup():
    with pytest.raises(ValidationError):
        settings(intent_llm_provider="gemini")
    with pytest.raises(ValidationError):
        settings(llm_provider="openai")


def test_router_dispatches_by_role_and_gates_json_mode():
    a, b = FakeProvider("openrouter", ["ra"]), FakeProvider("gemini", ["rb"])
    r = LLMRouter(settings(task_llm_provider="gemini", task_llm_model="g1"), {"openrouter": a, "gemini": b})
    msgs = [ChatMessage("user", "hi")]
    assert r.complete(LLMRole.INTENT, msgs, json_mode=True) == "ra"
    assert a.calls[0]["json_mode"] is False  # openrouter json mode off by default (R1)
    assert r.complete(LLMRole.TASK, msgs, json_mode=True) == "rb"
    assert b.calls[0]["json_mode"] is True and b.calls[0]["model"] == "g1"


def test_missing_api_keys_raise_config_error_only_when_used():
    with pytest.raises(LLMConfigError):
        LLMRouter(settings()).complete(LLMRole.INTENT, [ChatMessage("user", "hi")])


# ---- providers ----
def _or(handler, key="k"):
    return OpenRouterProvider(key, "https://or.test/api/v1", 5, 1, transport=httpx.MockTransport(handler))


def test_openrouter_request_and_response():
    seen = {}
    def h(req):
        seen["auth"], seen["body"] = req.headers["authorization"], req.read().decode()
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})
    out = _or(h).complete([ChatMessage("system", "s"), ChatMessage("user", "u")], model="m", temperature=0.1, max_tokens=50)
    assert out == "hello" and seen["auth"] == "Bearer k" and '"model":"m"' in seen["body"].replace(" ", "")


def test_openrouter_failure_modes():
    msgs = [ChatMessage("user", "u")]
    kw = dict(model="m", temperature=0, max_tokens=5)
    with pytest.raises(LLMConfigError):
        _or(lambda r: httpx.Response(200, json={}), key="").complete(msgs, **kw)
    with pytest.raises(LLMProviderError):
        _or(lambda r: httpx.Response(200, json={"error": {"message": "no credits"}})).complete(msgs, **kw)
    with pytest.raises(LLMResponseError):
        _or(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": None}}]})).complete(msgs, **kw)
    with pytest.raises(LLMConfigError):
        _or(lambda r: httpx.Response(401, json={})).complete(msgs, **kw)
    with pytest.raises(LLMProviderError) as ei:
        _or(lambda r: httpx.Response(400, json={})).complete(msgs, **kw)
    assert "Bearer" not in str(ei.value)  # error text never echoes credentials


def test_openrouter_retries_503(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    n = []
    def h(req):
        n.append(1)
        return httpx.Response(503) if len(n) == 1 else httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    assert _or(h).complete([ChatMessage("user", "u")], model="m", temperature=0, max_tokens=5) == "ok" and len(n) == 2


def test_attempts_and_retries_share_the_turn_time_budget(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    timeouts = []

    def slow_503(req):
        timeouts.append(req.extensions["timeout"]["read"])
        clock[0] += 50
        return httpx.Response(503)
    with llm_time_budget(120), pytest.raises(LLMProviderError, match="time budget"):
        post_json("https://or.test", headers={}, payload={}, timeout=100, max_retries=5,
                  transport=httpx.MockTransport(slow_503), sleep=lambda s: clock.__setitem__(0, clock[0] + s))
    assert timeouts == [100, 69, 17]  # each attempt gets only what is left; no attempt starts with < 5 s


def test_an_exhausted_budget_fails_without_calling_the_provider():
    calls = []
    with llm_time_budget(1), pytest.raises(LLMProviderError, match="time budget"):
        post_json("https://or.test", headers={}, payload={}, timeout=100, max_retries=2,
                  transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, json={})))
    assert calls == []


def test_gemini_maps_roles_system_and_skips_thought_parts():
    seen = {}
    def h(req):
        import json
        seen["body"], seen["key"], seen["path"] = json.loads(req.read()), req.headers["x-goog-api-key"], req.url.path
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "hidden", "thought": True}, {"text": '{"a":1}'}]}}]})
    g = GeminiProvider("gk", "https://g.test/v1beta", 5, 0, transport=httpx.MockTransport(h))
    out = g.complete([ChatMessage("system", "sys"), ChatMessage("user", "u"), ChatMessage("assistant", "a")],
                     model="gem", temperature=0.2, max_tokens=9, json_mode=True)
    b = seen["body"]
    assert out == '{"a":1}' and seen["key"] == "gk" and seen["path"].endswith("/models/gem:generateContent")
    assert b["systemInstruction"]["parts"][0]["text"] == "sys" and [c["role"] for c in b["contents"]] == ["user", "model"]
    assert b["generationConfig"]["responseMimeType"] == "application/json"


def test_gemini_blocked_prompt():
    g = GeminiProvider("gk", "https://g.test", 5, 0, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})))
    with pytest.raises(LLMResponseError):
        g.complete([ChatMessage("user", "u")], model="m", temperature=0, max_tokens=5)


# ---- structured output ----
class Out(BaseModel):
    n: int


def _router(replies):
    p = FakeProvider("openrouter", replies)
    return LLMRouter(settings(), {"openrouter": p}), p


def test_structured_strips_r1_thinking():
    r, _ = _router(['<think>hmm {"n": 99}</think>\n```json\n{"n": 3}\n```'])
    assert complete_structured(r, LLMRole.INTENT, [ChatMessage("user", "x")], Out).n == 3


def test_structured_retries_with_error_feedback_then_succeeds():
    r, p = _router(["not json", '{"n": "abc"}', '{"n": 5}'])
    assert complete_structured(r, LLMRole.INTENT, [ChatMessage("user", "x")], Out).n == 5
    assert len(p.calls) == 3 and "invalid" in p.calls[2]["messages"][-1].content


def test_structured_gives_up_with_llm_response_error():
    r, p = _router(["nope"] * 3)
    with pytest.raises(LLMResponseError):
        complete_structured(r, LLMRole.INTENT, [ChatMessage("user", "x")], Out)
    assert len(p.calls) == 3
