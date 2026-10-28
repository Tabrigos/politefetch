"""Schema versions and migrations of the stores that keep a database.

A store written by an earlier version must open with every promise, count
and freeze it holds: recreating it would forget them. The fixtures in
`fixtures/` were written by politefetch 0.1.1 itself (`make_store_0_1.py`),
and `store-0.1.1-integration.db` by a first integration, run against the
simulator.
"""

import shutil
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql

from politefetch import FakeClock, Gate, HostPolicy
from politefetch.errors import RefusalReason
from politefetch.store import RowState, SqliteStore
from politefetch.store.postgres import _MIGRATIONS as PG_MIGRATIONS
from politefetch.store.postgres import PostgresStore

from .stores import POSTGRES_DSN, drop_schema, fresh_schema, needs_postgres

FIXTURES = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 10, 20, 12, tzinfo=UTC)  # when the 0.1.1 fixture begins
LATER = datetime(2026, 10, 20, 16, 30, tzinfo=UTC)  # after it was written
LATEST = 2  # the schema version of this politefetch

# Two migrations where the second needs the first: run out of order, or twice,
# they fail.
COLUMN_THEN_INDEX = (
    ("ALTER TABLE hosts ADD COLUMN extra TEXT",),
    ("CREATE INDEX hosts_by_extra ON hosts (extra)",),
)


