import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.core import security
from app.core.config import get_settings


def test_access_token_roundtrip():
    uid = uuid.uuid4()
    assert security.decode_access_token(security.create_access_token(uid)) == uid


def test_rejects_tampered_and_garbage_tokens():
    tok = security.create_access_token(uuid.uuid4())
    with pytest.raises(security.TokenError):
        security.decode_access_token(tok[:-2] + "xx")
    with pytest.raises(security.TokenError):
        security.decode_access_token("not-a-jwt")


def test_rejects_expired_token():
    s = get_settings()
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    tok = jwt.encode({"sub": str(uuid.uuid4()), "type": "access", "exp": past}, s.jwt_secret, algorithm="HS256")
    with pytest.raises(security.TokenError):
        security.decode_access_token(tok)


def test_state_token_cannot_be_used_as_access_token():
    state = security.create_state_token("nonce")
    with pytest.raises(security.TokenError):
        security.decode_access_token(state)
    assert security.decode_state_token(state) == "nonce"


def test_access_token_cannot_be_used_as_state():
    with pytest.raises(security.TokenError):
        security.decode_state_token(security.create_access_token(uuid.uuid4()))


def test_alg_none_rejected():
    forged = jwt.encode({"sub": str(uuid.uuid4()), "type": "access"}, None, algorithm="none")
    with pytest.raises(security.TokenError):
        security.decode_access_token(forged)


def test_encryption_roundtrip_and_not_plaintext():
    ct = security.encrypt_secret("ya29.secret")
    assert "ya29" not in ct
    assert security.decrypt_secret(ct) == "ya29.secret"
    with pytest.raises(security.TokenError):
        security.decrypt_secret("garbage")
