from __future__ import annotations

from importlib.resources import files

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

CONN_KWARGS = {"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}


def make_pool(url: str, max_size: int = 8) -> ConnectionPool:
    """One pool for the application tables and the LangGraph checkpointer.

    PostgresSaver needs autocommit and dict rows, so every connection is opened that way and
    multi-statement writes use an explicit ``conn.transaction()``.
    """
    return ConnectionPool(url, min_size=1, max_size=max_size, kwargs=CONN_KWARGS, open=True)


def create_schema(pool: ConnectionPool, embed_dim: int) -> None:
    sql = files("coursedraft").joinpath("schema.sql").read_text(encoding="utf-8")
    with pool.connection() as conn:
        # A multi-statement script can't be a prepared statement.
        conn.execute(sql.replace("{embed_dim}", str(int(embed_dim))), prepare=False)
    PostgresSaver(pool).setup()


def reset_db(url: str) -> None:
    """Drop everything, including the checkpoint tables. Used by the tests and `seed --reset`."""
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"
