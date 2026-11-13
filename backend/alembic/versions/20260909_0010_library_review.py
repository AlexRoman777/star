"""Nightly library review: the standing notebook a stronger model writes into.

Revision ID: 20260909_0010
Revises: 20260906_0009

The library reviews itself once a day on the machine that can hold the 27B
model. Each pass reads a compact evidence pack of counts, never photographs,
and writes findings and bounded proposals here. Nothing it writes is applied on
its own: a proposal is a suggestion the owner accepts or dismisses, because a
model that edits its own rating prompt unmeasured will drift and no one will be
able to say why the stars changed.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260909_0010"
down_revision = "20260906_0009"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "library_review_notes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id", ondelete="SET NULL")),
        sa.Column("state", sa.String(32), nullable=False, server_default="requested"),
        sa.Column("model_key", sa.String(256), nullable=False, server_default=""),
        # The counts the pass was given, kept so a conclusion can be checked
        # against what it actually saw rather than what the library looks like
        # now.
        sa.Column("evidence", JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("findings", JSON, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("proposals", JSON, nullable=False, server_default=sa.text("'[]'")),
        # What the owner did with each proposal, keyed by its position in
        # the list above. Absent means undecided; nothing is ever applied
        # without a row here.
        sa.Column("decisions", JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("failure_class", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_library_review_notes_state_created",
        "library_review_notes",
        ["state", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_library_review_notes_state_created", table_name="library_review_notes")
    op.drop_table("library_review_notes")
