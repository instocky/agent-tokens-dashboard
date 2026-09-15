"""Read-only SQLite helper. Opens a fresh connection per request.

The connection is created **inside the endpoint body** via
`with get_db() as con:` so it lives, is used, and closes all in the
same worker thread. Previously this was a FastAPI `Depends()` --
but the dependency resolves in the event-loop thread while the sync
endpoint runs in a worker thread via `anyio.to_thread`, so even
`sqlite3.connect(..., check_same_thread=False)` raised at
`con.close()` (CPython sqlite3 still checks thread affinity there).
Moving the open into the endpoint body keeps creation, use, and
close all on one thread.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

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

    Use inside the endpoint body:

        @router.get(...)
        def endpoint() -> Snapshot:
            with get_db() as con:
                return svc.build_snapshot(con, now_in_tz())
    """
    con = _connect()
    try:
        yield con
    finally:
        con.close()

