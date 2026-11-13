"""Add immutable learned-preference and execution identities.

Revision ID: 20260905_0004
Revises: 20260904_0003
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260905_0004"
down_revision = "20260904_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_user AND rolsuper) THEN
            CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
        END IF;
    END $$""")
    op.create_table(
        "media_permits",
        sa.Column("slot", sa.Integer(), primary_key=True),
        sa.Column("owner", sa.Uuid()),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "performance_snapshots",
        sa.Column("key", sa.String(32), primary_key=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "inference_performance",
        sa.Column(
            "inference_run_id",
            sa.Uuid(),
            sa.ForeignKey("inference_runs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "worker_id", sa.Uuid(), sa.ForeignKey("workers.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("profile_digest", sa.String(64), nullable=False),
        sa.Column("task_type", sa.String(128), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timings_ms", postgresql.JSONB(), nullable=False),
    )
    op.create_index(
        "ix_inference_performance_worker_completed",
        "inference_performance",
        ["worker_id", "completed_at"],
    )
    for key in ("task_id", "profile_digest"):
        op.create_index(f"ix_inference_performance_{key}", "inference_performance", [key])
    op.create_table(
        "performance_buckets",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "worker_id", sa.Uuid(), sa.ForeignKey("workers.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("minute", sa.DateTime(timezone=True), nullable=False),
        sa.Column("profile_digest", sa.String(64), nullable=False),
        sa.Column("profile", postgresql.JSONB(), nullable=False),
        sa.Column("accepted", sa.Integer(), nullable=False),
        sa.Column("compute_ms", sa.BigInteger(), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False),
        sa.Column("busy_mask", sa.BigInteger(), nullable=False),
        sa.Column("busy_seconds", sa.Integer(), nullable=False),
        sa.Column("resolution_seconds", sa.Integer(), nullable=False),
    )
    op.create_index(
        "ix_performance_buckets_worker_minute", "performance_buckets", ["worker_id", "minute"]
    )
    for key in ("minute", "profile_digest"):
        op.create_index(f"ix_performance_buckets_{key}", "performance_buckets", [key])
    op.create_table(
        "work_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("program_id", sa.Uuid(), sa.ForeignKey("library_programs.id"), nullable=False),
        sa.Column("library_generation", sa.Integer(), nullable=False),
        sa.Column("requested", sa.Integer(), nullable=True),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_work_runs_program_id", "work_runs", ["program_id"])
    op.create_table(
        "work_run_assets",
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("work_runs.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column(
            "asset_id", sa.Uuid(), sa.ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("asset_revision_id", sa.Uuid(), nullable=False),
        sa.Column(
            "generation_id",
            sa.Uuid(),
            sa.ForeignKey("analysis_generations.id", ondelete="SET NULL"),
        ),
    )
    op.create_index("ix_work_run_assets_generation_id", "work_run_assets", ["generation_id"])
    op.create_index("ix_work_run_assets_asset_id", "work_run_assets", ["asset_id"])
    op.create_table(
        "machine_work_blocks",
        sa.Column("machine_group", sa.String(128), primary_key=True),
        sa.Column("block_id", sa.Uuid(), nullable=False),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("active_seconds", sa.Float(), nullable=False),
        sa.Column("completed_photos", sa.Integer(), nullable=False),
        sa.Column("target_photos", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("was_busy", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "media_staging_suppressions",
        sa.Column(
            "asset_id", sa.Uuid(), sa.ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("asset_revision_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "owner_feedback_clock",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "taste_calibration_snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False, unique=True),
        sa.Column("content_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("feedback_digest", sa.String(64), nullable=False),
        sa.Column("feedback_revision", sa.BigInteger(), nullable=False),
        sa.Column("generator_version", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "execution_manifests",
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "evaluation_reservations",
        sa.Column(
            "asset_id", sa.Uuid(), sa.ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("corpus_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_evaluation_reservations_corpus_id", "evaluation_reservations", ["corpus_id"]
    )


def downgrade() -> None:
    op.drop_table("media_permits")
    op.drop_table("performance_snapshots")
    op.drop_table("inference_performance")
    op.drop_table("performance_buckets")
    op.drop_table("work_run_assets")
    op.drop_table("work_runs")
    op.drop_table("machine_work_blocks")
    op.drop_table("media_staging_suppressions")
    for table in (
        "evaluation_reservations",
        "execution_manifests",
        "taste_calibration_snapshots",
        "owner_feedback_clock",
    ):
        op.drop_table(table)
