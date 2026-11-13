"""Aggregate PostgreSQL diagnostics without SQL text or parameters."""

import re

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession


async def statement_profile(session: AsyncSession) -> dict[str, object]:
    if session.get_bind().dialect.name != "postgresql":
        return {"available": False}
    try:
        async with session.begin_nested():
            row = (
                await session.execute(
                    text(
                        "SELECT count(*), coalesce(sum(calls),0), "
                        "coalesce(sum(total_exec_time),0), "
                        "coalesce(sum(shared_blks_read),0), coalesce(sum(shared_blks_hit),0), "
                        "coalesce(sum(temp_blks_written),0) FROM pg_stat_statements "
                        "WHERE dbid = (SELECT oid FROM pg_database "
                        "WHERE datname = current_database())"
                    )
                )
            ).one()
        return {
            "available": True,
            "statement_shapes": int(row[0]),
            "calls": int(row[1]),
            "execution_ms": float(row[2]),
            "shared_blocks_read": int(row[3]),
            "shared_blocks_hit": int(row[4]),
            "temporary_blocks_written": int(row[5]),
        }
    except SQLAlchemyError:
        return {"available": False, "reason": "profiling extension not ready"}


_SPACE = re.compile(r"\s+")
# "SELECT tasks.id, tasks.campaign_id, ... FROM tasks" carries thirty columns
# before the clause that matters; the shape keeps the table and the clause.
_COLUMN_LIST = re.compile(r"^SELECT (\w+)\.\w+(?:, \1\.\w+){3,} FROM \1\b")


def _query_shape(query: str) -> str:
    """A statement's shape: whitespace collapsed, column lists folded, parameters placeholders."""

    flat = _SPACE.sub(" ", query or "").strip()
    flat = _COLUMN_LIST.sub(lambda m: f"SELECT {m.group(1)}.* FROM {m.group(1)}", flat)  # noqa: S608
    return flat[:320]


async def database_activity(
    session: AsyncSession, *, older_than_seconds: float = 1.0
) -> dict[str, object]:
    """Sessions that have been busy or holding a transaction for a while, and who blocks whom.

    Answers the question a slow page raises: is the database working, or waiting?
    Star's own statements carry parameters as placeholders, so the shape of a
    statement is operational metadata, never a filename or a prompt.
    """

    if session.get_bind().dialect.name != "postgresql":
        return {"available": False, "sessions": []}
    try:
        async with session.begin_nested():
            rows = (
                await session.execute(
                    text(
                        "SELECT pid, state, wait_event_type, wait_event, application_name, "
                        "extract(epoch FROM clock_timestamp() - xact_start) "
                        "AS transaction_seconds, "
                        "extract(epoch FROM clock_timestamp() - query_start) AS statement_seconds, "
                        "pg_blocking_pids(pid) AS blocked_by, left(query, 1600) AS query "
                        "FROM pg_stat_activity "
                        "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                        "AND backend_type = 'client backend' "
                        "AND state IS NOT NULL AND state <> 'idle' "
                        "AND coalesce(xact_start, query_start) "
                        "< clock_timestamp() - make_interval(secs => :age) "
                        "ORDER BY coalesce(xact_start, query_start) LIMIT 25"
                    ),
                    {"age": older_than_seconds},
                )
            ).all()
        sessions = [
            {
                "pid": int(row.pid),
                "state": row.state,
                "wait": f"{row.wait_event_type}:{row.wait_event}" if row.wait_event_type else None,
                "application": row.application_name or "",
                "transaction_seconds": round(float(row.transaction_seconds or 0), 1),
                "statement_seconds": round(float(row.statement_seconds or 0), 1),
                "blocked_by": [int(pid) for pid in (row.blocked_by or [])],
                "statement": _query_shape(row.query),
            }
            for row in rows
        ]
        return {
            "available": True,
            "sessions": sessions,
            "blocked": sum(1 for item in sessions if item["blocked_by"]),
            "longest_transaction_seconds": max(
                (item["transaction_seconds"] for item in sessions), default=0.0
            ),
        }
    except SQLAlchemyError:
        return {"available": False, "sessions": [], "reason": "activity view not readable"}


async def database_tables(session: AsyncSession, *, limit: int = 12) -> dict[str, object]:
    """The largest relations, with their estimated row counts: where the gigabytes are."""

    if session.get_bind().dialect.name != "postgresql":
        return {"available": False, "tables": []}
    try:
        async with session.begin_nested():
            rows = (
                await session.execute(
                    text(
                        "SELECT c.relname, c.reltuples::bigint AS rows, "
                        "pg_total_relation_size(c.oid) AS total_bytes, "
                        "pg_table_size(c.oid) AS table_bytes, "
                        "pg_indexes_size(c.oid) AS index_bytes, "
                        "s.n_dead_tup AS dead_rows, "
                        "greatest(s.last_autovacuum, s.last_vacuum) AS last_vacuum_at "
                        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid "
                        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'm') "
                        "ORDER BY pg_total_relation_size(c.oid) DESC LIMIT :limit"
                    ),
                    {"limit": limit},
                )
            ).all()
            total = await session.scalar(text("SELECT pg_database_size(current_database())"))
        return {
            "available": True,
            "database_bytes": int(total or 0),
            "tables": [
                {
                    "table": row.relname,
                    "rows": max(0, int(row.rows or 0)),
                    "total_bytes": int(row.total_bytes or 0),
                    "table_bytes": int(row.table_bytes or 0),
                    "index_bytes": int(row.index_bytes or 0),
                    # Dead rows are space a vacuum has not reclaimed yet: a table
                    # that stays large after pruning is explained here.
                    "dead_rows": max(0, int(row.dead_rows or 0)),
                    "last_vacuum_at": (
                        row.last_vacuum_at.isoformat() if row.last_vacuum_at is not None else None
                    ),
                }
                for row in rows
            ],
        }
    except SQLAlchemyError:
        return {"available": False, "tables": [], "reason": "catalog not readable"}


async def slow_statements(session: AsyncSession, *, limit: int = 10) -> dict[str, object]:
    """The statement shapes that cost the most time since statistics were reset.

    pg_stat_statements normalises parameters away, so a shape names the code
    path and never a filename, prompt or identifier.
    """

    if session.get_bind().dialect.name != "postgresql":
        return {"available": False, "statements": []}
    try:
        async with session.begin_nested():
            rows = (
                await session.execute(
                    text(
                        "SELECT calls, mean_exec_time, max_exec_time, total_exec_time, "
                        "rows, shared_blks_read, left(query, 1600) AS query "
                        "FROM pg_stat_statements "
                        "WHERE dbid = (SELECT oid FROM pg_database "
                        "WHERE datname = current_database()) AND calls >= 3 "
                        "ORDER BY total_exec_time DESC LIMIT :limit"
                    ),
                    {"limit": limit},
                )
            ).all()
        return {
            "available": True,
            "statements": [
                {
                    "calls": int(row.calls or 0),
                    "mean_ms": round(float(row.mean_exec_time or 0), 1),
                    "max_ms": round(float(row.max_exec_time or 0), 1),
                    "total_ms": round(float(row.total_exec_time or 0), 1),
                    "rows": int(row.rows or 0),
                    "blocks_read": int(row.shared_blks_read or 0),
                    "statement": _query_shape(row.query),
                }
                for row in rows
            ],
        }
    except SQLAlchemyError:
        return {"available": False, "statements": [], "reason": "profiling extension not ready"}
