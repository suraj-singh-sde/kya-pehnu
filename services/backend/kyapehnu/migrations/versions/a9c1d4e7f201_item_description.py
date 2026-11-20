"""A stylist's description of each item, written by the tagging model.

Revision ID: a9c1d4e7f201
Revises: c1d2e3f4a5b6
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a9c1d4e7f201"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("item", sa.Column("description", sa.String(), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("item") as batch:
        batch.drop_column("description")
