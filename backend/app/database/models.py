"""SQLAlchemy 2.0 models. Every user-owned table carries user_id for isolation."""
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, JSONType


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    google_sub: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    picture_url: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    credentials: Mapped[list["OAuthCredential"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    conversations: Mapped[list["Conversation"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class OAuthCredential(Base):
    """Google tokens, Fernet-encrypted at rest. Never serialised to the frontend."""

    __tablename__ = "oauth_credentials"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_oauth_user_provider"),
        CheckConstraint("status IN ('active','revoked')", name="ck_oauth_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(32), default="google")
    access_token_enc: Mapped[str] = mapped_column(Text)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[str] = mapped_column(Text, default="")  # space-delimited
    status: Mapped[str] = mapped_column(String(16), default="active")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="credentials")

    @property
    def scope_set(self) -> set[str]:
        return set(self.scopes.split())


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_user_updated", "user_id", "updated_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200), default="New conversation")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="Message.created_at"
    )
    context: Mapped["SessionContext | None"] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", uselist=False
    )
    executions: Mapped[list["WorkflowExecution"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )
    agent_runs: Mapped[list["AgentRun"]] = relationship(cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("role IN ('user','assistant','system')", name="ck_message_role"),
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column("metadata", JSONType)  # intent, clarification flags, etc.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class SessionContext(Base):
    """Per-conversation structured memory (entities, reference date, pending slots)."""

    __tablename__ = "session_contexts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), unique=True
    )
    entities: Mapped[dict] = mapped_column(JSONType, default=dict)
    pending_intent: Mapped[dict | None] = mapped_column(JSONType)
    reference_date: Mapped[date | None] = mapped_column(Date)
    timezone: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="context")


class WorkflowExecution(Base):
    __tablename__ = "workflow_executions"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_execution_idempotency"),
        CheckConstraint(
            "status IN ('pending','awaiting_approval','running','succeeded','failed','rejected')",
            name="ck_execution_status",
        ),
        Index("ix_executions_user_created", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    service: Mapped[str | None] = mapped_column(String(16))
    intent: Mapped[str | None] = mapped_column(String(48))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request: Mapped[dict | None] = mapped_column(JSONType)  # validated action payload
    result: Mapped[dict | None] = mapped_column(JSONType)  # confirmed API response
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="executions")


AGENT_RUN_STATUSES = ("running", "completed", "needs_clarification", "awaiting_approval", "rejected", "failed")


class AgentRun(Base):
    """Execution history: one row per assistant request (conversation turn), shown in the Activity view.

    Stores what the assistant did (steps, tools, models, timings, approval and outcome) but never email bodies,
    OAuth tokens or credentials. Separate from application logs, which are for diagnosing the system."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint("status IN ('" + "','".join(AGENT_RUN_STATUSES) + "')", name="ck_agent_run_status"),
        Index("ix_agent_runs_user_created", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    user_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))
    assistant_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workflow_executions.id", ondelete="SET NULL"), index=True)
    request_id: Mapped[str] = mapped_column(String(64))  # correlation id: find this run in the application logs
    request_text: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(24), default="running")
    intent: Mapped[str | None] = mapped_column(String(48))
    service: Mapped[str | None] = mapped_column(String(16))
    agent: Mapped[str | None] = mapped_column(String(64))
    approval_status: Mapped[str | None] = mapped_column(String(16))  # pending | approved | rejected
    action_status: Mapped[str | None] = mapped_column(String(16))  # succeeded | failed (approved actions)
    steps: Mapped[list | None] = mapped_column(JSONType)
    tools: Mapped[list | None] = mapped_column(JSONType)
    llm_calls: Mapped[list | None] = mapped_column(JSONType)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(500))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
