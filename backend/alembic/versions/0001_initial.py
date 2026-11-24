"""initial schema: users, oauth_credentials, conversations, messages, session_contexts, workflow_executions

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

JSONT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("google_sub", sa.String(255), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("name", sa.String(255)),
        sa.Column("picture_url", sa.Text),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("last_login_at", TS),
    )
    op.create_index("ix_users_google_sub", "users", ["google_sub"], unique=True)
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "oauth_credentials",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("user_id", sa.Uuid, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("access_token_enc", sa.Text, nullable=False),
        sa.Column("refresh_token_enc", sa.Text),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("scopes", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("updated_at", TS, nullable=False),
        sa.UniqueConstraint("user_id", "provider", name="uq_oauth_user_provider"),
        sa.CheckConstraint("status IN ('active','revoked')", name="ck_oauth_status"),
    )
    op.create_index("ix_oauth_credentials_user_id", "oauth_credentials", ["user_id"])

    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("user_id", sa.Uuid, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_index("ix_conversations_user_updated", "conversations", ["user_id", "updated_at"])

    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("conversation_id", sa.Uuid, sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("metadata", JSONT),
        sa.Column("created_at", TS, nullable=False),
        sa.CheckConstraint("role IN ('user','assistant','system')", name="ck_message_role"),
    )
    op.create_index("ix_messages_conversation_created", "messages", ["conversation_id", "created_at"])

    op.create_table(
        "session_contexts",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "conversation_id", sa.Uuid, sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False, unique=True,
        ),
        sa.Column("entities", JSONT, nullable=False),
        sa.Column("pending_intent", JSONT),
        sa.Column("reference_date", sa.Date),
        sa.Column("timezone", sa.String(64)),
        sa.Column("summary", sa.Text),
        sa.Column("updated_at", TS, nullable=False),
    )

    op.create_table(
        "workflow_executions",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("user_id", sa.Uuid, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", sa.Uuid, sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("service", sa.String(16)),
        sa.Column("intent", sa.String(48)),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request", JSONT),
        sa.Column("result", JSONT),
        sa.Column("error", sa.Text),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_execution_idempotency"),
        sa.CheckConstraint(
            "status IN ('pending','awaiting_approval','running','succeeded','failed','rejected')",
            name="ck_execution_status",
        ),
    )
    op.create_index("ix_executions_user_created", "workflow_executions", ["user_id", "created_at"])


def downgrade() -> None:
    for t in ("workflow_executions", "session_contexts", "messages", "conversations", "oauth_credentials", "users"):
        op.drop_table(t)
