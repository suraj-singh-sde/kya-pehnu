"""Google picture on the account; almirah finish and shelf layout on the profile.

Revision ID: c1d2e3f4a5b6
Revises: b8e3f5a40d71
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5b6"
down_revision: str | None = "b8e3f5a40d71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user", sa.Column("avatar_url", sa.String(2048), nullable=True))
    op.add_column("userprofile", sa.Column("theme", sa.String(16), nullable=False, server_default="teal"))
    op.add_column("userprofile", sa.Column("layout", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("userprofile") as batch:
        batch.drop_column("layout")
        batch.drop_column("theme")
    with op.batch_alter_table("user") as batch:
        batch.drop_column("avatar_url")
