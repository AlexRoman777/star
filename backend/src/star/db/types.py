from __future__ import annotations

from sqlalchemy import Text, cast
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.types import TypeDecorator


class HalfVector384(TypeDecorator[str]):
    """PostgreSQL halfvec(384), represented as its stable text form in Python and SQLite."""

    impl = Text
    cache_ok = True

    def bind_expression(self, bindvalue: ColumnElement[str]) -> ColumnElement[str]:
        """Cast the text representation explicitly for PostgreSQL/asyncpg.

        The PostgreSQL compiler renders the column as ``halfvec(384)``, while the
        TypeDecorator's portable implementation remains text for SQLite. Asyncpg
        otherwise types the parameter as VARCHAR, which PostgreSQL correctly refuses
        to assign to a halfvec column.
        """

        return cast(bindvalue, self)


@compiles(HalfVector384, "postgresql")
def _compile_half_vector_384(_type: HalfVector384, _compiler: object, **_kw: object) -> str:
    # PostgreSQL reflection instantiates the registered halfvec type with its
    # declared dimension. Respect that value so Alembic can compare reflected
    # halfvec(768) columns with the explicit HalfVector768 ORM type correctly.
    reflected_length = getattr(_type, "length", None)
    return f"halfvec({reflected_length if isinstance(reflected_length, int) else 384})"


class HalfVector768(TypeDecorator[str]):
    """PostgreSQL halfvec(768), represented as its stable text form in Python and SQLite."""

    impl = Text
    cache_ok = True

    def bind_expression(self, bindvalue: ColumnElement[str]) -> ColumnElement[str]:
        return cast(bindvalue, self)


@compiles(HalfVector768, "postgresql")
def _compile_half_vector_768(_type: HalfVector768, _compiler: object, **_kw: object) -> str:
    return "halfvec(768)"
