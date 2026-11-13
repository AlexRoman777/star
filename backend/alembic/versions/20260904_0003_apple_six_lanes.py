"""Allow the benchmarked six-lane Mac Ministral limit.

Revision ID: 20260904_0003
Revises: 20260904_0002
"""

from alembic import op

revision = "20260904_0003"
down_revision = "20260904_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE workers DROP CONSTRAINT "
        "IF EXISTS ck_workers_ck_workers_model_concurrency_limit"
    )
    op.execute(
        "ALTER TABLE workers ADD CONSTRAINT ck_workers_ck_workers_model_concurrency_limit "
        "CHECK (model_concurrency_limit >= 1 AND model_concurrency_limit <= 6)"
    )
    op.alter_column("workers", "model_concurrency_limit", server_default="6")
    op.execute(
        "UPDATE workers SET model_concurrency_limit = 6 "
        "WHERE capabilities->>'apple-silicon' = 'true' "
        "AND model_concurrency_limit = 4"
    )


def downgrade() -> None:
    op.execute("UPDATE workers SET model_concurrency_limit = 4 WHERE model_concurrency_limit > 4")
    op.execute(
        "ALTER TABLE workers DROP CONSTRAINT "
        "IF EXISTS ck_workers_ck_workers_model_concurrency_limit"
    )
    op.execute(
        "ALTER TABLE workers ADD CONSTRAINT ck_workers_ck_workers_model_concurrency_limit "
        "CHECK (model_concurrency_limit >= 1 AND model_concurrency_limit <= 4)"
    )
    op.alter_column("workers", "model_concurrency_limit", server_default="4")
