"""Height and weight on the profile, for the mannequin's build.

Revision ID: d4f1a7c90b22
Revises: c7e2a9d40f13
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4f1a7c90b22"
down_revision: str | None = "c7e2a9d40f13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("userprofile", sa.Column("height_cm", sa.Integer(), nullable=True))
    op.add_column("userprofile", sa.Column("weight_kg", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("userprofile") as batch:
        batch.drop_column("weight_kg")
        batch.drop_column("height_cm")
