"""Every foreign key to a table that grows is indexed on the referencing side.

PostgreSQL checks every child table when a parent row is deleted or its key
changes. Without an index on the child column that check is a sequential scan,
and the September pruning found five such columns one release at a time. This
test asks the question of the whole schema, once, on every change.
"""

from __future__ import annotations

from star.db.base import Base

# Tables whose rows are deleted or re-keyed in bulk, or that reach hundreds of
# thousands of rows. A child column pointing at any of these must lead an index.
GROWING_PARENTS = frozenset(
    {
        "assets",
        "asset_revisions",
        "analysis_generations",
        "analysis_nodes",
        "analysis_runs",
        "tasks",
        "proposals",
        "immich_connections",
        "workers",
    }
)


def _leading_index_columns(table) -> set[str]:
    leading = {next(iter(table.primary_key.columns)).name} if table.primary_key.columns else set()
    for index in table.indexes:
        columns = list(index.columns)
        if columns:
            leading.add(columns[0].name)
    for constraint in table.constraints:
        columns = list(getattr(constraint, "columns", []))
        if columns and getattr(constraint, "__visit_name__", "") in {
            "unique_constraint",
            "primary_key_constraint",
        }:
            leading.add(columns[0].name)
    return leading


def test_every_foreign_key_to_a_growing_table_is_indexed() -> None:
    unindexed: list[str] = []
    for table in Base.metadata.sorted_tables:
        leading = _leading_index_columns(table)
        for constraint in table.foreign_key_constraints:
            referred = constraint.referred_table.name
            if referred not in GROWING_PARENTS:
                continue
            column = next(iter(constraint.columns)).name
            if column not in leading:
                unindexed.append(f"{table.name}.{column} -> {referred}")
    assert not unindexed, "foreign keys without an index on the referencing column:\n" + "\n".join(
        sorted(unindexed)
    )
