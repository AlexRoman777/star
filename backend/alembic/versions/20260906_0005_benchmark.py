"""Add the in-app rating benchmark tables.

Revision ID: 20260906_0005
Revises: 20260905_0004
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260906_0005"
down_revision = "20260905_0004"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "benchmark_sets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("filter_spec", JSON, nullable=False),
        sa.Column("members", JSON, nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("set_digest", sa.String(64), nullable=False),
        sa.Column("histogram", JSON, nullable=False),
        sa.Column("sensitive_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_timestamps(),
    )
    op.create_index("ix_benchmark_sets_set_digest", "benchmark_sets", ["set_digest"])
    op.create_index("ix_benchmark_sets_created", "benchmark_sets", ["created_at"])

    op.create_table(
        "benchmark_variants",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column(
            "model_version_id",
            sa.Uuid(),
            sa.ForeignKey("model_versions.id", ondelete="SET NULL"),
        ),
        sa.Column("model_key", sa.String(256), nullable=False),
        sa.Column("runtime_label", sa.String(128), nullable=False, server_default=""),
        sa.Column("spec", JSON, nullable=False),
        sa.Column("variant_digest", sa.String(64), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_timestamps(),
    )
    op.create_index(
        "ix_benchmark_variants_model_version_id", "benchmark_variants", ["model_version_id"]
    )
    op.create_index(
        "ix_benchmark_variants_variant_digest", "benchmark_variants", ["variant_digest"]
    )
    op.create_index(
        "ix_benchmark_variants_kind_created", "benchmark_variants", ["kind", "created_at"]
    )

    op.create_table(
        "benchmark_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "set_id",
            sa.Uuid(),
            sa.ForeignKey("benchmark_sets.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "variant_id",
            sa.Uuid(),
            sa.ForeignKey("benchmark_variants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("priority_mode", sa.String(16), nullable=False, server_default="preempt"),
        sa.Column("source", sa.String(16), nullable=False, server_default="workers"),
        sa.Column("requested_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("set_digest", sa.String(64), nullable=False),
        sa.Column("variant_digest", sa.String(64), nullable=False),
        sa.Column("execution_manifest_digest", sa.String(64)),
        sa.Column("metrics", JSON),
        sa.Column("insights", JSON),
        sa.Column("error", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        *_timestamps(),
    )
    op.create_index("ix_benchmark_runs_set_id", "benchmark_runs", ["set_id"])
    op.create_index("ix_benchmark_runs_variant_id", "benchmark_runs", ["variant_id"])
    op.create_index("ix_benchmark_runs_state_created", "benchmark_runs", ["state", "created_at"])
    op.create_index("ix_benchmark_runs_set_variant", "benchmark_runs", ["set_id", "variant_id"])

    op.create_table(
        "benchmark_results",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("benchmark_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("item_key", sa.String(64), nullable=False),
        sa.Column("asset_id", sa.Uuid(), sa.ForeignKey("assets.id", ondelete="CASCADE")),
        sa.Column("member_asset_ids", JSON, nullable=False),
        sa.Column("repeat_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("task_id", sa.Uuid()),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("model_rating", sa.Integer()),
        sa.Column("confidence", sa.Float()),
        sa.Column("sensitivity", sa.String(64)),
        sa.Column("flags", JSON, nullable=False),
        sa.Column("ranking", JSON, nullable=False),
        sa.Column("raw_output", JSON, nullable=False),
        sa.Column("prompt_tokens", sa.Integer()),
        sa.Column("completion_tokens", sa.Integer()),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("worker_id", sa.Uuid()),
        sa.Column("runtime", sa.String(64)),
        sa.Column("error_class", sa.String(128)),
        sa.Column("error_detail", sa.Text()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.UniqueConstraint("run_id", "item_key", "repeat_index"),
    )
    op.create_index("ix_benchmark_results_run_id", "benchmark_results", ["run_id"])
    op.create_index("ix_benchmark_results_asset_id", "benchmark_results", ["asset_id"])
    op.create_index("ix_benchmark_results_task_id", "benchmark_results", ["task_id"])
    op.create_index("ix_benchmark_results_run_status", "benchmark_results", ["run_id", "status"])


def downgrade() -> None:
    op.drop_table("benchmark_results")
    op.drop_table("benchmark_runs")
    op.drop_table("benchmark_variants")
    op.drop_table("benchmark_sets")
