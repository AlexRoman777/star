"""Indexed task-state reads and bounded runtime recovery."""

import sqlalchemy as sa
from alembic import context, op

revision = "20260915_0011"
down_revision = "20260909_0010"
branch_labels = None
depends_on = None

INDEXES = (
    (
        "ix_tasks_state_type_generation",
        "tasks",
        ["state", "task_type", "analysis_generation_id"],
        None,
    ),
    (
        "ix_tasks_failed_recovery",
        "tasks",
        ["last_error_class", "updated_at", "id"],
        "state = 'FAILED'",
    ),
    (
        "ix_analysis_nodes_state_task_generation",
        "analysis_nodes",
        ["state", "task_id", "generation_id"],
        None,
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
                # A cancelled concurrent build can leave an unusable index;
                # IF NOT EXISTS alone would silently keep it on the next boot.
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
