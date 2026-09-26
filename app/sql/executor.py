"""Run validated SELECT queries against the sample database.

Defence in depth: even though every query is validated first, execution uses a
read-only connection, the same read-only authorizer, a row cap and a timeout.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from .validator import _ReadOnlyAuthorizer, open_readonly


class QueryTimeout(Exception):
    pass


@dataclass
class QueryResult:
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: float = 0.0
    error: str = ""

    def to_dict(self) -> dict:
        return {"columns": self.columns, "rows": self.rows, "row_count": self.row_count,
                "truncated": self.truncated, "elapsed_ms": round(self.elapsed_ms, 1),
                "error": self.error}


def _jsonable(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    return value


def execute_query(sql: str, db_path: str | Path, max_rows: int = 200,
                  timeout_seconds: float = 5.0) -> QueryResult:
    result = QueryResult()
    conn = open_readonly(db_path)
    started = time.perf_counter()
    deadline = started + timeout_seconds

    def check_deadline():
        return 1 if time.perf_counter() > deadline else 0   # non-zero aborts the query

    try:
        conn.set_authorizer(_ReadOnlyAuthorizer())
        conn.set_progress_handler(check_deadline, 10_000)
        cursor = conn.execute(sql)
        result.columns = [d[0] for d in cursor.description or []]
        rows = cursor.fetchmany(max_rows + 1)
        result.truncated = len(rows) > max_rows
        result.rows = [[_jsonable(v) for v in r] for r in rows[:max_rows]]
        result.row_count = len(result.rows)
    except sqlite3.OperationalError as exc:
        result.error = (f"The query took longer than {timeout_seconds:g}s and was stopped."
                        if "interrupted" in str(exc) else f"Execution failed: {exc}")
    except sqlite3.Error as exc:
        result.error = f"Execution failed: {exc}"
    finally:
        result.elapsed_ms = (time.perf_counter() - started) * 1000
        conn.close()
    return result
