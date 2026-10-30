"""Postgres-specific behaviour, beyond the shared contract: a store that
keeps working when the session under it is reset or lost, that refuses a
machine clock far from the server's, whose writers wait only for those of
their own host, and that borrows its connections from a pool.

A connection pooler in transaction mode (pgbouncer) hands each transaction a
server connection that may have been reset in between (`DISCARD ALL`): the
store must not rely on anything set for the session.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import cast

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from politefetch.clock import FakeClock
from politefetch.errors import RefusalReason
from politefetch.store import HostState, HostUpdate, Outcome, RequestRow
from politefetch.store._postgres_connections import OwnConnection
from politefetch.store.postgres import PostgresStore, _lock_key

from .stores import POSTGRES_DSN, Harness, drop_schema, fresh_schema, needs_postgres

pytestmark = needs_postgres

T = datetime(2026, 10, 16, 12, tzinfo=UTC)
URL = "https://example.org/data.json"
LATER = T + timedelta(hours=1)


@pytest.fixture
def store() -> Iterator[PostgresStore]:
    harness = Harness("postgres")
    assert isinstance(harness.store, PostgresStore)
    try:
        yield harness.store
    finally:
        harness.close()


def session(store: PostgresStore) -> psycopg.Connection[tuple[object, ...]]:
    assert isinstance(store._connections, OwnConnection)
    connection = store._connections.conn
    assert connection is not None
    return connection


def test_a_session_reset_between_transactions_changes_nothing(
    store: PostgresStore,
) -> None:
    for minutes in range(8):  # enough for psycopg to prepare its statements
        store.note_data_time(URL, T, now=T + timedelta(minutes=minutes))
    session(store).execute("DISCARD ALL")  # what pgbouncer does between clients
    record = store.data_time_record(URL)
    assert record is not None
    assert record.reads == 8


def test_times_stay_utc_whatever_the_session_time_zone(store: PostgresStore) -> None:
    session(store).execute("SET TIME ZONE 'Europe/Rome'")
    store.note_data_time(URL, T, now=T)
    record = store.data_time_record(URL)
    assert record is not None
    assert record.latest == T
    assert record.latest.utcoffset() == timedelta(0)


def test_a_dropped_connection_is_opened_again(store: PostgresStore) -> None:
    store.note_data_time(URL, T, now=T)
    lost = session(store).info.backend_pid
    with psycopg.connect(POSTGRES_DSN, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (lost,))
    record = store.note_data_time(URL, T, now=T + timedelta(minutes=1))
    assert record.reads == 2
    assert session(store).info.backend_pid != lost


def test_a_clock_far_from_the_server_is_refused() -> None:
    schema = fresh_schema()
    try:
        with pytest.raises(ValueError, match="clock"):
            PostgresStore(
                POSTGRES_DSN,
                schema=schema,
                clock=FakeClock(datetime(2000, 1, 1, tzinfo=UTC)),
            )
    finally:
        drop_schema(schema)


# --- locks ----------------------------------------------------------------------


def wait_inside(
    store: PostgresStore, host: str, entered: threading.Event, release: threading.Event
) -> None:
    def cautious(_state: HostState) -> datetime:
        entered.set()
        release.wait(30)
        return LATER + timedelta(minutes=5)

    # Later than the other requests of the tests, which their promises
    # would refuse.
    store.open_request(
        host, f"https://{host}/", now=LATER, daily_cap=10, cautious=cautious
    )


@contextmanager
def writing(store: PostgresStore, host: str) -> Iterator[None]:
    """`store` stays inside the transaction of a request to `host`."""
    entered, release = threading.Event(), threading.Event()
    thread = threading.Thread(target=wait_inside, args=(store, host, entered, release))
    thread.start()
    try:
        assert entered.wait(30), "the writer never entered its transaction"
        yield
    finally:
        release.set()
        thread.join(30)


def request(store: PostgresStore, host: str) -> RequestRow:
    return store.open_request(
        host,
        f"https://{host}/data.json",
        now=T,
        daily_cap=10,
        cautious=lambda _s: T + timedelta(minutes=5),
    )


@pytest.fixture
def other(store: PostgresStore) -> Iterator[PostgresStore]:
    """A second store on the same schema, as another machine opens it, that
    gives up on a lock after half a second."""
    with PostgresStore(
        POSTGRES_DSN, schema=store.schema, lock_timeout=0.5, create=False
    ) as second:
        yield second


def test_a_writer_waits_only_for_the_writers_of_its_host(
    store: PostgresStore, other: PostgresStore
) -> None:
    row = request(other, "a.example")

    def close() -> None:
        other.close_request(
            row.id,
            now=T,
            outcome=Outcome(ok=True, status=200),
            update=lambda _s: HostUpdate(T, RefusalReason.MIN_INTERVAL),
        )

    close()  # a host takes one request at a time
    with writing(store, "a.example"):
        request(other, "b.example")
        other.note_data_time(URL, T, now=T)
        assert other.unfreeze("b.example", now=T, by="ops") is None
        with pytest.raises(psycopg.errors.LockNotAvailable):
            request(other, "a.example")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            close()  # even one that finds the row closed
        with pytest.raises(psycopg.errors.LockNotAvailable):
            other.unfreeze("a.example", now=T, by="ops")
    assert [r.host for r in other.rows()] == ["a.example", "b.example", "a.example"]


def test_operations_across_hosts_wait_for_every_writer(
    store: PostgresStore, other: PostgresStore
) -> None:
    with writing(store, "a.example"):
        with pytest.raises(psycopg.errors.LockNotAvailable):
            other.reconcile(now=T, older_than=timedelta(minutes=30))
        with pytest.raises(psycopg.errors.LockNotAvailable):
            other.prune(now=T, older_than=timedelta(days=1))
        with pytest.raises(psycopg.errors.LockNotAvailable):
            PostgresStore.setup(POSTGRES_DSN, schema=store.schema, lock_timeout=0.5)
        # Opening a store that may create the schema waits too.
        with pytest.raises(psycopg.errors.LockNotAvailable):
            PostgresStore(POSTGRES_DSN, schema=store.schema, lock_timeout=0.5)


def test_the_data_times_of_a_url_are_written_one_at_a_time(
    store: PostgresStore, other: PostgresStore
) -> None:
    with psycopg.connect(POSTGRES_DSN, autocommit=True) as admin, admin.transaction():
        # What a writer of the URL's data time holds.
        admin.execute(
            "SELECT pg_advisory_xact_lock_shared(%s)", (_lock_key(store.schema),)
        )
        admin.execute(
            "SELECT pg_advisory_xact_lock(%s)", (_lock_key(store.schema, f"url:{URL}"),)
        )
        other.note_data_time(URL + "?other", T, now=T)
        with pytest.raises(psycopg.errors.LockNotAvailable):
            other.note_data_time(URL, T, now=T)


def test_the_schema_lock_is_the_one_0_1_took() -> None:
    # A process of 0.1 takes this key for every write: the writers of this
    # version, which take it shared, still wait for it and it for them.
    # The value 0.1.1's `_lock_key` gives.
    assert _lock_key("politefetch") == -2270042712084238515


def test_every_writer_waits_for_an_operation_across_hosts(
    store: PostgresStore, other: PostgresStore
) -> None:
    with psycopg.connect(POSTGRES_DSN, autocommit=True) as admin, admin.transaction():
        # What reconcile, prune and setup hold.
        admin.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key(store.schema),))
        with pytest.raises(psycopg.errors.LockNotAvailable):
            request(other, "b.example")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            other.note_data_time(URL, T, now=T)
        with pytest.raises(psycopg.errors.LockNotAvailable):
            other.unfreeze("b.example", now=T, by="ops")
    request(other, "b.example")


# --- pools ----------------------------------------------------------------------


def test_threads_on_a_pool_write_to_two_hosts_at_once() -> None:
    schema = fresh_schema()
    try:
        with (
            ConnectionPool(POSTGRES_DSN, min_size=2, max_size=2, open=True) as pool,
            PostgresStore(pool=pool, schema=schema, lock_timeout=0.5) as store,
            writing(store, "a.example"),
        ):
            request(store, "b.example")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                request(store, "a.example")
    finally:
        drop_schema(schema)


def test_a_borrowed_connection_goes_back_to_the_pool_as_it_was() -> None:
    schema = fresh_schema()
    kwargs = {"prepare_threshold": 3, "row_factory": dict_row}
    try:
        with ConnectionPool(
            POSTGRES_DSN, min_size=1, max_size=1, kwargs=kwargs, open=True
        ) as pool:
            with PostgresStore(pool=pool, schema=schema) as store:
                for minutes in range(8):  # enough for psycopg to prepare
                    store.note_data_time(URL, T, now=T + timedelta(minutes=minutes))
                assert store.rows() == []
            with pool.connection(timeout=5) as conn:  # it was given back
                assert conn.prepare_threshold == 3
                found = cast(object, conn.execute("SELECT 1 AS one").fetchone())
                assert found == {"one": 1}
    finally:
        drop_schema(schema)


def test_a_session_reset_under_a_pool_that_prepares_changes_nothing() -> None:
    schema = fresh_schema()
    try:
        with (
            ConnectionPool(
                POSTGRES_DSN,
                min_size=1,
                max_size=1,
                kwargs={"prepare_threshold": 0},  # prepares every statement
                open=True,
            ) as pool,
            PostgresStore(pool=pool, schema=schema) as store,
        ):
            store.note_data_time(URL, T, now=T)
            with pool.connection() as conn:
                # What pgbouncer does between clients, behind psycopg's back:
                # psycopg forgets its prepared statements on a DISCARD it sees.
                conn.pgconn.exec_(b"DISCARD ALL")
            record = store.note_data_time(URL, T, now=T + timedelta(minutes=1))
            assert record.reads == 2
    finally:
        drop_schema(schema)


def test_closing_the_store_leaves_the_pool_open() -> None:
    schema = fresh_schema()
    try:
        with ConnectionPool(POSTGRES_DSN, min_size=1, max_size=1, open=True) as pool:
            store = PostgresStore(pool=pool, schema=schema)
            store.close()
            with pytest.raises(RuntimeError, match="closed"):
                store.rows()
            with pool.connection(timeout=5) as conn:
                assert conn.execute("SELECT 1").fetchone() == (1,)
            with PostgresStore(pool=pool, schema=schema) as again:
                assert again.rows() == []
    finally:
        drop_schema(schema)


def test_a_lost_pooled_connection_is_replaced() -> None:
    schema = fresh_schema()
    try:
        with (
            ConnectionPool(POSTGRES_DSN, min_size=1, max_size=1, open=True) as pool,
            PostgresStore(pool=pool, schema=schema) as store,
        ):
            store.note_data_time(URL, T, now=T)
            with pool.connection() as conn:
                lost = conn.info.backend_pid
            with psycopg.connect(POSTGRES_DSN, autocommit=True) as admin:
                admin.execute("SELECT pg_terminate_backend(%s)", (lost,))
            record = store.note_data_time(URL, T, now=T + timedelta(minutes=1))
            assert record.reads == 2
            with pool.connection() as conn:
                assert conn.info.backend_pid != lost
    finally:
        drop_schema(schema)


def test_a_store_takes_a_conninfo_or_a_pool() -> None:
    with (
        ConnectionPool(POSTGRES_DSN, min_size=1, max_size=1, open=True) as pool,
        pytest.raises(ValueError, match="conninfo or a pool"),
    ):
        PostgresStore(POSTGRES_DSN, pool=pool)
    with pytest.raises(ValueError, match="conninfo or a pool"):
        PostgresStore()


def test_the_clock_check_can_be_turned_off() -> None:
    schema = fresh_schema()
    try:
        PostgresStore(
            POSTGRES_DSN,
            schema=schema,
            clock=FakeClock(datetime(2000, 1, 1, tzinfo=UTC)),
            max_clock_skew=None,
        ).close()
    finally:
        drop_schema(schema)
