"""Read-only SQLite helper. Opens a fresh connection per request."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import Depends

from .config import settings


def _connect() -> sqlite3.Connection:
    """Open a read-only SQLite connection via URI mode.

    `mode=ro` enforces read-only at the OS level. Missing file or
    insufficient permissions raise `sqlite3.OperationalError`.
    """
    uri = f"file:{settings.db_path}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    """Yield a read-only connection; close on exit.

    Use as a context manager. Per-request connection is fine for
    3 req/min workload.
    """
    con = _connect()
    try:
        yield con
    finally:
        con.close()


def db_session() -> Generator[sqlite3.Connection, None, None]:
    """FastAPI dependency that yields a read-only connection per request."""
    with get_db() as con:
        yield con


# Shared FastAPI dependency type — use as `con: DbConnection` in endpoint
# signatures to avoid ruff B008 (function call in argument default).
DbConnection = Annotated[sqlite3.Connection, Depends(db_session)]

