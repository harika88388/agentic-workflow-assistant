import uuid
from datetime import datetime

from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, field_validator


class ConnectionStatus(BaseModel):
    gmail: bool
    calendar: bool


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    email: str
    name: str | None
    picture_url: str | None
    timezone: str
    created_at: datetime


class MeOut(BaseModel):
    user: UserOut
    connections: ConnectionStatus


class TimezoneUpdate(BaseModel):
    timezone: str

    @field_validator("timezone")
    @classmethod
    def _iana(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("timezone must be a valid IANA name, e.g. Asia/Kolkata") from exc
        return v
