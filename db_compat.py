from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pg8000.dbapi


dict_row = object()


class CursorCompat:
    def __init__(self, cursor: Any, usar_dict: bool = False):
        self._cursor = cursor
        self._usar_dict = usar_dict

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._cursor.close()

    def execute(self, query: str, params: Any = None):
        self._cursor.execute(query, params or ())
        return self

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None or not self._usar_dict:
            return row
        return self._dict_row(row)

    def fetchall(self):
        rows = self._cursor.fetchall()
        if not self._usar_dict:
            return rows
        return [self._dict_row(row) for row in rows]

    def _dict_row(self, row: Any) -> dict[str, Any]:
        columnas = [col[0] for col in self._cursor.description or []]
        return dict(zip(columnas, row))


class ConnectionCompat:
    def __init__(self, conn: Any, usar_dict: bool = False):
        self._conn = conn
        self._usar_dict = usar_dict

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type:
                self._conn.rollback()
            else:
                self._conn.commit()
        finally:
            self._conn.close()

    def cursor(self):
        return CursorCompat(self._conn.cursor(), self._usar_dict)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    @contextmanager
    def transaction(self):
        try:
            yield self
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise


def connect(**kwargs: Any) -> ConnectionCompat:
    row_factory = kwargs.pop("row_factory", None)
    connect_timeout = kwargs.pop("connect_timeout", None)
    conn = pg8000.dbapi.connect(
        host=kwargs.get("host", "localhost"),
        port=int(kwargs.get("port", 5432)),
        database=kwargs.get("dbname") or kwargs.get("database"),
        user=kwargs.get("user") or kwargs.get("username"),
        password=kwargs.get("password"),
        timeout=connect_timeout,
    )
    return ConnectionCompat(conn, usar_dict=row_factory is dict_row)