"""JWT session tokens, OAuth state tokens and at-rest token encryption."""
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings


class TokenError(Exception):
    """Raised for any invalid / expired / tampered token."""


def _encode(payload: dict, minutes: int) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    payload = {**payload, "iat": now, "exp": now + timedelta(minutes=minutes), "jti": uuid.uuid4().hex}
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)


def _decode(token: str, expected_type: str) -> dict:
    s = get_settings()
    try:
        data = jwt.decode(
            token, s.jwt_secret, algorithms=[s.jwt_algorithm], options={"require": ["exp", "sub", "type"]}
        )
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    if data.get("type") != expected_type:
        raise TokenError("wrong token type")
    return data


def create_access_token(user_id: uuid.UUID) -> str:
    return _encode({"sub": str(user_id), "type": "access"}, get_settings().jwt_expire_minutes)


def decode_access_token(token: str) -> uuid.UUID:
    data = _decode(token, "access")
    try:
        return uuid.UUID(data["sub"])
    except ValueError as exc:
        raise TokenError("bad subject") from exc


def generate_nonce() -> str:
    return secrets.token_urlsafe(32)


def create_state_token(nonce: str) -> str:
    """Signed, short-lived OAuth `state` value that embeds the CSRF nonce."""
    return _encode({"sub": nonce, "type": "oauth_state"}, 10)


def decode_state_token(token: str) -> str:
    return _decode(token, "oauth_state")["sub"]


def _fernet() -> Fernet:
    return Fernet(get_settings().token_encryption_key.encode())


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise TokenError("cannot decrypt stored secret") from exc
