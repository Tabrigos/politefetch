"""SQLite-specific behaviour, beyond the shared contract."""

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from politefetch.store import SqliteStore

T = datetime(2026, 10, 16, 12, tzinfo=UTC)


def test_settings_for_safety_across_processes(tmp_path: Path) -> None:
    with SqliteStore(tmp_path / "s.db") as store:
        assert store.path == str(tmp_path / "s.db")
    conn = sqlite3.connect(tmp_path / "s.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    finally:
        conn.close()


def open_together(path: Path, start: threading.Barrier, errors: list[Any]) -> None:
    start.wait()
    try:
        SqliteStore(path).close()
    except BaseException as error:
        errors.append(error)


def test_processes_opening_a_new_file_at_once_all_open(tmp_path: Path) -> None:
    # Switching a new file to WAL takes a lock that SQLite does not wait for:
    # without a retry, all but one of the first openers failed.
    for round_ in range(10):
        path = tmp_path / f"{round_}.db"
        start = threading.Barrier(4)
        errors: list[Any] = []
        threads = [
            threading.Thread(target=open_together, args=(path, start, errors))
            for _ in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        conn = sqlite3.connect(path)
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        finally:
            conn.close()


def test_a_lock_that_stays_taken_still_fails_the_open(tmp_path: Path) -> None:
    path = tmp_path / "held.db"
    holder = sqlite3.connect(path, isolation_level=None)
    try:
        holder.execute("CREATE TABLE t (x)")
        holder.execute("BEGIN EXCLUSIVE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            SqliteStore(path, busy_timeout=0.2)
    finally:
        holder.close()


def test_a_database_from_an_earlier_stage_gains_new_columns(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    try:
        # The hosts table as stage T1 created it, without `throttled`.
        conn.execute(
            "CREATE TABLE hosts (host TEXT PRIMARY KEY, not_before TEXT,"
            " not_before_reason TEXT, failures INTEGER NOT NULL DEFAULT 0,"
            " freeze_since TEXT, freeze_until TEXT, freeze_status INTEGER,"
            " freeze_note TEXT)"
        )
        conn.execute("INSERT INTO hosts (host, failures) VALUES ('example.org', 3)")
        conn.commit()
    finally:
        conn.close()
    with SqliteStore(path) as store:
        state = store.host_state("example.org", T)
        assert state.failures == 3
        assert state.throttled == 0
    SqliteStore(path).close()  # opening again is harmless


def test_a_database_from_t1_gains_the_byte_columns(tmp_path: Path) -> None:
    path = tmp_path / "t1.db"
    conn = sqlite3.connect(path)
    try:
        # daily_counts and requests as stage T1 created them, without bytes.
        conn.execute(
            "CREATE TABLE daily_counts (host TEXT NOT NULL, day TEXT NOT NULL,"
            " requests INTEGER NOT NULL, PRIMARY KEY (host, day))"
        )
        conn.execute(
            "CREATE TABLE requests (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " host TEXT NOT NULL, url TEXT NOT NULL, opened_at TEXT NOT NULL,"
            " state TEXT NOT NULL, closed_at TEXT, ok INTEGER, status INTEGER,"
            " failure TEXT)"
        )
        conn.execute("INSERT INTO daily_counts VALUES ('example.org', '2026-10-16', 2)")
        conn.execute(
            "INSERT INTO requests (host, url, opened_at, state, closed_at, ok, status)"
            " VALUES ('example.org', 'https://example.org/x',"
            " '2026-10-16T11:00:00.000000Z', 'closed',"
            " '2026-10-16T11:00:01.000000Z', 1, 200)"
        )
        conn.commit()
    finally:
        conn.close()
    with SqliteStore(path) as store:
        state = store.host_state("example.org", T)
        assert (state.requests_today, state.wire_bytes_today) == (2, 0)
        row = store.rows()[0]
        assert row.reserved_bytes == 0
        assert row.outcome is not None
        assert row.outcome.wire_bytes is None


def test_a_database_from_t3_gains_since(tmp_path: Path) -> None:
    path = tmp_path / "t3.db"
    conn = sqlite3.connect(path)
    try:
        # data_times as stage T3 created it, without `since`.
        conn.execute(
            "CREATE TABLE data_times (url TEXT PRIMARY KEY, latest TEXT NOT NULL,"
            " reads INTEGER NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO data_times VALUES ('https://example.org/a',"
            " '2026-10-16T10:00:00.000000Z', 3, '2026-10-16T11:00:00.000000Z')"
        )
        conn.commit()
    finally:
        conn.close()
    with SqliteStore(path) as store:
        record = store.data_time_record("https://example.org/a")
        assert record is not None
        # Unknown before: the last read is the latest moment it can be.
        assert record.since == datetime(2026, 10, 16, 11, tzinfo=UTC)
        assert record.reads == 3
        later = store.note_data_time("https://example.org/a", record.latest, now=T)
        assert (later.reads, later.since) == (4, record.since)
