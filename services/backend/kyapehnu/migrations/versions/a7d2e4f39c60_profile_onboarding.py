"""Name and age on the profile, asked at onboarding.

Revision ID: a7d2e4f39c60
Revises: f6c9d3e28b51
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7d2e4f39c60"
down_revision: str | None = "f6c9d3e28b51"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("userprofile", sa.Column("name", sa.String(60), nullable=False, server_default=""))
    op.add_column("userprofile", sa.Column("age", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("userprofile") as batch:
        batch.drop_column("age")
        batch.drop_column("name")
