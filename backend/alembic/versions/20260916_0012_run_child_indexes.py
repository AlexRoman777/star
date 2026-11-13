"""Indexes on the two analysis_runs children that had none.

Deleting a run makes PostgreSQL visit every table that references it.
analysis_nodes.result_analysis_run_id (2.3 million rows, SET NULL) and
visual_fingerprints.analysis_run_id (CASCADE) had no index, so removing the
review runs of five hundred pruned generations scanned both tables for each
run, took 44 seconds, timed out every audit tick, and held locks that worker
completions queued behind while it ran.
"""

import sqlalchemy as sa
from alembic import context, op

revision = "20260916_0012"
down_revision = "20260915_0011"
branch_labels = None
depends_on = None

INDEXES = (
    ("ix_analysis_nodes_result_run", "analysis_nodes", ["result_analysis_run_id"]),
    ("ix_visual_fingerprints_run", "visual_fingerprints", ["analysis_run_id"]),
)


def _create(concurrent: bool) -> None:
    for name, table, columns in INDEXES:
        if concurrent and not context.is_offline_mode():
            invalid = op.get_bind().scalar(
                sa.text(
                    "SELECT NOT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"
                ),
                {"name": name},
            )
            if invalid:
                # A cancelled concurrent build can leave an unusable index;
                # IF NOT EXISTS alone would silently keep it on the next boot.
                op.drop_index(name, table_name=table, postgresql_concurrently=True)
        op.create_index(
            name, table, columns, if_not_exists=True, postgresql_concurrently=concurrent
        )


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            _create(True)
    else:
        _create(False)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for name, table, _columns in reversed(INDEXES):
                op.drop_index(name, table_name=table, postgresql_concurrently=True)
    else:
        for name, table, _columns in reversed(INDEXES):
            op.drop_index(name, table_name=table)
