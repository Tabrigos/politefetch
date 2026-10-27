"""Postgres store, behind the `postgres` extra (psycopg 3).

    store = PostgresStore("host=db dbname=ingest user=ingest", schema="politefetch")

Safe to share between processes and machines:

- every critical operation is one transaction that first takes a
  transaction-level advisory lock for the schema, so operations that change
  state run one at a time, as with SQLite's `BEGIN IMMEDIATE`; reads run in
  `REPEATABLE READ` snapshots and do not wait;
- `lock_timeout` bounds the wait for that lock: if it runs out the
  operation raises (`psycopg.errors.LockNotAvailable`) and nothing is sent;
- a process that dies inside a transaction loses its connection, and
  Postgres rolls the transaction back and releases the lock;
- nothing is set for the session: every transaction sets its own schema,
  time zone and lock timeout (`SET LOCAL`), and statements are not
  prepared, so a pooler in transaction mode (pgbouncer) that resets the
  server connection between transactions changes nothing;
- a connection found lost when a transaction starts is opened again, and
  the transaction runs on the new one; one lost in the middle raises, and
  the next operation opens a new one;
- on opening, the machine's clock is compared with the server's: every
  machine sharing the store writes promises with its own clock, and one
  that runs ahead would ask the source too early. Beyond `max_clock_skew`
  the store refuses to open.

Tables live in their own schema (default `politefetch`), created with
`CREATE ... IF NOT EXISTS`. The rules are those of `politefetch.store.base`,
shared with the other stores.
"""

import hashlib
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager, suppress
from dataclasses import replace
from datetime import date, datetime, timedelta
from types import TracebackType
from typing import Any, Self

import psycopg
from psycopg import sql

from politefetch.clock import Clock, SystemClock, as_utc
from politefetch.errors import RefusalReason
from politefetch.policy import parse_duration
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

__all__ = ["PostgresStore"]

_SCHEMA_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")