def copy(name: str, tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    target = tmp_path / name
    shutil.copyfile(FIXTURES / name, target)
    return target


def raw(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def version(path: Path) -> int | None:
    conn = raw(path)
    try:
        found = conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'schema_version'"
        ).fetchone()
        if found is None:
            return None
        rows = conn.execute("SELECT version FROM schema_version").fetchall()
        assert len(rows) == 1, rows
        return int(rows[0][0])
    finally:
        conn.close()


def contents(path: Path) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of every table but the version, in a stable order."""
    conn = raw(path)
    try:
        tables = [
            name
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
            if name != "schema_version"
        ]
        return {
            table: sorted(conn.execute(f"SELECT * FROM {table}").fetchall(), key=repr)
            for table in tables
        }
    finally:
        conn.close()


def columns(path: Path, table: str) -> set[str]:
    conn = raw(path)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


# --- SQLite -------------------------------------------------------------------


def test_a_new_store_records_its_version(tmp_path: Path) -> None:
    path = tmp_path / "new.db"
    SqliteStore(path).close()
    assert version(path) == LATEST


@pytest.mark.parametrize("name", ["store-0.1.1.db", "store-0.1.1-integration.db"])
def test_a_store_written_by_0_1_opens_with_every_row(tmp_path: Path, name: str) -> None:
    path = copy(name, tmp_path)
    assert version(path) is None
    before = contents(path)
    assert sum(len(rows) for rows in before.values()) > 10  # the fixture has state
    SqliteStore(path).close()
    assert version(path) == LATEST
    migrated = {**before, "host_caps": []}  # version 2 adds an empty table
    assert contents(path) == migrated
    SqliteStore(path).close()  # opening again changes nothing
    assert contents(path) == migrated


def test_a_store_written_by_0_1_keeps_its_promises_counts_and_freezes(
    tmp_path: Path,
) -> None:
    path = copy("store-0.1.1.db", tmp_path)
    with SqliteStore(path) as store:
        ok = store.host_state("ok.example", LATER)
        # The row left in flight at T0 + 4h promised T0 + 4h + 10m.
        assert ok.not_before == T0 + timedelta(hours=4, minutes=10)
        assert (ok.requests_today, ok.in_flight) == (2, 1)
        assert (ok.wire_bytes_today, ok.decoded_bytes_today) == (120, 480)

        frozen = store.host_state("frozen.example", LATER).freeze
        assert frozen is not None
        assert (frozen.since, frozen.until, frozen.status) == (T0, None, 429)

        assert store.host_state("lifted.example", LATER).freeze is None
        [lifted] = store.unfreezes(host="lifted.example")
        assert (lifted.by, lifted.note, lifted.freeze.status) == (
            "operator",
            "checked by hand",
            403,
        )

        assert store.host_state("failing.example", LATER).failures == 2

        record = store.url_record("https://ok.example/data.json")
        assert record is not None
        assert record.validators.etag == '"v1"'
        data_time = store.data_time_record("https://ok.example/data.json")
        assert data_time is not None
        assert data_time.latest == T0 - timedelta(minutes=30)

        states = sorted(row.state.value for row in store.rows())
        assert states.count(RowState.IN_FLIGHT.value) == 1
        assert states.count(RowState.RECONCILED.value) == 1

        gate = Gate(
            store,
            {"frozen.example": HostPolicy(daily_cap=100, min_interval="10m")},
            clock=FakeClock(LATER),
        )
        refusal = gate.may_fetch("https://frozen.example/feed").refusal
        assert refusal is not None
        assert refusal.reason is RefusalReason.FROZEN


def test_migrations_run_in_order_and_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = copy("store-0.1.1.db", tmp_path)
    before = contents(path)
    monkeypatch.setattr("politefetch.store.sqlite._MIGRATIONS", COLUMN_THEN_INDEX)
    SqliteStore(path).close()
    assert version(path) == 3
    assert "extra" in columns(path, "hosts")
    SqliteStore(path).close()  # a second run would add the column twice
    assert version(path) == 3
    after = contents(path)
    assert after["hosts"] == [(*row, None) for row in before["hosts"]]


def test_a_new_store_runs_the_migrations_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("politefetch.store.sqlite._MIGRATIONS", COLUMN_THEN_INDEX)
    path = tmp_path / "new.db"
    SqliteStore(path).close()
    assert version(path) == 3
    assert "extra" in columns(path, "hosts")


def test_a_failed_migration_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = copy("store-0.1.1.db", tmp_path)
    before = contents(path)
    broken = (COLUMN_THEN_INDEX[0], ("CREATE INDEX broken ON nowhere (x)",))
    monkeypatch.setattr("politefetch.store.sqlite._MIGRATIONS", broken)
    with pytest.raises(sqlite3.OperationalError, match="nowhere"):
        SqliteStore(path)
    assert version(path) is None
    assert "extra" not in columns(path, "hosts")
    assert contents(path) == before


def test_a_store_from_a_newer_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "newer.db"
    SqliteStore(path).close()
    conn = sqlite3.connect(path)
    conn.execute("UPDATE schema_version SET version = 99")
    conn.commit()
    conn.close()
    before = contents(path)
    with pytest.raises(ValueError, match="version 99"):
        SqliteStore(path)
    assert version(path) == 99
    assert contents(path) == before


def open_together(path: Path, start: threading.Barrier, errors: list[Any]) -> None:
    start.wait()
    try:
        SqliteStore(path).close()
    except BaseException as error:
        errors.append(error)


def test_two_stores_opening_at_once_migrate_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for round_ in range(10):
        path = copy("store-0.1.1.db", tmp_path / str(round_))
        # Opened once first, at version 1, so the file is in WAL mode: the
        # race on switching to it is another matter.
        monkeypatch.setattr("politefetch.store.sqlite._MIGRATIONS", ())
        SqliteStore(path).close()
        monkeypatch.setattr("politefetch.store.sqlite._MIGRATIONS", COLUMN_THEN_INDEX)
        start = threading.Barrier(6)
        errors: list[Any] = []
        threads = [
            threading.Thread(target=open_together, args=(path, start, errors))
            for _ in range(6)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert version(path) == 3
        monkeypatch.undo()


def shape(path: Path) -> dict[str, Any]:
    """The columns of every table and the columns of every index."""
    conn = raw(path)
    try:
        found: dict[str, Any] = {}
        for (table,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ):
            found[table] = [r[1:] for r in conn.execute(f"PRAGMA table_info({table})")]
        for index, table in conn.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE type = 'index'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ):
            on = [r[2] for r in conn.execute(f"PRAGMA index_info({index})")]
            found[f"index {index}"] = (table, on)
        return found
    finally:
        conn.close()


@pytest.mark.parametrize(
    "migrations", [None, COLUMN_THEN_INDEX], ids=["as shipped", "two more"]
)
def test_a_new_store_has_the_schema_of_a_migrated_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrations: tuple[tuple[str, ...], ...] | None,
) -> None:
    # The base schema stays at version 1: a column added to it, and not as a
    # migration, would reach new stores and not migrated ones.
    if migrations is not None:
        monkeypatch.setattr("politefetch.store.sqlite._MIGRATIONS", migrations)
    new = tmp_path / "new.db"
    SqliteStore(new).close()
    old = copy("store-0.1.1.db", tmp_path)
    SqliteStore(old).close()
    assert len(shape(new)) >= 9  # tables and indexes to compare
    assert shape(new) == shape(old)


# --- Postgres -----------------------------------------------------------------


@pytest.fixture
def schema() -> Iterator[str]:
    name = fresh_schema()
    try:
        yield name
    finally:
        drop_schema(name)


def pg(schema: str, query: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(POSTGRES_DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        cursor = conn.execute(query.encode())
        return cursor.fetchall() if cursor.description else []


def pg_version(schema: str) -> int | None:
    found = pg(
        schema,
        "SELECT 1 FROM information_schema.tables"
        f" WHERE table_schema = '{schema}' AND table_name = 'schema_version'",
    )
    if not found:
        return None
    rows = pg(schema, "SELECT version FROM schema_version")
    assert len(rows) == 1, rows
    return int(rows[0][0])


def pg_columns(schema: str, table: str) -> set[str]:
    rows = pg(
        schema,
        "SELECT column_name FROM information_schema.columns"
        f" WHERE table_schema = '{schema}' AND table_name = '{table}'",
    )
    return {r[0] for r in rows}


def written_by_0_1(schema: str) -> None:
    """A schema as 0.1 leaves it: the tables and some state, no version.
    The state is written by this version, then the tables 0.1 did not have
    are dropped."""
    with PostgresStore(POSTGRES_DSN, schema=schema) as store:
        policy = HostPolicy(daily_cap=100, min_interval="10m")
        gate = Gate(store, {"frozen.example": policy}, clock=FakeClock(T0))
        with gate.attempt("https://frozen.example/feed") as attempt:
            attempt.response(429)
    pg(schema, "DROP TABLE schema_version")
    pg(schema, "DROP TABLE host_caps")  # version 2


@needs_postgres
def test_a_new_postgres_schema_records_its_version(schema: str) -> None:
    PostgresStore(POSTGRES_DSN, schema=schema).close()
    assert pg_version(schema) == LATEST


@needs_postgres
def test_a_postgres_schema_written_by_0_1_opens_with_its_state(schema: str) -> None:
    written_by_0_1(schema)
    assert pg_version(schema) is None
    with pytest.raises(ValueError, match="migrate=True"):
        PostgresStore(POSTGRES_DSN, schema=schema)
    with PostgresStore(POSTGRES_DSN, schema=schema, migrate=True) as store:
        freeze = store.host_state("frozen.example", LATER).freeze
        assert freeze is not None
        assert freeze.status == 429
    assert pg_version(schema) == LATEST


@needs_postgres
def test_postgres_migrates_only_when_asked(
    schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    written_by_0_1(schema)
    # After the real ones, which the store's queries need.
    monkeypatch.setattr(
        "politefetch.store.postgres._MIGRATIONS", PG_MIGRATIONS + COLUMN_THEN_INDEX
    )
    with pytest.raises(ValueError, match="migrate=True"):
        PostgresStore(POSTGRES_DSN, schema=schema)
    assert "extra" not in pg_columns(schema, "hosts")
    PostgresStore(POSTGRES_DSN, schema=schema, migrate=True).close()
    assert pg_version(schema) == LATEST + 2
    assert "extra" in pg_columns(schema, "hosts")
    with PostgresStore(POSTGRES_DSN, schema=schema) as store:  # now current
        freeze = store.host_state("frozen.example", LATER).freeze
        assert freeze is not None


@needs_postgres
def test_a_new_postgres_schema_runs_the_migrations_without_asking(
    schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("politefetch.store.postgres._MIGRATIONS", COLUMN_THEN_INDEX)
    PostgresStore(POSTGRES_DSN, schema=schema).close()
    assert pg_version(schema) == 3


@needs_postgres
def test_a_failed_postgres_migration_changes_nothing(
    schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    written_by_0_1(schema)
    broken = (COLUMN_THEN_INDEX[0], ("CREATE INDEX broken ON nowhere (x)",))
    monkeypatch.setattr("politefetch.store.postgres._MIGRATIONS", broken)
    with pytest.raises(psycopg.errors.UndefinedTable):
        PostgresStore(POSTGRES_DSN, schema=schema, migrate=True)
    assert pg_version(schema) is None
    assert "extra" not in pg_columns(schema, "hosts")


@needs_postgres
def test_a_postgres_schema_from_a_newer_version_is_refused(schema: str) -> None:
    PostgresStore(POSTGRES_DSN, schema=schema).close()
    pg(schema, "UPDATE schema_version SET version = 99")
    with pytest.raises(ValueError, match="version 99"):
        PostgresStore(POSTGRES_DSN, schema=schema, migrate=True)
    assert pg_version(schema) == 99


def pg_shape(schema: str) -> dict[str, Any]:
    """Columns and indexes, with the schema's name taken out."""
    columns = pg(
        schema,
        "SELECT table_name, column_name, data_type, is_nullable, column_default"
        f" FROM information_schema.columns WHERE table_schema = '{schema}'"
        " ORDER BY table_name, ordinal_position",
    )
    indexes = pg(
        schema,
        "SELECT indexname, indexdef FROM pg_indexes"
        f" WHERE schemaname = '{schema}' ORDER BY indexname",
    )
    return {
        "columns": [tuple(str(v).replace(schema, "S") for v in r) for r in columns],
        "indexes": [(n, d.replace(schema, "S")) for n, d in indexes],
    }


@needs_postgres
@pytest.mark.parametrize(
    "migrations", [None, COLUMN_THEN_INDEX], ids=["as shipped", "two more"]
)
def test_a_new_postgres_schema_has_the_schema_of_a_migrated_one(
    schema: str,
    monkeypatch: pytest.MonkeyPatch,
    migrations: tuple[tuple[str, ...], ...] | None,
) -> None:
    if migrations is not None:
        monkeypatch.setattr("politefetch.store.postgres._MIGRATIONS", migrations)
    # `schema` gets the tables exactly as 0.1.1 created them, then migrates.
    pg(schema, f"CREATE SCHEMA {schema}")
    ddl = (FIXTURES / "postgres-0.1.1.sql").read_text(encoding="utf-8")
    for statement in ddl.splitlines():
        if statement and not statement.startswith("--"):
            pg(schema, statement)
    PostgresStore(POSTGRES_DSN, schema=schema, migrate=True).close()
    new = fresh_schema()
    try:
        PostgresStore(POSTGRES_DSN, schema=new).close()
        expected = pg_shape(schema)
        assert len(expected["columns"]) > 30  # tables to compare
        assert pg_shape(new) == expected
    finally:
        drop_schema(new)
