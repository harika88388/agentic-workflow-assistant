from app.api.deps import get_calendar_service, get_gmail_service
from app.integrations.calendar.models import CalendarEvent
from app.integrations.errors import GoogleAuthError, GoogleRateLimitError
from app.integrations.gmail.mime import EmailMessageData, EmailThreadData
from app.main import app
from app.services.llm import get_llm_router
from app.services.llm.base import LLMConfigError
from app.core.config import get_settings
from app.services.llm.router import LLMRouter


class FakeGmail:
    def search_emails(self, q, n):
        return [EmailMessageData(id="m1", thread_id="t1", subject=f"about {q}")]

    def read_email(self, mid):
        return EmailMessageData(id=mid, thread_id="t1", body_text="hello")

    def get_thread(self, tid):
        return EmailThreadData(tid, [EmailMessageData(id="m1", thread_id=tid)])


class FakeCal:
    tz = __import__("zoneinfo").ZoneInfo("UTC")

    def list_events(self, days=7, query=None):
        return [CalendarEvent(id="e1", summary="Sync", start="2026-11-25T10:00:00Z", end="2026-11-25T11:00:00Z")]

    def next_event(self):
        return None


def test_routes_require_auth(client):
    for path in ["/api/gmail/messages?q=x", "/api/calendar/events", "/api/intent/analyze"]:
        assert client.get(path).status_code in (401, 405)


def test_not_connected_returns_reconnect_error(client, make_user):
    _, h = make_user()
    r = client.get("/api/gmail/messages?q=x", headers=h)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "google_reconnect_required"
    assert client.get("/api/calendar/events", headers=h).status_code == 403


def test_gmail_routes_serialize_and_flag_untrusted(client):
    app.dependency_overrides[get_gmail_service] = lambda: FakeGmail()
    r = client.get("/api/gmail/messages", params={"q": "invoice"})
    assert r.status_code == 200 and r.json()[0]["subject"] == "about invoice" and r.json()[0]["untrusted"] is True
    assert client.get("/api/gmail/messages/m1").json()["body_text"] == "hello"
    assert client.get("/api/gmail/threads/t9").json()["id"] == "t9"
    assert client.get("/api/gmail/messages").status_code == 422  # q required


def test_calendar_routes(client):
    app.dependency_overrides[get_calendar_service] = lambda: FakeCal()
    assert client.get("/api/calendar/events").json()[0]["summary"] == "Sync"
    assert client.get("/api/calendar/next").json() == {"event": None}
    assert client.get("/api/calendar/events?days=0").status_code == 422


def test_google_errors_become_clean_http_errors(client):
    class Boom:
        def __init__(self, exc): self.exc = exc
        def search_emails(self, q, n): raise self.exc
    app.dependency_overrides[get_gmail_service] = lambda: Boom(GoogleAuthError("token revoked", 401))
    r = client.get("/api/gmail/messages", params={"q": "x"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "google_reconnect_required"
    app.dependency_overrides[get_gmail_service] = lambda: Boom(GoogleRateLimitError("slow down", 429))
    assert client.get("/api/gmail/messages", params={"q": "x"}).status_code == 429


def test_timezone_update(client, make_user):
    _, h = make_user()
    assert client.patch("/api/auth/me/timezone", json={"timezone": "Asia/Kolkata"}, headers=h).json()["timezone"] == "Asia/Kolkata"
    assert client.patch("/api/auth/me/timezone", json={"timezone": "Mars/Base"}, headers=h).status_code == 422


def test_llm_config_and_intent_endpoint(client, make_user):
    _, h = make_user()
    class Prov:
        name = "openrouter"
        def complete(self, messages, **kw):
            return '{"service":"gmail","intent":"search_emails","entities":{"query":"invoice"}}'
    app.dependency_overrides[get_llm_router] = lambda: LLMRouter(get_settings(), {"openrouter": Prov()})
    cfg = client.get("/api/llm/config", headers=h).json()
    assert cfg["distinct_models"] == 1 and "api_key" not in str(cfg).lower()
    r = client.post("/api/intent/analyze", json={"message": "find invoice emails"}, headers=h)
    assert r.status_code == 200 and r.json()["intent"] == "search_emails" and r.json()["missing_parameters"] == []
    assert client.post("/api/intent/analyze", json={"message": "   "}, headers=h).status_code == 422
    assert client.post("/api/intent/analyze", json={"message": "x"}).status_code == 401


def test_unconfigured_llm_gives_503_not_500(client, make_user):
    _, h = make_user()
    class NoKey:
        name = "openrouter"
        def complete(self, messages, **kw): raise LLMConfigError("OPENROUTER_API_KEY is not set")
    app.dependency_overrides[get_llm_router] = lambda: LLMRouter(get_settings(), {"openrouter": NoKey()})
    r = client.post("/api/intent/analyze", json={"message": "hi"}, headers=h)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "llm_not_configured"
