import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

import psycopg
from opentelemetry import trace
from psycopg import sql
from psycopg.rows import dict_row

from services.common.tracing import mark_span_error, operation_span


@asynccontextmanager
async def database_span(operation: str, table_name: str | None = None) -> AsyncIterator[trace.Span]:
    attributes = {"db.system.name": "postgresql", "db.operation.name": operation}
    span_name = operation
    if table_name is not None:
        attributes["db.collection.name"] = table_name
        span_name = f"{operation} {table_name}"

    with operation_span(span_name, kind=trace.SpanKind.CLIENT, attributes=attributes) as span:
        try:
            yield span
        except Exception as exc:
            mark_span_error(span, type(exc).__name__)
            if isinstance(exc, psycopg.Error) and exc.sqlstate:
                span.set_attribute("db.response.status_code", exc.sqlstate)
            raise


async def connect(
    *, default_port: str = "5432", default_password: str = ""
) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(
        host=os.getenv("PGHOST", "localhost"),
        port=os.getenv("PGPORT", default_port),
        dbname=os.getenv("POSTGRES_DB", "tracepilot"),
        user=os.getenv("POSTGRES_USER", "tracepilot"),
        password=os.getenv("POSTGRES_PASSWORD", default_password),
        connect_timeout=2,
        options="-c statement_timeout=3000",
        row_factory=dict_row,
    )


async def check_database(table_name: Literal["game_sessions", "game_events"]) -> None:
    async with await connect() as connection:
        await connection.execute(
            sql.SQL("SELECT 1 FROM {} LIMIT 0").format(sql.Identifier(table_name))
        )
