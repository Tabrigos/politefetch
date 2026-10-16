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
which sorts like the times it represents. Tables are created with
`CREATE TABLE IF NOT EXISTS`; there are no numbered migrations before the
first release (D-002).
"""

import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import TracebackType
from typing import Any, Self

from politefetch.clock import as_utc
from politefetch.errors import RefusalReason
from politefetch.store.base import (
    ABANDONED,
    Freeze,
    HostState,
    HostUpdate,
    Outcome,
    RequestRow,
    RowState,
    Unfreeze,
    apply_close,
    check_admission,
    check_cautious,
    check_threshold,
    check_unfreezer,
)

__all__ = ["SqliteStore"]

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

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
        failure TEXT
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
)

# Columns added after a table first shipped: created on databases that lack
# them, so a file written by an earlier stage keeps working (no numbered
# migrations before the first release, D-002).
_ADDED_COLUMNS = (("hosts", "throttled", "INTEGER NOT NULL DEFAULT 0"),)

_ROW_COLUMNS = "id, host, url, opened_at, state, closed_at, ok, status, failure"


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
    row_id, host, url, opened_at, state, closed_at, ok, status, failure = record
    outcome = (
        None if ok is None else Outcome(ok=bool(ok), status=status, failure=failure)
    )
    return RequestRow(
        id=row_id,
        host=host,
        url=url,
        opened_at=_from_text(opened_at),
        state=RowState(state),
        closed_at=_maybe_time(closed_at),
        outcome=outcome,
    )


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
            self._conn.execute("PRAGMA journal_mode=WAL").fetchall()
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
        except BaseException:
            self._conn.close()
            raise

    # --- plumbing -----------------------------------------------------------

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
        (count,) = conn.execute(
            "SELECT COALESCE(SUM(requests), 0) FROM daily_counts"
            " WHERE host = ? AND day = ?",
            (host, day.isoformat()),
        ).fetchone()
        (in_flight,) = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE host = ? AND state = ?",
            (host, RowState.IN_FLIGHT.value),
        ).fetchone()
        state = HostState(host=host, day=day, requests_today=count, in_flight=in_flight)
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
        self, *, host: str | None = None, state: RowState | None = None
    ) -> list[RequestRow]:
        clauses: list[str] = []
        params: list[str] = []
        if host is not None:
            clauses.append("host = ?")
            params.append(host)
        if state is not None:
            clauses.append("state = ?")
            params.append(state.value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._transaction(immediate=False) as conn:
            records = conn.execute(
                f"SELECT {_ROW_COLUMNS} FROM requests{where} ORDER BY id", params
            ).fetchall()
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
    ) -> RequestRow:
        now = as_utc(now, "now")
        with self._transaction(immediate=True) as conn:
            state = self._state(conn, host, now)
            refusal = check_admission(state, now, daily_cap)
            if refusal is not None:
                raise refusal
            promise = check_cautious(now, cautious(state))
            cursor = conn.execute(
                "INSERT INTO requests (host, url, opened_at, state)"
                " VALUES (?, ?, ?, ?)",
                (host, url, _to_text(now), RowState.IN_FLIGHT.value),
            )
            conn.execute(
                "INSERT INTO daily_counts (host, day, requests) VALUES (?, ?, 1)"
                " ON CONFLICT (host, day) DO UPDATE SET requests = requests + 1",
                (host, now.date().isoformat()),
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
            id=row_id, host=host, url=url, opened_at=now, state=RowState.IN_FLIGHT
        )

    def close_request(
        self,
        row_id: int,
        *,
        now: datetime,
        outcome: Outcome,
        update: Callable[[HostState], HostUpdate],
    ) -> RequestRow:
        now = as_utc(now, "now")
        with self._transaction(immediate=True) as conn:
            row = self._get_row(conn, row_id)
            if row.state is not RowState.IN_FLIGHT:
                return row
            state = self._state(conn, row.host, now)
            new_state = apply_close(state, outcome, update(state))
            self._write_host(conn, new_state)
            conn.execute(
                "UPDATE requests SET state = ?, closed_at = ?, ok = ?, status = ?,"
                " failure = ? WHERE id = ?",
                (
                    RowState.CLOSED.value,
                    _to_text(now),
                    int(outcome.ok),
                    outcome.status,
                    outcome.failure,
                    row_id,
                ),
            )
        return RequestRow(
            id=row.id,
            host=row.host,
            url=row.url,
            opened_at=row.opened_at,
            state=RowState.CLOSED,
            closed_at=now,
            outcome=outcome,
        )

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
                    RequestRow(
                        id=row.id,
                        host=row.host,
                        url=row.url,
                        opened_at=row.opened_at,
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
