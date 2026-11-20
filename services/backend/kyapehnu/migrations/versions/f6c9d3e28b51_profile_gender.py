"""Gender on the profile, for the almirah's layout and the mannequin.

Revision ID: f6c9d3e28b51
Revises: e5b8c2d17a40
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6c9d3e28b51"
down_revision: str | None = "e5b8c2d17a40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("userprofile", sa.Column("gender", sa.String(8), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("userprofile") as batch:
        batch.drop_column("gender")
