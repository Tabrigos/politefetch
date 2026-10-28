"""SQLite store, the default: standard-library `sqlite3`.

Safe to share between processes on one machine:

- every critical operation is one `BEGIN IMMEDIATE` transaction, which takes
  the database write lock before reading, so two processes cannot both read
  "one request left" and both spend it;
- WAL journal, so readers do not block the writer;
- `synchronous=FULL`, so a promise that was committed survives a power cut,
  not only a process crash;
- a busy timeout, so a process that finds the lock taken waits for it instead
  of failing at once; if the wait runs out the operation raises and nothing
  is sent.

Times are stored as fixed-width UTC text (`2026-10-16T12:00:00.000000Z`),
which sorts like the times it represents. Opening the store creates what is
missing and runs the pending migrations, in the same transaction
(`politefetch.store._schema`, D-052).
"""

import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import TracebackType
from typing import Any, Self

from politefetch.clock import as_utc
from politefetch.errors import RefusalReason
from politefetch.store._schema import BASE_VERSION, VERSION_TABLE, pending
from politefetch.store.base import (
    ABANDONED,
    DataTimeRecord,
    Freeze,
    HostState,
    HostUpdate,
    Outcome,
    RequestRow,
    RowState,
    Unfreeze,
    UrlRecord,
    Validators,
    apply_close,
    apply_data_time,
    apply_late_close,
    apply_validators,
    byte_changes,
    check_admission,
    check_cautious,
    check_latest,
    check_threshold,
    check_unfreezer,
    is_late,
)

__all__ = ["SqliteStore"]

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

_WAL_PAUSE = 0.01
"""Seconds between two tries of switching a new file to WAL."""

