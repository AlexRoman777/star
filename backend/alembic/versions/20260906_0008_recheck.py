"""Recheck tool: library benchmark sets, streamed membership, and apply bookkeeping.

Revision ID: 20260906_0008
Revises: 20260906_0007

Benchmark sets gain a ``source`` (owner_rated | library) and a
``membership_mode`` (frozen | streamed): a library set above the frozen-list
size stores only its filter and each run draws the membership in a
deterministic order (``benchmark_runs.stream_cursor`` / ``materialized_count``).
Results remember the star the photo had when the answer was requested
(``reference_rating``) so the before/after shift needs no owner label. Applying
a run records ``recheck`` rating proposals; the policy and progress live on the
run (``applied_at``, ``apply_policy``, ``apply_summary``) and the chosen star per
photo on its result (``apply_target``, ``applied_at``, ``applied_proposal_id``).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260906_0008"
down_revision = "20260906_0007"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column(
        "benchmark_sets",
        sa.Column("source", sa.String(16), nullable=False, server_default="owner_rated"),
    )
    op.add_column(
        "benchmark_sets",
        sa.Column("membership_mode", sa.String(16), nullable=False, server_default="frozen"),
    )

    op.add_column("benchmark_runs", sa.Column("stream_cursor", JSON, nullable=True))
    op.add_column(
        "benchmark_runs",
        sa.Column("materialized_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "benchmark_runs", sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("benchmark_runs", sa.Column("apply_policy", JSON, nullable=True))
    op.add_column("benchmark_runs", sa.Column("apply_summary", JSON, nullable=True))

    op.add_column("benchmark_results", sa.Column("reference_rating", sa.Integer(), nullable=True))
    op.add_column("benchmark_results", sa.Column("apply_target", sa.Integer(), nullable=True))
    op.add_column(
        "benchmark_results", sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("benchmark_results", sa.Column("applied_proposal_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_benchmark_results_run_apply",
        "benchmark_results",
        ["run_id", "apply_target", "applied_at"],
    )
    # Existing owner-rated results: the reference star is the owner label.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                """
                UPDATE benchmark_results AS r
                SET reference_rating = (m.member->>'owner_rating')::int
                FROM benchmark_runs AS run
                JOIN benchmark_sets AS s ON s.id = run.set_id
                CROSS JOIN LATERAL jsonb_array_elements(s.members) AS m(member)
                WHERE run.id = r.run_id
                  AND r.asset_id IS NOT NULL
                  AND m.member->>'asset_id' = CAST(r.asset_id AS text)
                  AND r.reference_rating IS NULL
                """
            )
        )


def downgrade() -> None:
    op.drop_index("ix_benchmark_results_run_apply", table_name="benchmark_results")
    op.drop_column("benchmark_results", "applied_proposal_id")
    op.drop_column("benchmark_results", "applied_at")
    op.drop_column("benchmark_results", "apply_target")
    op.drop_column("benchmark_results", "reference_rating")
    op.drop_column("benchmark_runs", "apply_summary")
    op.drop_column("benchmark_runs", "apply_policy")
    op.drop_column("benchmark_runs", "applied_at")
    op.drop_column("benchmark_runs", "materialized_count")
    op.drop_column("benchmark_runs", "stream_cursor")
    op.drop_column("benchmark_sets", "membership_mode")
    op.drop_column("benchmark_sets", "source")
