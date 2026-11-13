"""Add owner-editable production inference settings to the library program.

Revision ID: 20260906_0006
Revises: 20260906_0005
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260906_0006"
down_revision = "20260906_0005"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    # NULL keeps the released defaults; the API only stores a validated
    # document, so no backfill is needed.
    op.add_column("library_programs", sa.Column("inference_settings", JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("library_programs", "inference_settings")
