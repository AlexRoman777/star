from __future__ import annotations

import asyncio
from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context
from alembic.script import ScriptDirectory
from sqlalchemy import pool
from sqlalchemy.dialects.postgresql.base import ischema_names
from sqlalchemy.ext.asyncio import async_engine_from_config

from star.core.config import get_settings
from star.db import models
from star.db.types import HalfVector384

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
config.set_main_option("sqlalchemy.url", get_settings().alembic_database_url)
target_metadata = models.Base.metadata
ischema_names["halfvec"] = HalfVector384

MANUALLY_MANAGED_INDEXES = {
    "ix_asset_revisions_gallery_camera",
    "ix_asset_revisions_gallery_captured",
    "ix_assets_gallery_cache_priority",
    "ix_search_embedding_hnsw",
    "ix_visual_embedding_hnsw",
}


def include_object(
    _object: object,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: object | None,
) -> bool:
    """Exclude PostgreSQL expression indexes maintained by explicit migrations."""

    return not (
        type_ == "index" and reflected and compare_to is None and name in MANUALLY_MANAGED_INDEXES
    )


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


BASELINE_REVISION = "20260904_0001"
# v180 shipped at 0105. Revisions 0106-0108 existed only on the development
# branch while v181 was being prepared, so accepting them as upgrade inputs is
# useful for local candidates too. Migration 0002 is deliberately idempotent
# and completes whichever part of that short bridge is absent.
LEGACY_COMPATIBLE_REVISIONS = {
    "20260903_0105",
    "20260903_0106",
    "20260903_0107",
    "20260903_0108",
}


def restamp_legacy_database(connection) -> None:
    """Move a database from the retired incremental chain onto the baseline.

    v181 squashed the migration history. A live v180 database carries 0105,
    which no longer exists in this script directory; it is re-stamped to the
    baseline so the idempotent bridge/data migration can apply 0106 onward.
    Any other unknown revision means an older Star: upgrade to v180 first.
    """

    inspector = sa.inspect(connection)
    try:
        if "alembic_version" not in inspector.get_table_names():
            return
        current = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
        if current is None:
            return
        known = {script.revision for script in ScriptDirectory.from_config(config).walk_revisions()}
        if current in known:
            return
        if current in LEGACY_COMPATIBLE_REVISIONS:
            connection.execute(
                sa.text("UPDATE alembic_version SET version_num = :baseline"),
                {"baseline": BASELINE_REVISION},
            )
            return
        raise RuntimeError(
            f"database revision {current} predates the v180 baseline; "
            "deploy Star v180 and let it migrate before installing this release"
        )
    finally:
        # End the inspection transaction so alembic opens and commits its own.
        connection.commit()


def do_run_migrations(connection) -> None:
    restamp_legacy_database(connection)
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