# Schema version 1, frozen: a new store creates it and then runs every
# migration, as an old one does. A column or table added here and not as a
# migration would reach new stores only (tests/test_store_schema.py).
_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS hosts (
        host TEXT PRIMARY KEY,
        not_before TEXT,
        not_before_reason TEXT,
        failures INTEGER NOT NULL DEFAULT 0,
        throttled INTEGER NOT NULL DEFAULT 0,
        freeze_since TEXT,
        freeze_until TEXT,
        freeze_status INTEGER,
        freeze_note TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_counts (
        host TEXT NOT NULL,
        day TEXT NOT NULL,
        requests INTEGER NOT NULL,
        wire_bytes INTEGER NOT NULL DEFAULT 0,
        decoded_bytes INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (host, day)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        host TEXT NOT NULL,
        url TEXT NOT NULL,
        opened_at TEXT NOT NULL,
        state TEXT NOT NULL,
        closed_at TEXT,
        ok INTEGER,
        status INTEGER,
        failure TEXT,
        reserved_bytes INTEGER NOT NULL DEFAULT 0,
        wire_bytes INTEGER,
        decoded_bytes INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS data_times (
        url TEXT PRIMARY KEY,
        latest TEXT NOT NULL,
        reads INTEGER NOT NULL,
        updated_at TEXT NOT NULL,
        since TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS urls (
        url TEXT PRIMARY KEY,
        etag TEXT,
        last_modified TEXT,
        size INTEGER,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS unfreezes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        host TEXT NOT NULL,
        at TEXT NOT NULL,
        by_whom TEXT NOT NULL,
        note TEXT NOT NULL,
        freeze_since TEXT NOT NULL,
        freeze_until TEXT,
        freeze_status INTEGER,
        freeze_note TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS requests_by_state ON requests (state, opened_at)",
    "CREATE INDEX IF NOT EXISTS requests_by_host ON requests (host, state)",
    "CREATE INDEX IF NOT EXISTS requests_latest ON requests (host, id)",
)

# Columns added after a table first shipped: created on databases that lack
# them, so a file written by an earlier stage keeps working (no numbered
# migrations before the first release, D-002).
_ADDED_COLUMNS = (
    ("hosts", "throttled", "INTEGER NOT NULL DEFAULT 0"),
    ("daily_counts", "wire_bytes", "INTEGER NOT NULL DEFAULT 0"),
    ("daily_counts", "decoded_bytes", "INTEGER NOT NULL DEFAULT 0"),
    ("requests", "reserved_bytes", "INTEGER NOT NULL DEFAULT 0"),
    ("requests", "wire_bytes", "INTEGER"),
    ("requests", "decoded_bytes", "INTEGER"),
    ("data_times", "since", "TEXT"),
)

# The statements of each schema version after the first: `_MIGRATIONS[0]`
# leads to version 2. Only additions (D-052).
_MIGRATIONS: tuple[tuple[str, ...], ...] = ()

_ROW_COLUMNS = (
    "id, host, url, opened_at, state, closed_at, ok, status, failure,"
    " reserved_bytes, wire_bytes, decoded_bytes"
)


def _to_text(value: datetime) -> str:
    return value.strftime(_TIME_FORMAT)


def _from_text(value: str) -> datetime:
    return datetime.strptime(value, _TIME_FORMAT).replace(tzinfo=UTC)


def _maybe_time(value: str | None) -> datetime | None:
    return None if value is None else _from_text(value)


def _freeze(
    since: str | None, until: str | None, status: int | None, note: str | None
) -> Freeze | None:
    if since is None:
        return None
    return Freeze(
        since=_from_text(since),
        until=_maybe_time(until),
        status=status,
        note=note or "",
    )


def _row(record: tuple[Any, ...]) -> RequestRow:
    (row_id, host, url, opened_at, state, closed_at, ok, status, failure) = record[:9]
    reserved, wire, decoded = record[9:]
    outcome = None
    if ok is not None:
        outcome = Outcome(
            ok=bool(ok),
            status=status,
            failure=failure,
            wire_bytes=wire,
            decoded_bytes=decoded,
        )
    return RequestRow(
        id=row_id,
        host=host,
        url=url,
        opened_at=_from_text(opened_at),
        state=RowState(state),
        closed_at=_maybe_time(closed_at),
        outcome=outcome,
        reserved_bytes=reserved,
    )


def _switch_to_wal(conn: sqlite3.Connection, timeout: float) -> None:
    """Put the file in WAL mode. On a file not yet in WAL the switch needs a
    lock that SQLite does not wait for while another connection is switching
    too: it answers "busy" at once. Try again until the busy timeout ends."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL").fetchall()
            return
        except sqlite3.OperationalError as error:
            busy = error.sqlite_errorcode == sqlite3.SQLITE_BUSY
            if not busy or time.monotonic() >= deadline:
                raise
        time.sleep(_WAL_PAUSE)


class SqliteStore:
    """A `Store` in a SQLite file, shareable between processes."""

    def __init__(
        self, path: str | os.PathLike[str], *, busy_timeout: float = 30.0
    ) -> None:
        self.path = os.fspath(path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            self.path,
            timeout=busy_timeout,
            isolation_level=None,  # transactions are opened explicitly
            check_same_thread=False,  # access is serialised by self._lock
        )
        try:
            _switch_to_wal(self._conn, busy_timeout)
            self._conn.execute("PRAGMA synchronous=FULL")
            with self._transaction(immediate=True) as conn:
                for statement in _SCHEMA:
                    conn.execute(statement)
                for table, column, declaration in _ADDED_COLUMNS:
                    existing = {
                        r[1] for r in conn.execute(f"PRAGMA table_info({table})")
                    }
                    if column not in existing:
                        conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
                        )
                for version, statements in pending(
                    self._version(conn), _MIGRATIONS, f"the SQLite store {self.path}"
                ):
                    for statement in statements:
                        conn.execute(statement)
                    conn.execute("UPDATE schema_version SET version = ?", (version,))
        except BaseException:
            self._conn.close()
            raise

    # --- plumbing -----------------------------------------------------------

    @staticmethod
    def _version(conn: sqlite3.Connection) -> int:
        """The schema version, recorded as the first one if the database had
        none (a new one, or one written by 0.1)."""
        conn.execute(VERSION_TABLE)
        found = conn.execute("SELECT version FROM schema_version").fetchone()
        if found is None:
            conn.execute("INSERT INTO schema_version VALUES (?)", (BASE_VERSION,))
            return BASE_VERSION
        return int(found[0])

    @contextmanager
    def _transaction(self, *, immediate: bool) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._conn is None:
                raise RuntimeError("the store is closed")
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    @staticmethod
    def _state(conn: sqlite3.Connection, host: str, now: datetime) -> HostState:
        day = now.date()
        record = conn.execute(
            "SELECT not_before, not_before_reason, failures, throttled,"
            " freeze_since, freeze_until, freeze_status, freeze_note"
            " FROM hosts WHERE host = ?",
            (host,),
        ).fetchone()
        count, wire, decoded = conn.execute(
            "SELECT COALESCE(SUM(requests), 0), COALESCE(SUM(wire_bytes), 0),"
            " COALESCE(SUM(decoded_bytes), 0) FROM daily_counts"
            " WHERE host = ? AND day = ?",
            (host, day.isoformat()),
        ).fetchone()
        (in_flight,) = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE host = ? AND state = ?",
            (host, RowState.IN_FLIGHT.value),
        ).fetchone()
        state = HostState(
            host=host,
            day=day,
            requests_today=count,
            in_flight=in_flight,
            wire_bytes_today=wire,
            decoded_bytes_today=decoded,
        )
        if record is None:
            return state
        not_before, reason, failures, throttled = record[:4]
        freeze = _freeze(*record[4:])
        return HostState(
            host=host,
            day=day,
            requests_today=count,
            not_before=_maybe_time(not_before),
            not_before_reason=None if reason is None else RefusalReason(reason),
            failures=failures,
            freeze=freeze,
            in_flight=in_flight,
            throttled=throttled,
            wire_bytes_today=wire,
            decoded_bytes_today=decoded,
        )

    @staticmethod
    def _write_host(conn: sqlite3.Connection, state: HostState) -> None:
        freeze = state.freeze
        conn.execute(
            "INSERT INTO hosts (host, not_before, not_before_reason, failures,"
            " throttled, freeze_since, freeze_until, freeze_status, freeze_note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (host) DO UPDATE SET"
            " not_before = excluded.not_before,"
            " not_before_reason = excluded.not_before_reason,"
            " failures = excluded.failures,"
            " throttled = excluded.throttled,"
            " freeze_since = excluded.freeze_since,"
            " freeze_until = excluded.freeze_until,"
            " freeze_status = excluded.freeze_status,"
            " freeze_note = excluded.freeze_note",
            (
                state.host,
                None if state.not_before is None else _to_text(state.not_before),
                None
                if state.not_before_reason is None
                else state.not_before_reason.value,
                state.failures,
                state.throttled,
                None if freeze is None else _to_text(freeze.since),
                None
                if freeze is None or freeze.until is None
                else _to_text(freeze.until),
                None if freeze is None else freeze.status,
                None if freeze is None else freeze.note,
            ),
        )

    @staticmethod
    def _get_row(conn: sqlite3.Connection, row_id: int) -> RequestRow:
        record = conn.execute(
            f"SELECT {_ROW_COLUMNS} FROM requests WHERE id = ?", (row_id,)
        ).fetchone()
        if record is None:
            raise KeyError(row_id)
        return _row(record)

    # --- reads --------------------------------------------------------------

    def host_state(self, host: str, now: datetime) -> HostState:
        now = as_utc(now, "now")
        with self._transaction(immediate=False) as conn:
            return self._state(conn, host, now)

    def requests_on(self, host: str, day: date) -> int:
        with self._transaction(immediate=False) as conn:
            (count,) = conn.execute(
                "SELECT COALESCE(SUM(requests), 0) FROM daily_counts"
                " WHERE host = ? AND day = ?",
                (host, day.isoformat()),
            ).fetchone()
            return int(count)

    def row(self, row_id: int) -> RequestRow:
        with self._transaction(immediate=False) as conn:
            return self._get_row(conn, row_id)

    def rows(
        self,
        *,
        host: str | None = None,
        state: RowState | None = None,
        latest: int | None = None,
    ) -> list[RequestRow]:
        check_latest(latest)
        clauses: list[str] = []
        params: list[str | int] = []
        if host is not None:
            clauses.append("host = ?")
            params.append(host)
        if state is not None:
            clauses.append("state = ?")
            params.append(state.value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT {_ROW_COLUMNS} FROM requests{where} ORDER BY id"
        if latest is not None:
            query = f"{query} DESC LIMIT ?"
            params.append(latest)
        with self._transaction(immediate=False) as conn:
            records = conn.execute(query, params).fetchall()
        if latest is not None:
            records.reverse()
        return [_row(r) for r in records]

    # --- critical operations ------------------------------------------------

    def open_request(
        self,
        host: str,
        url: str,
        *,
        now: datetime,
        daily_cap: int,
        cautious: Callable[[HostState], datetime],
        byte_budget: int | None = None,
    ) -> RequestRow:
        now = as_utc(now, "now")
        with self._transaction(immediate=True) as conn:
            state = self._state(conn, host, now)
            record = self._url_record(conn, url)
            reserve = record.size if record is not None and record.size else 0
            refusal = check_admission(state, now, daily_cap, byte_budget, reserve)
            if refusal is not None:
                raise refusal
            promise = check_cautious(now, cautious(state))
            cursor = conn.execute(
                "INSERT INTO requests (host, url, opened_at, state, reserved_bytes)"
                " VALUES (?, ?, ?, ?, ?)",
                (host, url, _to_text(now), RowState.IN_FLIGHT.value, reserve),
            )
            conn.execute(
                "INSERT INTO daily_counts (host, day, requests, wire_bytes)"
                " VALUES (?, ?, 1, ?)"
                " ON CONFLICT (host, day) DO UPDATE SET requests = requests + 1,"
                " wire_bytes = wire_bytes + excluded.wire_bytes",
                (host, now.date().isoformat(), reserve),
            )
            self._write_host(
                conn,
                replace(
                    state, not_before=promise, not_before_reason=RefusalReason.BACKOFF
                ),
            )
            row_id = cursor.lastrowid
            assert row_id is not None
        return RequestRow(
            id=row_id,
            host=host,
            url=url,
            opened_at=now,
            state=RowState.IN_FLIGHT,
            reserved_bytes=reserve,
        )

    def close_request(
        self,
        row_id: int,
        *,
        now: datetime,
        outcome: Outcome,
        update: Callable[[HostState], HostUpdate],
        validators: Validators | None = None,
    ) -> RequestRow:
        now = as_utc(now, "now")
        with self._transaction(immediate=True) as conn:
            row = self._get_row(conn, row_id)
            late = is_late(row)
            if row.state is not RowState.IN_FLIGHT and not late:
                return row
            state = self._state(conn, row.host, now)
            if late:
                new_state = apply_late_close(state, outcome, update(state))
                record = None
            else:
                new_state = apply_close(state, outcome, update(state))
                record = apply_validators(
                    self._url_record(conn, row.url), row.url, outcome, validators, now
                )
            closed_state = row.state if late else RowState.CLOSED
            self._write_host(conn, new_state)
            conn.execute(
                "UPDATE requests SET state = ?, closed_at = ?, ok = ?, status = ?,"
                " failure = ?, wire_bytes = ?, decoded_bytes = ? WHERE id = ?",
                (
                    closed_state.value,
                    _to_text(now),
                    int(outcome.ok),
                    outcome.status,
                    outcome.failure,
                    outcome.wire_bytes,
                    outcome.decoded_bytes,
                    row_id,
                ),
            )
            wire, decoded = byte_changes(row, outcome, late=late)
            conn.execute(
                "UPDATE daily_counts SET wire_bytes = wire_bytes + ?,"
                " decoded_bytes = decoded_bytes + ? WHERE host = ? AND day = ?",
                (wire, decoded, row.host, row.opened_at.date().isoformat()),
            )
            if record is not None:
                conn.execute(
                    "INSERT INTO urls (url, etag, last_modified, size, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT (url) DO UPDATE SET etag = excluded.etag,"
                    " last_modified = excluded.last_modified, size = excluded.size,"
                    " updated_at = excluded.updated_at",
                    (
                        record.url,
                        record.validators.etag,
                        record.validators.last_modified,
                        record.size,
                        _to_text(record.updated_at),
                    ),
                )
        return replace(row, state=closed_state, closed_at=now, outcome=outcome)

    @staticmethod
    def _url_record(conn: sqlite3.Connection, url: str) -> UrlRecord | None:
        found = conn.execute(
            "SELECT etag, last_modified, size, updated_at FROM urls WHERE url = ?",
            (url,),
        ).fetchone()
        if found is None:
            return None
        etag, last_modified, size, updated_at = found
        return UrlRecord(
            url=url,
            validators=Validators(etag=etag, last_modified=last_modified),
            size=size,
            updated_at=_from_text(updated_at),
        )

    def url_record(self, url: str) -> UrlRecord | None:
        with self._transaction(immediate=False) as conn:
            return self._url_record(conn, url)

    @staticmethod
    def _data_time_record(conn: sqlite3.Connection, url: str) -> DataTimeRecord | None:
        found = conn.execute(
            "SELECT latest, reads, updated_at, since FROM data_times WHERE url = ?",
            (url,),
        ).fetchone()
        if found is None:
            return None
        latest, reads, updated_at, since = found
        # A row written before `since` existed: its last read is the latest
        # moment the stuck time can have started.
        return DataTimeRecord(
            url=url,
            latest=_from_text(latest),
            reads=reads,
            updated_at=_from_text(updated_at),
            since=_from_text(since if since is not None else updated_at),
        )

    def note_data_time(
        self, url: str, data_time: datetime, *, now: datetime
    ) -> DataTimeRecord:
        now = as_utc(now, "now")
        with self._transaction(immediate=True) as conn:
            record = apply_data_time(
                self._data_time_record(conn, url), url, data_time, now
            )
            conn.execute(
                "INSERT INTO data_times (url, latest, reads, updated_at, since)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (url) DO UPDATE SET latest = excluded.latest,"
                " reads = excluded.reads, updated_at = excluded.updated_at,"
                " since = excluded.since",
                (
                    url,
                    _to_text(record.latest),
                    record.reads,
                    _to_text(record.updated_at),
                    _to_text(record.since),
                ),
            )
        return record

    def data_time_record(self, url: str) -> DataTimeRecord | None:
        with self._transaction(immediate=False) as conn:
            return self._data_time_record(conn, url)

    def reconcile(self, *, now: datetime, older_than: timedelta) -> list[RequestRow]:
        now = as_utc(now, "now")
        check_threshold(older_than)
        cutoff = now - older_than
        with self._transaction(immediate=True) as conn:
            records = conn.execute(
                f"SELECT {_ROW_COLUMNS} FROM requests"
                " WHERE state = ? AND opened_at <= ? ORDER BY id",
                (RowState.IN_FLIGHT.value, _to_text(cutoff)),
            ).fetchall()
            reconciled: list[RequestRow] = []
            for record in records:
                row = _row(record)
                conn.execute(
                    "UPDATE requests SET state = ?, closed_at = ?, ok = 0,"
                    " status = NULL, failure = ? WHERE id = ?",
                    (RowState.RECONCILED.value, _to_text(now), ABANDONED, row.id),
                )
                conn.execute(
                    "INSERT INTO hosts (host, failures) VALUES (?, 1)"
                    " ON CONFLICT (host) DO UPDATE SET failures = failures + 1",
                    (row.host,),
                )
                reconciled.append(
                    replace(
                        row,
                        state=RowState.RECONCILED,
                        closed_at=now,
                        outcome=Outcome(ok=False, failure=ABANDONED),
                    )
                )
        return reconciled

    def unfreeze(
        self, host: str, *, now: datetime, by: str, note: str = ""
    ) -> Unfreeze | None:
        now = as_utc(now, "now")
        check_unfreezer(by)
        with self._transaction(immediate=True) as conn:
            record = conn.execute(
                "SELECT freeze_since, freeze_until, freeze_status, freeze_note"
                " FROM hosts WHERE host = ?",
                (host,),
            ).fetchone()
            freeze = None if record is None else _freeze(*record)
            if freeze is None:
                return None
            cursor = conn.execute(
                "INSERT INTO unfreezes (host, at, by_whom, note, freeze_since,"
                " freeze_until, freeze_status, freeze_note)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (host, _to_text(now), by, note, *record[:3], freeze.note),
            )
            conn.execute(
                "UPDATE hosts SET freeze_since = NULL, freeze_until = NULL,"
                " freeze_status = NULL, freeze_note = NULL WHERE host = ?",
                (host,),
            )
            trace_id = cursor.lastrowid
            assert trace_id is not None
        return Unfreeze(id=trace_id, host=host, at=now, by=by, note=note, freeze=freeze)

    def unfreezes(self, *, host: str | None = None) -> list[Unfreeze]:
        where, params = ("", ()) if host is None else (" WHERE host = ?", (host,))
        with self._transaction(immediate=False) as conn:
            records = conn.execute(
                "SELECT id, host, at, by_whom, note, freeze_since, freeze_until,"
                f" freeze_status, freeze_note FROM unfreezes{where} ORDER BY id",
                params,
            ).fetchall()
        traces = []
        for row_id, h, at, by, note, *frozen in records:
            freeze = _freeze(*frozen)
            assert freeze is not None
            traces.append(
                Unfreeze(
                    id=row_id,
                    host=h,
                    at=_from_text(at),
                    by=by,
                    note=note,
                    freeze=freeze,
                )
            )
        return traces

    # --- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
