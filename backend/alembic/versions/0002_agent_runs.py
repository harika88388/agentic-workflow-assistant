"""execution history: agent_runs

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

JSONT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("user_id", sa.Uuid, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", sa.Uuid, sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_message_id", sa.Uuid, sa.ForeignKey("messages.id", ondelete="SET NULL")),
        sa.Column("assistant_message_id", sa.Uuid, sa.ForeignKey("messages.id", ondelete="SET NULL")),
        sa.Column("workflow_id", sa.Uuid, sa.ForeignKey("workflow_executions.id", ondelete="SET NULL")),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("request_text", sa.String(500), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("intent", sa.String(48)),
        sa.Column("service", sa.String(16)),
        sa.Column("agent", sa.String(64)),
        sa.Column("approval_status", sa.String(16)),
        sa.Column("action_status", sa.String(16)),
        sa.Column("steps", JSONT),
        sa.Column("tools", JSONT),
        sa.Column("llm_calls", JSONT),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.String(500)),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("completed_at", TS),
        sa.Column("updated_at", TS, nullable=False),
        sa.CheckConstraint(
            "status IN ('running','completed','needs_clarification','awaiting_approval','rejected','failed')",
            name="ck_agent_run_status",
        ),
    )
    op.create_index("ix_agent_runs_user_created", "agent_runs", ["user_id", "created_at"])
    op.create_index("ix_agent_runs_workflow_id", "agent_runs", ["workflow_id"])


def downgrade() -> None:
    op.drop_table("agent_runs")
