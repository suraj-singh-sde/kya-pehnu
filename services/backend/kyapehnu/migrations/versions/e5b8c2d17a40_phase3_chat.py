"""phase 3: chat sessions, messages, tool calls and feedback

Additive only: no earlier table is altered and itemseq is left alone.

Revision ID: e5b8c2d17a40
Revises: d4f1a7c90b22
Create Date: 2026-10-06 18:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5b8c2d17a40"
down_revision: str | None = "d4f1a7c90b22"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chatsession",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.Column("updated_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(op.f("ix_chatsession_updated_at"), "chatsession", ["updated_at"], unique=False)
    op.create_table(
        "chatmessage",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("turn", sa.Integer(), nullable=False),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("role", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("content", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["chatsession.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chatmessage_session_id_id", "chatmessage", ["session_id", "id"], unique=False)
    op.create_table(
        "toolcall",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("turn", sa.Integer(), nullable=False),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("call_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("args", sa.JSON(), nullable=True),
        sa.Column("result", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("error", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=True),
        sa.Column("ui", sa.JSON(), nullable=True),
        sa.Column("proposal_state", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("proposed_on", sa.Date(), nullable=True),
        sa.Column("decided_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True),
        sa.Column("outcome", sa.JSON(), nullable=True),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.CheckConstraint(
            "proposal_state IN ('pending', 'confirmed', 'cancelled', 'expired')", name="ck_toolcall_proposal_state"
        ),
        sa.ForeignKeyConstraint(["event_id"], ["event.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["chatsession.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_toolcall_session_id_id", "toolcall", ["session_id", "id"], unique=False)
    op.create_index(op.f("ix_toolcall_event_id"), "toolcall", ["event_id"], unique=False)
    op.create_table(
        "feedback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("outfit_id", sa.Integer(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("reason", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.CheckConstraint("rating IN (-1, 1)", name="ck_feedback_rating"),
        sa.ForeignKeyConstraint(["outfit_id"], ["outfit.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_feedback_outfit_id_id", "feedback", ["outfit_id", "id"], unique=False)


def downgrade() -> None:
    # Children first: foreign keys are enforced.
    op.drop_index("ix_feedback_outfit_id_id", table_name="feedback")
    op.drop_table("feedback")
    op.drop_index(op.f("ix_toolcall_event_id"), table_name="toolcall")
    op.drop_index("ix_toolcall_session_id_id", table_name="toolcall")
    op.drop_table("toolcall")
    op.drop_index("ix_chatmessage_session_id_id", table_name="chatmessage")
    op.drop_table("chatmessage")
    op.drop_index(op.f("ix_chatsession_updated_at"), table_name="chatsession")
    op.drop_table("chatsession")
