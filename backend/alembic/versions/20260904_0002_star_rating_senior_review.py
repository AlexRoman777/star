"""v181: bridge from released v180 plus the star-rating contract.

The released v180 head is 0105. This migration first applies the schema and
identifier changes developed as 0106-0107, then installs the new rating policy.
Every operation is safe when upgrading a short-lived 0106-0108 candidate too.

Revision ID: 20260904_0002
Revises: 20260904_0001
"""

import sqlalchemy as sa
from alembic import op

revision = "20260904_0002"
down_revision = "20260904_0001"
branch_labels = None
depends_on = None

RETIRED_TASK_TYPES = (
    "asset.fast_rating",
    "asset.apple_order_check",
    "asset.deep_review_experiment",
)
ACTIVE_TASK_STATES = "('PLANNED', 'QUEUED', 'RUNNING', 'RETRY_WAITING', 'CANCEL_REQUESTED')"


def _complete_v180_bridge() -> None:
    """Apply the retired 0106-0107 changes idempotently after the re-stamp."""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    op.execute("DROP INDEX IF EXISTS uq_tasks_active_primary_generation")
    op.execute(
        "UPDATE tasks SET task_type = 'asset.primary_review' "
        "WHERE task_type = 'asset.photo_dossier'"
    )
    for table, column in (
        ("analysis_runs", "analysis_type"),
        ("analysis_nodes", "stage_key"),
        ("analysis_obligations", "stage_key"),
        ("analysis_generations", "start_stage"),
    ):
        op.execute(
            f"UPDATE {table} SET {column} = 'primary_review' "  # noqa: S608
            f"WHERE {column} = 'photo_dossier'"
        )
    for table in ("review_prompt_overrides", "review_prompt_revisions"):
        op.execute(
            f"UPDATE {table} SET prompt_key = 'primary_review' "  # noqa: S608
            "WHERE prompt_key = 'photo_dossier'"
        )
    for table, column in (
        ("analysis_runs", "processor_version"),
        ("inference_runs", "prompt_version"),
        ("analysis_generations", "prompt_version"),
        ("analysis_generations", "schema_version"),
        ("processing_batches", "pipeline_version"),
    ):
        for old, new in (
            ("photo-observation-dossier-v1", "photo-observations-v1"),
            ("photo-dossier-", "primary-review-"),
            ("dossier-r", "primary-r"),
        ):
            op.execute(
                f"UPDATE {table} SET {column} = replace({column}, '{old}', '{new}') "  # noqa: S608
                f"WHERE {column} LIKE '%{old}%'"
            )
    for old, new in (
        ('"primary_dossier"', '"primary_review"'),
        ("photo-dossier-", "primary-review-"),
        ("dossier-r", "primary-r"),
    ):
        op.execute(
            sa.text(
                "UPDATE tasks SET target_ref = replace(target_ref::text, :old, :new)::jsonb "
                "WHERE state IN ('PLANNED', 'QUEUED', 'RUNNING', 'RETRY_WAITING', "
                "'CANCEL_REQUESTED') AND target_ref::text LIKE :pattern"
            ).bindparams(old=old, new=new, pattern=f"%{old}%")
        )
    op.execute(
        "UPDATE analysis_generations SET expanded_stages = "
        "replace(expanded_stages::text, '\"photo_dossier\"', "
        "'\"primary_review\"')::jsonb "
        "WHERE expanded_stages::text LIKE '%\"photo_dossier\"%'"
    )
    op.create_index(
        "uq_tasks_active_primary_generation",
        "tasks",
        ["analysis_generation_id"],
        unique=True,
        postgresql_where=sa.text(
            "analysis_generation_id IS NOT NULL "
            "AND task_type = 'asset.primary_review' "
            f"AND state IN {ACTIVE_TASK_STATES}"
        ),
    )
    if "family_roster_members" not in inspector.get_table_names():
        op.create_table(
            "family_roster_members",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column("role", sa.String(length=16), nullable=False),
            sa.Column("encrypted_canonical_name", sa.LargeBinary(), nullable=False),
            sa.Column("encrypted_aliases", sa.LargeBinary(), nullable=False),
            sa.Column("species", sa.String(length=64), nullable=True),
            sa.Column("breed", sa.String(length=64), nullable=True),
            sa.Column("pronouns", sa.String(length=32), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint("kind IN ('person', 'pet')", name="ck_family_roster_kind"),
            sa.CheckConstraint("role IN ('parent', 'child', 'pet')", name="ck_family_roster_role"),
            sa.PrimaryKeyConstraint("id"),
        )


def upgrade() -> None:
    _complete_v180_bridge()
    op.execute(
        sa.text(
            "UPDATE library_programs SET active_deep_review_prompt = 'senior-review-v1' "
            "WHERE active_deep_review_prompt <> 'senior-review-v1'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE library_programs SET model_pipeline_policy = "
            "jsonb_set(COALESCE(model_pipeline_policy, '{}'::jsonb), '{apple_advisory_model}', "
            "'\"qwen3.8:27b\"'::jsonb, true)"
        )
    )
    op.execute(
        sa.text(
            "UPDATE library_programs SET model_pipeline_policy = "
            "jsonb_set(COALESCE(model_pipeline_policy, '{}'::jsonb), '{paused_stages}', "
            "(COALESCE(model_pipeline_policy->'paused_stages', '[]'::jsonb) "
            "|| '[\"quick_rating\"]'::jsonb), true) "
            "WHERE NOT jsonb_exists(COALESCE(model_pipeline_policy->'paused_stages', "
            "'[]'::jsonb), 'quick_rating')"
        )
    )
    op.execute(sa.text("UPDATE workers SET helper_mode_enabled = true WHERE revoked_at IS NULL"))
    op.execute(
        sa.text(
            "UPDATE tasks SET state = 'cancelled', last_error_class = 'PipelineContractSuperseded' "
            "WHERE task_type IN ('asset.fast_rating', 'asset.apple_order_check', "
            "'asset.deep_review_experiment') AND state IN "
            "('PLANNED', 'QUEUED', 'RETRY_WAITING')"
        )
    )


def downgrade() -> None:
    # Data-only; earlier releases tolerate these values.
    pass
