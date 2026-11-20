"""phase 2: events, outfits, wear log and the item id sequence

Additive only: no Phase 1 table is altered, so Phase 1 rows, constraints and indexes are untouched.

Revision ID: c7e2a9d40f13
Revises: b2c4e6a81f30
Create Date: 2026-10-06 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7e2a9d40f13"
down_revision: str | None = "b2c4e6a81f30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("starts_at", sa.DateTime(), nullable=False),
        sa.Column("ends_at", sa.DateTime(), nullable=True),
        sa.Column("place", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("outdoors", sa.Boolean(), nullable=False),
        sa.Column("dress_code", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("audience_tags", sa.JSON(), nullable=False),
        sa.Column("notes", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_event_starts_at"), "event", ["starts_at"], unique=False)
    op.create_table(
        "outfit",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("item_ids", sa.JSON(), nullable=False),
        sa.Column("item_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_by", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("occasion_tags", sa.JSON(), nullable=False),
        sa.Column("collage_path", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("reason", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_outfit_item_key"), "outfit", ["item_key"], unique=True)
    op.create_table(
        "wearlog",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("outfit_id", sa.Integer(), nullable=True),
        sa.Column("item_ids", sa.JSON(), nullable=False),
        sa.Column("worn_on", sa.Date(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=True),
        sa.Column("audience_tags", sa.JSON(), nullable=False),
        sa.Column("weather_snapshot", sa.JSON(), nullable=True),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["event.id"]),
        sa.ForeignKeyConstraint(["outfit_id"], ["outfit.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_wearlog_worn_on"), "wearlog", ["worn_on"], unique=False)
    op.create_index(op.f("ix_wearlog_outfit_id"), "wearlog", ["outfit_id"], unique=False)
    op.create_index(op.f("ix_wearlog_event_id"), "wearlog", ["event_id"], unique=False)
    op.create_table(
        "itemseq",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("last_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("INSERT INTO itemseq (id, last_id) SELECT 1, coalesce(max(id), 0) FROM item")


def downgrade() -> None:
    # Foreign keys are enforced: the referencing table goes first.
    op.drop_table("itemseq")
    op.drop_index(op.f("ix_wearlog_event_id"), table_name="wearlog")
    op.drop_index(op.f("ix_wearlog_outfit_id"), table_name="wearlog")
    op.drop_index(op.f("ix_wearlog_worn_on"), table_name="wearlog")
    op.drop_table("wearlog")
    op.drop_index(op.f("ix_outfit_item_key"), table_name="outfit")
    op.drop_table("outfit")
    op.drop_index(op.f("ix_event_starts_at"), table_name="event")
    op.drop_table("event")
