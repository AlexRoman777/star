"""Indexes for the pruning reads, ordered along them.

Ordering the pruning selects by id made PostgreSQL walk whole tables in id
order hunting for the few matching rows (1.1 million buffers for 2,000
cancelled tasks on a copy of the production database; two minutes on the coordinator
disk). The selects now order along an index, and the two that had none get
one: pending proposals by creation time, and review runs without a
generation by start time.
"""

import sqlalchemy as sa
from alembic import context, op

revision = "20260916_0015"
down_revision = "20260916_0014"
branch_labels = None
depends_on = None

INDEXES = (
    ("ix_proposals_state_created", "proposals", ["state", "created_at"], None),
    (
        "ix_analysis_runs_orphan_started",
        "analysis_runs",
        ["started_at"],
        "analysis_generation_id IS NULL AND analysis_type IN "
        "('primary_review', 'photo_deep_review', "
        "'highlight_comparison_allocation', 'series_comparison_allocation')",
    ),
)


def _create(concurrent: bool) -> None:
    for name, table, columns, predicate in INDEXES:
        if concurrent and not context.is_offline_mode():
            invalid = op.get_bind().scalar(
                sa.text(
                    "SELECT NOT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"
                ),
                {"name": name},
            )
            if invalid:
                op.drop_index(name, table_name=table, postgresql_concurrently=True)
        options = {}
        if predicate:
            options = {"postgresql_where": sa.text(predicate), "sqlite_where": sa.text(predicate)}
        op.create_index(
            name, table, columns, if_not_exists=True, postgresql_concurrently=concurrent, **options
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
            for name, table, _columns, _predicate in reversed(INDEXES):
                op.drop_index(name, table_name=table, postgresql_concurrently=True)
    else:
        for name, table, _columns, _predicate in reversed(INDEXES):
            op.drop_index(name, table_name=table)
