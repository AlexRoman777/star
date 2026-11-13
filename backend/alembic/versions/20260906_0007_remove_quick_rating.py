"""Retire the quick_rating stage: strip it from stored paused_stages and cancel leftovers.

Revision ID: 20260906_0007
Revises: 20260906_0006

Data-only. v209 removed the four-photo Qwen quick group (group.cohort_triage),
the CLIP search index (asset.clip_embedding) and description embeddings
(asset.description_embedding). Their evidence tables stay readable; queued
rows of the retired task types are cancelled here (the scheduler also
reconciles any that appear later) and primaries a quick group still held in
PLANNED are released to QUEUED so no batch waits behind a lane that no longer
exists.
"""

import sqlalchemy as sa
from alembic import op

revision = "20260906_0007"
down_revision = "20260906_0006"
branch_labels = None
depends_on = None

RETIRED_TASK_TYPES = (
    "asset.clip_embedding",
    "asset.description_embedding",
    "group.cohort_triage",
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite test fixtures create the schema from the models; the JSON
        # rewrite below is PostgreSQL-only and the data step is a no-op there.
        return
    op.execute(
        sa.text(
            "UPDATE library_programs SET model_pipeline_policy = "
            "jsonb_set(COALESCE(model_pipeline_policy, '{}'::jsonb), '{paused_stages}', "
            "COALESCE((SELECT jsonb_agg(value) FROM jsonb_array_elements("
            "COALESCE(model_pipeline_policy->'paused_stages', '[]'::jsonb)) AS elements(value) "
            "WHERE value <> '\"quick_rating\"'::jsonb), '[]'::jsonb), true) "
            "WHERE jsonb_exists(COALESCE(model_pipeline_policy->'paused_stages', '[]'::jsonb), "
            "'quick_rating')"
        )
    )
    op.execute(
        sa.text(
            "UPDATE library_programs SET model_pipeline_policy = "
            "(COALESCE(model_pipeline_policy, '{}'::jsonb) - 'quick_group_model' "
            "- 'quick_group_mode') || jsonb_build_object('highlight_model', "
            "COALESCE(model_pipeline_policy->'quick_group_model', "
            "'\"qwen3-vl:8b-instruct-q4_K_M\"'::jsonb)) "
            "WHERE model_pipeline_policy ? 'quick_group_model' "
            "OR NOT (model_pipeline_policy ? 'highlight_model')"
        )
    )
    op.execute(
        sa.text(
            "UPDATE tasks SET state = 'QUEUED', available_at = NOW(), "
            'target_ref = target_ref || \'{"cohort_triage_state": "retired"}\'::jsonb '
            "WHERE task_type = 'asset.primary_review' AND state = 'PLANNED' "
            "AND target_ref ? 'cohort_triage_state'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE tasks SET state = 'CANCELLED', last_error_class = 'TaskTypeRetired', "
            "lease_id = NULL, leased_to_worker_id = NULL, lease_expires_at = NULL, "
            "progress = COALESCE(progress, '{}'::jsonb) || "
            '\'{"stage": "cancelled", "reason": "task_type_retired"}\'::jsonb '
            "WHERE task_type IN ('asset.clip_embedding', 'asset.description_embedding', "
            "'group.cohort_triage') AND state IN "
            "('PLANNED', 'QUEUED', 'RETRY_WAITING', 'RUNNING', 'CANCEL_REQUESTED')"
        )
    )


def downgrade() -> None:
    # Data-only; earlier releases tolerate an empty paused_stages list and
    # cancelled task history.
    pass
