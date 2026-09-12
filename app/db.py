"""
Database connection helper.

Responsibility:
- pyodbc connection pool / session helper for SQL Server.
- Provides a connection (or cursor) to routers/services without each
  of them managing pyodbc connection details directly.
- Handles the dcm / Test two-database, single-instance setup
  (three-part naming, no linked server).
"""

from contextlib import contextmanager
from typing import Any, Iterable, Optional

import pyodbc

from app.config import settings


@contextmanager
def get_connection():
    conn = pyodbc.connect(settings.connection_string)
    try:
        yield conn
    finally:
        conn.close()


def fetch_all(query: str, params: Iterable[Any] = ()) -> list[dict]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, tuple(params))
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def fetch_one(query: str, params: Iterable[Any] = ()) -> Optional[dict]:
    rows = fetch_all(query, params)
    return rows[0] if rows else None


def execute(query: str, params: Iterable[Any] = ()) -> None:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, tuple(params))
        conn.commit()


def execute_returning_identity(query: str, params: Iterable[Any] = ()) -> int:
    """Run an INSERT and return the new row's IDENTITY value."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, tuple(params))
        cursor.execute("SELECT SCOPE_IDENTITY()")
        new_id = cursor.fetchone()[0]
        conn.commit()
        return int(new_id)
