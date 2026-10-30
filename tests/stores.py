"""Store harnesses shared by the contract suite and the property tests.

A harness owns one store and can restart it: close it and open a new one over
the same persistent state, as a process restart would.

Postgres runs when `POLITEFETCH_TEST_POSTGRES` holds a connection string
(`scripts/postgres-for-tests.sh` prints one); otherwise its tests are skipped,
unless `POLITEFETCH_REQUIRE_POSTGRES=1` (as in CI) turns that into an error.
Each Postgres harness works in a schema of its own and drops it at the end.
"""

import os
import tempfile
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg_pool import ConnectionPool

from politefetch.store import MemoryStore, SqliteStore, Store
from politefetch.store.postgres import PostgresStore

POSTGRES_DSN = os.environ.get("POLITEFETCH_TEST_POSTGRES", "")
if os.environ.get("POLITEFETCH_REQUIRE_POSTGRES") == "1" and not POSTGRES_DSN:
    raise RuntimeError(
        "POLITEFETCH_REQUIRE_POSTGRES=1 but POLITEFETCH_TEST_POSTGRES is unset"
    )

needs_postgres = pytest.mark.skipif(
    not POSTGRES_DSN,
    reason='no Postgres: run `eval "$(scripts/postgres-for-tests.sh)"` first',
)

KINDS = ["memory", "sqlite", pytest.param("postgres", marks=needs_postgres)]

# The contract suite also runs on a Postgres store that borrows its
# connections from a pool (more connections than one, so threads interleave).
CONTRACT_KINDS = [*KINDS, pytest.param("postgres-pool", marks=needs_postgres)]


def fresh_schema() -> str:
    return f"test_{uuid.uuid4().hex[:12]}"


def drop_schema(schema: str) -> None:
    with psycopg.connect(POSTGRES_DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
        )


class Harness:
    def __init__(self, kind: str, directory: Path | None = None) -> None:
        self.kind = kind
        self._tmp: tempfile.TemporaryDirectory[str] | None = None
        if directory is None:
            self._tmp = tempfile.TemporaryDirectory()
            directory = Path(self._tmp.name)
        self.path = directory / "store.db"
        self.schema = fresh_schema()
        self.pool: ConnectionPool | None = None
        self.store: Store = self._open(None)

    def _open(self, previous: Store | None) -> Store:
        if self.kind == "memory":
            data = previous.data if isinstance(previous, MemoryStore) else None
            return MemoryStore(data)
        if self.kind == "sqlite":
            return SqliteStore(self.path)
        if self.kind == "postgres":
            return PostgresStore(POSTGRES_DSN, schema=self.schema)
        if self.kind == "postgres-pool":
            if self.pool is None:
                self.pool = ConnectionPool(
                    POSTGRES_DSN, min_size=1, max_size=4, open=True
                )
            return PostgresStore(pool=self.pool, schema=self.schema)
        raise ValueError(f"unknown store kind {self.kind!r}")

    def restart(self) -> Store:
        """Close the store and open it again over the same state."""
        previous = self.store
        previous.close()
        self.store = self._open(previous)
        return self.store

    def another(self) -> Store:
        """A second store over the same state, as another process opens it;
        the caller closes it."""
        if isinstance(self.store, MemoryStore):
            return MemoryStore(self.store.data)
        return self._open(None)

    def close(self) -> None:
        self.store.close()
        if self.pool is not None:
            self.pool.close()
        if self.kind.startswith("postgres"):
            drop_schema(self.schema)
        if self._tmp is not None:
            self._tmp.cleanup()
