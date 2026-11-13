"""Retire Apple Vision and the technical preview: cancel leftovers, map the Apple worker order.

Revision ID: 20260906_0009
Revises: 20260906_0008

Data-only. v209 removed ``asset.apple_vision`` (and its Apple worker) and
``asset.technical_preview``: neither fed the star. Their evidence tables stay
readable; queued rows of the retired task types are cancelled here (the
scheduler also reconciles any that appear later) and a stored ``vision`` apple
order becomes ``primary`` ("Ministral first"), the only orders that remain
being balanced, primary and senior.
"""

import sqlalchemy as sa
from alembic import op

revision = "20260906_0009"
down_revision = "20260906_0008"
branch_labels = None
depends_on = None

RETIRED_TASK_TYPES = ("asset.apple_vision", "asset.technical_preview")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(
        sa.text(
            "UPDATE library_programs SET model_pipeline_policy = "
            "jsonb_set(COALESCE(model_pipeline_policy, '{}'::jsonb), '{apple_priority}', "
            "'\"primary\"'::jsonb, true) "
            "WHERE model_pipeline_policy->>'apple_priority' = 'vision'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE tasks SET state = 'CANCELLED', last_error_class = 'TaskTypeRetired', "
            "lease_id = NULL, leased_to_worker_id = NULL, lease_expires_at = NULL, "
            "progress = COALESCE(progress, '{}'::jsonb) || "
            '\'{"stage": "cancelled", "reason": "task_type_retired"}\'::jsonb '
            "WHERE task_type IN ('asset.apple_vision', 'asset.technical_preview') AND state IN "
            "('PLANNED', 'QUEUED', 'RETRY_WAITING', 'RUNNING', 'CANCEL_REQUESTED')"
        )
    )


def downgrade() -> None:
    # Data-only; earlier releases tolerate cancelled task history.
    pass
