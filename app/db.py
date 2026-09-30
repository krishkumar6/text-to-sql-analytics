"""Read-only query execution.

Every query runs in its own READ ONLY transaction with a statement_timeout and a row cap, and the
transaction is always rolled back. This sits on top of the analytics_ro role's SELECT-only grants;
neither this module nor the SQL guard is the security boundary on its own.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


class QueryError(Exception):
    """The database rejected or aborted the query. The message is safe to show the LLM."""


@dataclass
class QueryResult:
    sql: str
    columns: list[str]
    rows: list[list[Any]]  # JSON-safe values
    truncated: bool  # more rows existed than the cap allowed
    elapsed_ms: int

    @property
    def row_count(self) -> int:
        return len(self.rows)


def to_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, memoryview)):
        return "<binary>"
    if isinstance(value, (list, tuple)):
        return [to_json_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_json_value(v) for k, v in value.items()}
    return str(value)


class Database:
    def __init__(self, url: str, *, statement_timeout_ms: int, max_rows: int) -> None:
        self.engine: Engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5,
                                            connect_args={"connect_timeout": 5})
        self.statement_timeout_ms = statement_timeout_ms
        self.max_rows = max_rows

    def execute(self, sql: str, params: dict[str, Any] | None = None, *, max_rows: int | None = None) -> QueryResult:
        cap = max_rows or self.max_rows
        started = time.perf_counter()
        try:
            conn = self.engine.connect()
        except Exception as exc:  # database down / unreachable
            raise QueryError(f"Could not connect to the database: {_db_error_message(exc)}") from exc
        with conn:
            try:
                conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                conn.exec_driver_sql(f"SET LOCAL statement_timeout = {int(self.statement_timeout_ms)}")
                if params:
                    result = conn.execute(text(sql), params)
                else:
                    # no_parameters: pass the SQL through untouched so a literal '%' isn't read as a placeholder.
                    result = conn.exec_driver_sql(sql, execution_options={"no_parameters": True})
                columns = list(result.keys())
                fetched = result.fetchmany(cap + 1)
            except Exception as exc:  # driver errors vary; surface the DB's own message
                raise QueryError(_db_error_message(exc)) from exc
            finally:
                conn.rollback()
        return QueryResult(
            sql=sql,
            columns=columns,
            rows=[[to_json_value(v) for v in row] for row in fetched[:cap]],
            truncated=len(fetched) > cap,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    def ping(self) -> bool:
        try:
            self.execute("SELECT 1")
            return True
        except QueryError:
            return False

    def quote_identifier(self, name: str) -> str:
        return self.engine.dialect.identifier_preparer.quote(name)


def _db_error_message(exc: Exception) -> str:
    orig = getattr(exc, "orig", None) or exc
    message = str(orig).strip()
    # Drop SQLAlchemy's "(Background on this error at ...)" footer and similar noise.
    return message.split("\n(Background on this error")[0][:1500]
