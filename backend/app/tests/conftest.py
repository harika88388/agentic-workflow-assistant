import os

from cryptography.fernet import Fernet

# Must be set before any `app` import reads settings.
os.environ.setdefault("JWT_SECRET", "x" * 48)
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite://")
# Tests must never reach a real LLM, whatever the developer has exported.
os.environ["OPENROUTER_API_KEY"] = ""
os.environ["GEMINI_API_KEY"] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.config import CALENDAR_SCOPE, GMAIL_SCOPE  # noqa: E402
from app.core.security import create_access_token  # noqa: E402
from app.database import models  # noqa: E402,F401
from app.database.base import Base  # noqa: E402
from app.database.models import User  # noqa: E402
from app.database.session import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services import google_oauth  # noqa: E402

ALL_SCOPES = f"openid email {GMAIL_SCOPE} {CALENDAR_SCOPE}"


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def client(db_session):
    def _override():
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app, follow_redirects=False) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def make_user(db_session):
    def _make(email="a@example.com", sub="sub-a") -> tuple[User, dict]:
        u = User(google_sub=sub, email=email, name=email.split("@")[0])
        db_session.add(u)
        db_session.commit()
        return u, {"Authorization": f"Bearer {create_access_token(u.id)}"}

    return _make


@pytest.fixture()
def mock_google(monkeypatch):
    """Replace network calls to Google with deterministic fakes."""
    state = {"refresh_token": "refresh-1", "scope": ALL_SCOPES}
    monkeypatch.setattr(
        google_oauth,
        "exchange_code",
        lambda code: google_oauth.GoogleTokens("access-1", state["refresh_token"], 3600, state["scope"], "idtok"),
    )
    monkeypatch.setattr(
        google_oauth,
        "verify_id_token",
        lambda t: google_oauth.GoogleIdentity("google-sub-1", "user@example.com", "User", None),
    )
    monkeypatch.setattr(google_oauth, "revoke_token", lambda t: state.setdefault("revoked", t))
    return state
