"""Baseline: the complete Star schema as shipped in v180.

This single revision replaces the 108 incremental migrations shipped between
July and September 2026. It applies the exact DDL that chain produced
(`baseline_v180_schema.sql`, captured with ``pg_dump --schema-only``), so a
fresh installation and an upgraded v180 database end up identical. Databases
that were already at v180 are re-stamped onto this revision by ``env.py``
before any later data migration runs.

Revision ID: 20260904_0001
Revises:
"""

from pathlib import Path

from alembic import op

revision = "20260904_0001"
down_revision = None
branch_labels = None
depends_on = None

LEGACY_HEAD = "20260903_0105"
SCHEMA_FILE = Path(__file__).with_name("baseline_v180_schema.sql")


def split_statements(script: str) -> list[str]:
    """Split a pg_dump script into statements, respecting quotes and $$ bodies."""

    statements: list[str] = []
    buffer: list[str] = []
    index = 0
    length = len(script)
    dollar_tag: str | None = None
    in_single = False
    in_line_comment = False
    while index < length:
        char = script[index]
        if in_line_comment:
            if char == "\n":
                in_line_comment = False
            index += 1
            continue
        if dollar_tag is not None:
            if script.startswith(dollar_tag, index):
                buffer.append(dollar_tag)
                index += len(dollar_tag)
                dollar_tag = None
                continue
            buffer.append(char)
            index += 1
            continue
        if in_single:
            buffer.append(char)
            if char == "'":
                in_single = False
            index += 1
            continue
        if char == "-" and script.startswith("--", index):
            in_line_comment = True
            index += 2
            continue
        if char == "'":
            in_single = True
            buffer.append(char)
            index += 1
            continue
        if char == "$":
            end = script.find("$", index + 1)
            if (end != -1 and script[index + 1 : end].replace("_", "a").isalnum()) or (
                end == index + 1
            ):
                dollar_tag = script[index : end + 1]
                buffer.append(dollar_tag)
                index = end + 1
                continue
        if char == ";":
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1
    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("Star runs on PostgreSQL only")
    for statement in split_statements(SCHEMA_FILE.read_text(encoding="utf-8")):
        # Session settings from pg_dump would change search_path for alembic's
        # own bookkeeping; every object in the script is schema-qualified.
        upper = statement.lstrip().upper()
        if upper.startswith("SET ") or upper.startswith("SELECT PG_CATALOG.SET_CONFIG"):
            continue
        bind.exec_driver_sql(statement)


def downgrade() -> None:
    raise RuntimeError("the v180 baseline cannot be downgraded; restore a database backup instead")
