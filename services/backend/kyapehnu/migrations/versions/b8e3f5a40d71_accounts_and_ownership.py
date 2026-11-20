"""Accounts and ownership: the user table and a required user_id on every owned table.

Irreversible (waiver W1), and only for an empty database: a Phase 3 database is imported with `kyapehnu import-phase3`,
never upgraded in place. The startup rule in kyapehnu.core.db refuses it before this revision can run.

Revision ID: b8e3f5a40d71
Revises: a7d2e4f39c60
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8e3f5a40d71"
down_revision: str | None = "a7d2e4f39c60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# table -> (index name, columns) of the owner index; `userprofile` has a unique constraint instead.
OWNED_TABLES: dict[str, tuple[str, list[str]] | None] = {
    "userprofile": None,
    "ingestjob": ("ix_ingestjob_user_id_id", ["user_id", "id"]),
    "photo": ("ix_photo_user_id", ["user_id"]),
    "item": ("ix_item_user_id_status", ["user_id", "status"]),
    "itemcorrection": ("ix_itemcorrection_user_id", ["user_id"]),
    "ingestpiece": ("ix_ingestpiece_user_id_state", ["user_id", "state"]),
    "llmcall": ("ix_llmcall_user_id_id", ["user_id", "id"]),
    "event": ("ix_event_user_id_starts_at", ["user_id", "starts_at"]),
    "outfit": None,
    "wearlog": ("ix_wearlog_user_id_worn_on", ["user_id", "worn_on"]),
    "chatsession": ("ix_chatsession_user_id_updated_at", ["user_id", "updated_at"]),
    "chatmessage": ("ix_chatmessage_user_id", ["user_id"]),
    "toolcall": ("ix_toolcall_user_id_id", ["user_id", "id"]),
    "feedback": ("ix_feedback_user_id", ["user_id"]),
}


class OwnershipMigrationError(RuntimeError):
    """The database already holds rows: this revision cannot give them an owner."""


def upgrade() -> None:
    bind = op.get_bind()
    for table in OWNED_TABLES:
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise OwnershipMigrationError("accounts_and_ownership needs an empty database; use kyapehnu import-phase3")

    op.create_table(
        "user",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("google_sub", sa.String(255), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_sign_in_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("google_sub", name="uq_user_google_sub"),
    )
    op.create_index("ix_user_email", "user", ["email"])
    op.create_index("ux_user_one_admin", "user", ["is_admin"], unique=True, sqlite_where=sa.text("is_admin = 1"))

    for table, owner_index in OWNED_TABLES.items():
        # AUTOINCREMENT would be lost by the table copy: a deleted chat's id must never be handed out again.
        kwargs = {"sqlite_autoincrement": True} if table == "chatsession" else {}
        with op.batch_alter_table(table, recreate="always", table_kwargs=kwargs) as batch:
            batch.add_column(sa.Column("user_id", sa.Integer(), nullable=False))
            batch.create_foreign_key(f"fk_{table}_user_id_user", "user", ["user_id"], ["id"])
            if owner_index is not None:
                batch.create_index(*owner_index)
            elif table == "userprofile":
                batch.create_unique_constraint("uq_userprofile_user_id", ["user_id"])
            else:  # outfit: one saved outfit per user and set of items, no longer one per set across everybody
                batch.drop_index("ix_outfit_item_key")
                batch.create_unique_constraint("uq_outfit_user_id_item_key", ["user_id", "item_key"])


def downgrade() -> None:
    raise NotImplementedError("irreversible: waiver W1")
