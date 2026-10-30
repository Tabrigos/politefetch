"""Where the Postgres store gets its connections: one of its own, or one
borrowed from the caller's pool for each transaction.

A connection of its own serves one transaction at a time, so the threads of a
process sharing the store queue for it; one found lost is opened again at the
next borrow. A pool lends a connection per transaction, so the threads write
at once; the pool owns its connections, checks them and replaces the lost
ones, and closing the store leaves it open.

Statements are never prepared: a pooler in transaction mode (pgbouncer) may
hand the next transaction a server connection that never saw them. A borrowed
connection gets its own settings back when it returns to the pool.
"""

import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Protocol, cast

import psycopg
from psycopg.rows import tuple_row

__all__ = ["Connections", "OwnConnection", "Pool", "PooledConnections"]

Conn = psycopg.Connection[tuple[Any, ...]]


class Pool(Protocol):
    """What the store needs of a pool: `psycopg_pool.ConnectionPool` has it."""

    def connection(self) -> AbstractContextManager[psycopg.Connection[Any]]: ...


class Connections(Protocol):
    def borrow(self) -> AbstractContextManager[Conn]:
        """A connection for one transaction, given back at the end."""
        ...

    def close(self) -> None: ...


class OwnConnection:
    """One connection, opened by the store and closed with it."""

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo
        self._lock = threading.RLock()
        self.conn: Conn | None = self._connect()

    def _connect(self) -> Conn:
        return psycopg.connect(self._conninfo, autocommit=True, prepare_threshold=None)

    @contextmanager
    def borrow(self) -> Iterator[Conn]:
        with self._lock:
            if self.conn is None:
                raise RuntimeError("the store is closed")
            if self.conn.closed or self.conn.broken:
                self.conn = self._connect()
            yield self.conn

    def close(self) -> None:
        with self._lock:
            if self.conn is not None:
                self.conn.close()
                self.conn = None


class PooledConnections:
    """Connections borrowed from the caller's pool, which stays the caller's."""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool
        self._closed = False

    @contextmanager
    def borrow(self) -> Iterator[Conn]:
        if self._closed:
            raise RuntimeError("the store is closed")
        with self._pool.connection() as conn:
            saved = conn.prepare_threshold, conn.row_factory
            conn.prepare_threshold = None
            conn.row_factory = tuple_row
            try:
                yield cast(Conn, conn)
            finally:
                conn.prepare_threshold, conn.row_factory = saved

    def close(self) -> None:
        self._closed = True
