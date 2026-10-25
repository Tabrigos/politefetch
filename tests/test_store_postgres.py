"""Postgres-specific behaviour, beyond the shared contract: a store that
keeps working when the session under it is reset or lost, and that refuses a
machine clock far from the server's.

A connection pooler in transaction mode (pgbouncer) hands each transaction a
server connection that may have been reset in between (`DISCARD ALL`): the
store must not rely on anything set for the session.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from politefetch.clock import FakeClock
from politefetch.store.postgres import PostgresStore

from .stores import POSTGRES_DSN, Harness, drop_schema, fresh_schema, needs_postgres

pytestmark = needs_postgres

T = datetime(2026, 10, 16, 12, tzinfo=UTC)
URL = "https://example.org/data.json"


@pytest.fixture
def store() -> Iterator[PostgresStore]:
    harness = Harness("postgres")
    assert isinstance(harness.store, PostgresStore)
    try:
        yield harness.store
    finally:
        harness.close()


def session(store: PostgresStore) -> psycopg.Connection[tuple[object, ...]]:
    connection = store._conn
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
