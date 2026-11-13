"""Count how often a Highlight entry left the rounds for a source failure.

Immich reports a photograph as available while refusing to serve a derivative
for it, so the six-hour recovery returned the same photographs to the rounds
for ever: twenty-two of them spoiled about five hundred rounds a day. The
count bounds those returns.
"""

import sqlalchemy as sa
from alembic import op

revision = "20260920_0017"
down_revision = "20260919_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "highlight_league_entries",
        sa.Column("source_failure_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("highlight_league_entries", "source_failure_count")
