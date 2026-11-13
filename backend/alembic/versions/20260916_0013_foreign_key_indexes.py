"""An index on every foreign key that a bulk delete or update walks.

PostgreSQL checks every referencing table when a parent row is deleted or its
key updated; without an index on the referencing column each check scans the
child table. The v180 baseline left sixteen such columns unindexed, among them
the self-reference on analysis_nodes (2.3 million rows) that made the audit
tick's node delete take longer than its budget. The whole schema was audited
this time, not one table, and a unit test now keeps every foreign key to a
large table indexed.
"""

import sqlalchemy as sa
from alembic import context, op

revision = "20260916_0013"
down_revision = "20260916_0012"
branch_labels = None
depends_on = None

INDEXES = (
    ("ix_analysis_nodes_superseded_by_node_id", "analysis_nodes", ["superseded_by_node_id"]),
    (
        "ix_analysis_publication_snapshots_asset_revision_id",
        "analysis_publication_snapshots",
        ["asset_revision_id"],
    ),
    ("ix_analysis_runs_asset_revision_id", "analysis_runs", ["asset_revision_id"]),
    (
        "ix_asset_search_embeddings_asset_revision_id",
        "asset_search_embeddings",
        ["asset_revision_id"],
    ),
    (
        "ix_collection_group_members_asset_revision_id",
        "collection_group_members",
        ["asset_revision_id"],
    ),
    (
        "ix_collection_groups_manual_representative_asset_id",
        "collection_groups",
        ["manual_representative_asset_id"],
    ),
    ("ix_deep_review_prompt_comparisons_asset_id", "deep_review_prompt_comparisons", ["asset_id"]),
    (
        "ix_deep_review_prompt_results_asset_revision_id",
        "deep_review_prompt_results",
        ["asset_revision_id"],
    ),
    (
        "ix_owner_rating_session_items_asset_revision_id",
        "owner_rating_session_items",
        ["asset_revision_id"],
    ),
    (
        "ix_preference_photo_insights_asset_revision_id",
        "preference_photo_insights",
        ["asset_revision_id"],
    ),
    ("ix_task_failure_evidence_asset_revision_id", "task_failure_evidence", ["asset_revision_id"]),
    ("ix_task_failure_evidence_worker_id", "task_failure_evidence", ["worker_id"]),
    ("ix_visual_embeddings_asset_revision_id", "visual_embeddings", ["asset_revision_id"]),
    ("ix_visual_fingerprints_asset_revision_id", "visual_fingerprints", ["asset_revision_id"]),
    ("ix_writeback_operations_asset_id", "writeback_operations", ["asset_id"]),
    ("ix_writeback_operations_connection_id", "writeback_operations", ["connection_id"]),
    ("ix_library_review_notes_task_id", "library_review_notes", ["task_id"]),
)


def _create(concurrent: bool) -> None:
    for name, table, columns in INDEXES:
        if concurrent and not context.is_offline_mode():
            invalid = op.get_bind().scalar(
                sa.text(
                    "SELECT NOT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"
                ),
                {"name": name},
            )
            if invalid:
                op.drop_index(name, table_name=table, postgresql_concurrently=True)
        op.create_index(
            name, table, columns, if_not_exists=True, postgresql_concurrently=concurrent
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
            for name, table, _columns in reversed(INDEXES):
                op.drop_index(name, table_name=table, postgresql_concurrently=True)
    else:
        for name, table, _columns in reversed(INDEXES):
            op.drop_index(name, table_name=table)
