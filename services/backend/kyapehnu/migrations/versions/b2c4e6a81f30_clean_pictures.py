"""clean pictures: a second picture per item and review piece

Revision ID: b2c4e6a81f30
Revises: 971397b5ec4d
Create Date: 2026-10-05 21:30:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c4e6a81f30"
down_revision: str | None = "971397b5ec4d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("item", "ingestpiece")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("alt_cutout_path", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        op.add_column(table, sa.Column("alt_thumb_path", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        op.add_column(table, sa.Column("picture_generated", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    for table in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.drop_column("picture_generated")
            batch.drop_column("alt_thumb_path")
            batch.drop_column("alt_cutout_path")
