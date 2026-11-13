"""Indexes for the sweeps that read the whole tasks table every cycle.

Statement statistics on a copy of the production database named three full
scans of ``tasks`` (1.4 million rows) that ran every scheduler cycle and
returned nothing: the task-state invariant sweep, the cancelled-task prune
select, and the throughput group-by. Two partial indexes make the lease
contradictions the sweep looks for an index lookup over an empty set, and one
composite index serves every "this state, this time window" read.
"""

import sqlalchemy as sa
from alembic import context, op

revision = "20260916_0014"
down_revision = "20260916_0013"
branch_labels = None
depends_on = None

LEASE_RESIDUE = (
    "state NOT IN ('RUNNING', 'CANCEL_REQUESTED') AND "
    "(lease_id IS NOT NULL OR leased_to_worker_id IS NOT NULL OR lease_expires_at IS NOT NULL)"
)
LEASE_MISSING = (
    "state IN ('RUNNING', 'CANCEL_REQUESTED') AND "
    "(lease_id IS NULL OR leased_to_worker_id IS NULL OR lease_expires_at IS NULL)"
)

INDEXES = (
    ("ix_tasks_state_updated_at", "tasks", ["state", "updated_at"], None),
    ("ix_tasks_lease_residue", "tasks", ["updated_at", "id"], LEASE_RESIDUE),
    ("ix_tasks_lease_missing", "tasks", ["updated_at", "id"], LEASE_MISSING),
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


# The ranking repair looks for the few pending highlight proposals by the
# source inside their JSON value; without this it read every pending proposal.
PROPOSAL_SOURCE_INDEX = "ix_proposals_pending_source"


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            _create(True)
            op.execute(
                sa.text(
                    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {PROPOSAL_SOURCE_INDEX} "
                    "ON proposals ((value ->> 'source')) WHERE state = 'PENDING'"
                )
            )
    else:
        _create(False)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {PROPOSAL_SOURCE_INDEX}"))
            for name, table, _columns, _predicate in reversed(INDEXES):
                op.drop_index(name, table_name=table, postgresql_concurrently=True)
    else:
        for name, table, _columns, _predicate in reversed(INDEXES):
            op.drop_index(name, table_name=table)