_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS hosts (
        host text PRIMARY KEY,
        not_before timestamptz,
        not_before_reason text,
        failures bigint NOT NULL DEFAULT 0,
        throttled bigint NOT NULL DEFAULT 0,
        freeze_since timestamptz,
        freeze_until timestamptz,
        freeze_status integer,
        freeze_note text
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_counts (
        host text NOT NULL,
        day date NOT NULL,
        requests bigint NOT NULL,
        wire_bytes bigint NOT NULL DEFAULT 0,
        decoded_bytes bigint NOT NULL DEFAULT 0,
        PRIMARY KEY (host, day)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS requests (
        id bigserial PRIMARY KEY,
        host text NOT NULL,
        url text NOT NULL,
        opened_at timestamptz NOT NULL,
        state text NOT NULL,
        closed_at timestamptz,
        ok boolean,
        status integer,
        failure text,
        reserved_bytes bigint NOT NULL DEFAULT 0,
        wire_bytes bigint,
        decoded_bytes bigint
    )
    """,
    "CREATE INDEX IF NOT EXISTS requests_by_state ON requests (state, opened_at)",
    "CREATE INDEX IF NOT EXISTS requests_by_host ON requests (host, state)",
    "CREATE INDEX IF NOT EXISTS requests_latest ON requests (host, id)",
    """
    CREATE TABLE IF NOT EXISTS unfreezes (
        id bigserial PRIMARY KEY,
        host text NOT NULL,
        at timestamptz NOT NULL,
        by_whom text NOT NULL,
        note text NOT NULL,
        freeze_since timestamptz NOT NULL,
        freeze_until timestamptz,
        freeze_status integer,
        freeze_note text NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS urls (
        url text PRIMARY KEY,
        etag text,
        last_modified text,
        size bigint,
        updated_at timestamptz NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS data_times (
        url text PRIMARY KEY,
        latest timestamptz NOT NULL,
        reads bigint NOT NULL,
        updated_at timestamptz NOT NULL,
        since timestamptz
    )
    """,
    # Added after the table first shipped (no numbered migrations before the
    # first release, D-002).
    "ALTER TABLE data_times ADD COLUMN IF NOT EXISTS since timestamptz",
)

_ROW_COLUMNS = (
    "id, host, url, opened_at, state, closed_at, ok, status, failure,"
    " reserved_bytes, wire_bytes, decoded_bytes"
)


def _utc(value: datetime | None) -> datetime | None:
    return None if value is None else as_utc(value)


def _freeze(
    since: datetime | None, until: datetime | None, status: int | None, note: str | None
) -> Freeze | None:
    if since is None:
        return None
    return Freeze(
        since=as_utc(since), until=_utc(until), status=status, note=note or ""
    )


def _row(record: tuple[Any, ...]) -> RequestRow:
    row_id, host, url, opened_at, state, closed_at, ok, status, failure = record[:9]
    reserved, wire, decoded = record[9:]
    outcome = None
    if ok is not None:
        outcome = Outcome(
            ok=ok,
            status=status,
            failure=failure,
            wire_bytes=wire,
            decoded_bytes=decoded,
        )
    return RequestRow(
        id=row_id,
        host=host,
        url=url,
        opened_at=as_utc(opened_at),
        state=RowState(state),
        closed_at=_utc(closed_at),
        outcome=outcome,
        reserved_bytes=reserved,
    )


_Conn = psycopg.Connection[tuple[Any, ...]]


def _lock_key(schema: str) -> int:
    digest = hashlib.sha256(f"politefetch:{schema}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class PostgresStore:
    """A `Store` in a Postgres schema, shareable between processes."""

    def __init__(
        self,
        conninfo: str,
        *,
        schema: str = "politefetch",
        lock_timeout: float = 30.0,
        clock: Clock | None = None,
        max_clock_skew: timedelta | str | None = "1m",
    ) -> None:
        """`clock` is the machine's (by default the system clock), compared
        once with the server's; `max_clock_skew=None` skips the check."""
        if not _SCHEMA_NAME.fullmatch(schema):
            raise ValueError(f"schema must be a lowercase identifier, got {schema!r}")
        if not lock_timeout > 0:
            raise ValueError("lock_timeout must be positive")
        skew = None if max_clock_skew is None else parse_duration(max_clock_skew)
        self.schema = schema
        self._conninfo = conninfo
        self._lock_key = _lock_key(schema)
        self._lock_timeout = f"{int(lock_timeout * 1000)}ms"
        self._local = (
            sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)),
            sql.SQL("SET LOCAL TIME ZONE 'UTC'"),
        )
        self._lock = threading.RLock()
        self._conn: _Conn | None = self._connect()
        try:
            if skew is not None:
                self._check_clock(clock if clock is not None else SystemClock(), skew)
            with self._write() as conn:
                conn.execute(
                    sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                        sql.Identifier(schema)
                    )
                )
                for statement in _TABLES:
                    conn.execute(statement.encode())
        except BaseException:
            self._conn.close()
            raise

    # --- plumbing -----------------------------------------------------------

    def _connect(self) -> _Conn:
        # No prepared statements: a pooler may hand the next transaction a
        # server connection that never saw them.
        return psycopg.connect(self._conninfo, autocommit=True, prepare_threshold=None)

    def _check_clock(self, clock: Clock, max_skew: timedelta) -> None:
        found = self._connection().execute("SELECT now()").fetchone()
        assert found is not None
        skew = clock.now() - found[0]
        if abs(skew) > max_skew:
            ahead = "ahead of" if skew > timedelta(0) else "behind"
            raise ValueError(
                f"this machine's clock is {abs(skew)} {ahead} the Postgres server's,"
                f" over max_clock_skew {max_skew}: machines sharing a store must"
                " agree on the time, or one asks the source before its promise"
                " ends. Fix the clock (NTP), or pass max_clock_skew=None"
            )

    def _connection(self) -> _Conn:
        if self._conn is None:
            raise RuntimeError("the store is closed")
        if self._conn.closed or self._conn.broken:
            self._conn = self._connect()
        return self._conn

    @contextmanager
    def _transaction(self, start: Callable[[_Conn], None]) -> Iterator[_Conn]:
        """A transaction with the store's own settings, begun by `start`. A
        connection lost before anything ran is replaced, once."""
        with self._lock:
            try:
                conn, transaction = self._begin(start)
            except psycopg.OperationalError:
                if self._conn is None or not self._conn.broken:
                    raise
                conn, transaction = self._begin(start)
            with ExitStack() as stack:
                stack.push(transaction)
                yield conn

    def _begin(self, start: Callable[[_Conn], None]) -> tuple[_Conn, Any]:
        conn = self._connection()
        transaction = conn.transaction()
        transaction.__enter__()
        try:
            start(conn)
            for statement in self._local:
                conn.execute(statement)
        except BaseException as error:
            # Rolling back a lost connection fails too: the first error counts.
            with suppress(psycopg.Error):
                transaction.__exit__(type(error), error, error.__traceback__)
            raise
        return conn, transaction

    def _write(self) -> AbstractContextManager[_Conn]:
        """A transaction that holds the schema's lock: one writer at a time."""

        def start(conn: _Conn) -> None:
            conn.execute(
                sql.SQL("SET LOCAL lock_timeout = {}").format(
                    sql.Literal(self._lock_timeout)
                )
            )
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (self._lock_key,))

        return self._transaction(start)

    def _read(self) -> AbstractContextManager[_Conn]:
        """A consistent snapshot, without waiting for writers."""

        def start(conn: _Conn) -> None:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")

        return self._transaction(start)

    @staticmethod
    def _state(
        conn: psycopg.Connection[tuple[Any, ...]], host: str, now: datetime
    ) -> HostState:
        day = now.date()
        found = conn.execute(
            "SELECT not_before, not_before_reason, failures, throttled,"
            " freeze_since, freeze_until, freeze_status, freeze_note"
            " FROM hosts WHERE host = %s",
            (host,),
        ).fetchone()
        totals = conn.execute(
            "SELECT COALESCE(SUM(requests), 0), COALESCE(SUM(wire_bytes), 0),"
            " COALESCE(SUM(decoded_bytes), 0) FROM daily_counts"
            " WHERE host = %s AND day = %s",
            (host, day),
        ).fetchone()
        assert totals is not None
        count, wire, decoded = (int(v) for v in totals)
        in_flight_row = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE host = %s AND state = %s",
            (host, RowState.IN_FLIGHT.value),
        ).fetchone()
        assert in_flight_row is not None
        state = HostState(
            host=host,
            day=day,
            requests_today=count,
            in_flight=int(in_flight_row[0]),
            wire_bytes_today=wire,
            decoded_bytes_today=decoded,
        )
        if found is None:
            return state
        not_before, reason, failures, throttled = found[:4]
        return replace(
            state,
            not_before=_utc(not_before),
            not_before_reason=None if reason is None else RefusalReason(reason),
            failures=int(failures),
            throttled=int(throttled),
            freeze=_freeze(*found[4:]),
        )

    @staticmethod
    def _write_host(
        conn: psycopg.Connection[tuple[Any, ...]], state: HostState
    ) -> None:
        freeze = state.freeze
        conn.execute(
            "INSERT INTO hosts (host, not_before, not_before_reason, failures,"
            " throttled, freeze_since, freeze_until, freeze_status, freeze_note)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
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
                state.not_before,
                None
                if state.not_before_reason is None
                else state.not_before_reason.value,
                state.failures,
                state.throttled,
                None if freeze is None else freeze.since,
                None if freeze is None else freeze.until,
                None if freeze is None else freeze.status,
                None if freeze is None else freeze.note,
            ),
        )

    @staticmethod
    def _get_row(conn: psycopg.Connection[tuple[Any, ...]], row_id: int) -> RequestRow:
        found = conn.execute(
            f"SELECT {_ROW_COLUMNS} FROM requests WHERE id = %s".encode(), (row_id,)
        ).fetchone()
        if found is None:
            raise KeyError(row_id)
        return _row(found)

    @staticmethod
    def _url_record(
        conn: psycopg.Connection[tuple[Any, ...]], url: str
    ) -> UrlRecord | None:
        found = conn.execute(
            "SELECT etag, last_modified, size, updated_at FROM urls WHERE url = %s",
            (url,),
        ).fetchone()
        if found is None:
            return None
        etag, last_modified, size, updated_at = found
        return UrlRecord(
            url=url,
            validators=Validators(etag=etag, last_modified=last_modified),
            size=None if size is None else int(size),
            updated_at=as_utc(updated_at),
        )

    @staticmethod
    def _data_time_record(
        conn: psycopg.Connection[tuple[Any, ...]], url: str
    ) -> DataTimeRecord | None:
        found = conn.execute(
            "SELECT latest, reads, updated_at, since FROM data_times WHERE url = %s",
            (url,),
        ).fetchone()
        if found is None:
            return None
        latest, reads, updated_at, since = found
        return DataTimeRecord(
            url=url,
            latest=as_utc(latest),
            reads=int(reads),
            updated_at=as_utc(updated_at),
            since=as_utc(since if since is not None else updated_at),
        )

    # --- reads --------------------------------------------------------------

    def host_state(self, host: str, now: datetime) -> HostState:
        now = as_utc(now, "now")
        with self._read() as conn:
            return self._state(conn, host, now)

    def requests_on(self, host: str, day: date) -> int:
        with self._read() as conn:
            found = conn.execute(
                "SELECT COALESCE(SUM(requests), 0) FROM daily_counts"
                " WHERE host = %s AND day = %s",
                (host, day),
            ).fetchone()
        assert found is not None
        return int(found[0])

    def row(self, row_id: int) -> RequestRow:
        with self._read() as conn:
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
            clauses.append("host = %s")
            params.append(host)
        if state is not None:
            clauses.append("state = %s")
            params.append(state.value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT {_ROW_COLUMNS} FROM requests{where} ORDER BY id"
        if latest is not None:
            query = f"{query} DESC LIMIT %s"
            params.append(latest)
        with self._read() as conn:
            found = conn.execute(query.encode(), params).fetchall()
        if latest is not None:
            found.reverse()
        return [_row(r) for r in found]

    def url_record(self, url: str) -> UrlRecord | None:
        with self._read() as conn:
            return self._url_record(conn, url)

    def data_time_record(self, url: str) -> DataTimeRecord | None:
        with self._read() as conn:
            return self._data_time_record(conn, url)

    def unfreezes(self, *, host: str | None = None) -> list[Unfreeze]:
        where, params = ("", ()) if host is None else (" WHERE host = %s", (host,))
        with self._read() as conn:
            found = conn.execute(
                (
                    "SELECT id, host, at, by_whom, note, freeze_since, freeze_until,"
                    f" freeze_status, freeze_note FROM unfreezes{where} ORDER BY id"
                ).encode(),
                params,
            ).fetchall()
        traces = []
        for trace_id, trace_host, at, by, note, *frozen in found:
            freeze = _freeze(*frozen)
            assert freeze is not None
            traces.append(
                Unfreeze(
                    id=trace_id,
                    host=trace_host,
                    at=as_utc(at),
                    by=by,
                    note=note,
                    freeze=freeze,
                )
            )
        return traces

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
        abandon_before: datetime | None = None,
    ) -> RequestRow:
        now = as_utc(now, "now")
        with self._write() as conn:
            if abandon_before is not None:
                cutoff = as_utc(abandon_before, "abandon_before")
                self._abandon(conn, now, cutoff, host)
            state = self._state(conn, host, now)
            record = self._url_record(conn, url)
            reserve = record.size if record is not None and record.size else 0
            refusal = check_admission(state, now, daily_cap, byte_budget, reserve)
            if refusal is None:
                return self._admit(conn, host, url, now, state, reserve, cautious)
        # Raised after the commit: the abandoned rows stay closed.
        raise refusal

    def _admit(
        self,
        conn: _Conn,
        host: str,
        url: str,
        now: datetime,
        state: HostState,
        reserve: int,
        cautious: Callable[[HostState], datetime],
    ) -> RequestRow:
        """Open the row of an admitted request, inside the caller's
        transaction."""
        promise = check_cautious(now, cautious(state))
        inserted = conn.execute(
            "INSERT INTO requests (host, url, opened_at, state, reserved_bytes)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (host, url, now, RowState.IN_FLIGHT.value, reserve),
        ).fetchone()
        assert inserted is not None
        conn.execute(
            "INSERT INTO daily_counts (host, day, requests, wire_bytes)"
            " VALUES (%s, %s, 1, %s)"
            " ON CONFLICT (host, day) DO UPDATE SET"
            " requests = daily_counts.requests + 1,"
            " wire_bytes = daily_counts.wire_bytes + excluded.wire_bytes",
            (host, now.date(), reserve),
        )
        self._write_host(
            conn,
            replace(state, not_before=promise, not_before_reason=RefusalReason.BACKOFF),
        )
        return RequestRow(
            id=int(inserted[0]),
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
        with self._write() as conn:
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
                "UPDATE requests SET state = %s, closed_at = %s, ok = %s, status = %s,"
                " failure = %s, wire_bytes = %s, decoded_bytes = %s WHERE id = %s",
                (
                    closed_state.value,
                    now,
                    outcome.ok,
                    outcome.status,
                    outcome.failure,
                    outcome.wire_bytes,
                    outcome.decoded_bytes,
                    row_id,
                ),
            )
            wire, decoded = byte_changes(row, outcome, late=late)
            conn.execute(
                "UPDATE daily_counts SET wire_bytes = wire_bytes + %s,"
                " decoded_bytes = decoded_bytes + %s WHERE host = %s AND day = %s",
                (wire, decoded, row.host, row.opened_at.date()),
            )
            if record is not None:
                conn.execute(
                    "INSERT INTO urls (url, etag, last_modified, size, updated_at)"
                    " VALUES (%s, %s, %s, %s, %s)"
                    " ON CONFLICT (url) DO UPDATE SET etag = excluded.etag,"
                    " last_modified = excluded.last_modified, size = excluded.size,"
                    " updated_at = excluded.updated_at",
                    (
                        record.url,
                        record.validators.etag,
                        record.validators.last_modified,
                        record.size,
                        record.updated_at,
                    ),
                )
        return replace(row, state=closed_state, closed_at=now, outcome=outcome)

    def reconcile(self, *, now: datetime, older_than: timedelta) -> list[RequestRow]:
        now = as_utc(now, "now")
        check_threshold(older_than)
        with self._write() as conn:
            return self._abandon(conn, now, now - older_than)

    @staticmethod
    def _abandon(
        conn: _Conn, now: datetime, cutoff: datetime, host: str | None = None
    ) -> list[RequestRow]:
        """Close as abandoned the rows in flight since `cutoff` or earlier, of
        `host` or of every host, inside the caller's transaction."""
        abandoned = Outcome(ok=False, failure=ABANDONED)
        if host is None:
            found = conn.execute(
                f"SELECT {_ROW_COLUMNS} FROM requests"
                " WHERE state = %s AND opened_at <= %s ORDER BY id".encode(),
                (RowState.IN_FLIGHT.value, cutoff),
            ).fetchall()
        else:
            found = conn.execute(
                f"SELECT {_ROW_COLUMNS} FROM requests"
                " WHERE state = %s AND opened_at <= %s AND host = %s"
                " ORDER BY id".encode(),
                (RowState.IN_FLIGHT.value, cutoff, host),
            ).fetchall()
        reconciled: list[RequestRow] = []
        for record in found:
            row = _row(record)
            conn.execute(
                "UPDATE requests SET state = %s, closed_at = %s, ok = false,"
                " status = NULL, failure = %s WHERE id = %s",
                (RowState.RECONCILED.value, now, ABANDONED, row.id),
            )
            conn.execute(
                "INSERT INTO hosts (host, failures) VALUES (%s, 1)"
                " ON CONFLICT (host) DO UPDATE SET failures = hosts.failures + 1",
                (row.host,),
            )
            reconciled.append(
                replace(
                    row, state=RowState.RECONCILED, closed_at=now, outcome=abandoned
                )
            )
        return reconciled

    def note_data_time(
        self, url: str, data_time: datetime, *, now: datetime
    ) -> DataTimeRecord:
        now = as_utc(now, "now")
        with self._write() as conn:
            record = apply_data_time(
                self._data_time_record(conn, url), url, data_time, now
            )
            conn.execute(
                "INSERT INTO data_times (url, latest, reads, updated_at, since)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (url) DO UPDATE SET latest = excluded.latest,"
                " reads = excluded.reads, updated_at = excluded.updated_at,"
                " since = excluded.since",
                (url, record.latest, record.reads, record.updated_at, record.since),
            )
        return record

    def unfreeze(
        self, host: str, *, now: datetime, by: str, note: str = ""
    ) -> Unfreeze | None:
        now = as_utc(now, "now")
        check_unfreezer(by)
        with self._write() as conn:
            found = conn.execute(
                "SELECT freeze_since, freeze_until, freeze_status, freeze_note"
                " FROM hosts WHERE host = %s",
                (host,),
            ).fetchone()
            freeze = None if found is None else _freeze(*found)
            if freeze is None:
                return None
            inserted = conn.execute(
                "INSERT INTO unfreezes (host, at, by_whom, note, freeze_since,"
                " freeze_until, freeze_status, freeze_note)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    host,
                    now,
                    by,
                    note,
                    freeze.since,
                    freeze.until,
                    freeze.status,
                    freeze.note,
                ),
            ).fetchone()
            assert inserted is not None
            conn.execute(
                "UPDATE hosts SET freeze_since = NULL, freeze_until = NULL,"
                " freeze_status = NULL, freeze_note = NULL WHERE host = %s",
                (host,),
            )
        return Unfreeze(
            id=int(inserted[0]), host=host, at=now, by=by, note=note, freeze=freeze
        )

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
