"""In-memory store, for tests and single-process tools.

Its state lives in a `MemoryData` object. Passing the same object to a new
`MemoryStore` simulates a restart over persistent state; a real process
restart loses it, so production code uses `SqliteStore`. Operations are
atomic between threads of one process, not between processes.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from types import TracebackType
from typing import Self

from politefetch.clock import as_utc
from politefetch.errors import RefusalReason
from politefetch.store.base import (
    ABANDONED,
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

__all__ = ["MemoryData", "MemoryStore"]


@dataclass
class MemoryData:
    """The persistent part of a `MemoryStore`."""

    hosts: dict[str, HostState] = field(default_factory=dict)
    counts: dict[tuple[str, date], int] = field(default_factory=dict)
    rows: dict[int, RequestRow] = field(default_factory=dict)
    next_id: int = 1
    unfreezes: list[Unfreeze] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)


class MemoryStore:
    """A `Store` kept in memory."""

    def __init__(self, data: MemoryData | None = None) -> None:
        self.data = data if data is not None else MemoryData()

    # --- reads --------------------------------------------------------------

    def _state(self, host: str, now: datetime) -> HostState:
        day = now.date()
        stored = self.data.hosts.get(host)
        base = stored if stored is not None else HostState(host=host, day=day)
        in_flight = sum(
            1
            for r in self.data.rows.values()
            if r.host == host and r.state is RowState.IN_FLIGHT
        )
        return replace(
            base,
            day=day,
            requests_today=self.data.counts.get((host, day), 0),
            in_flight=in_flight,
        )

    def host_state(self, host: str, now: datetime) -> HostState:
        now = as_utc(now, "now")
        with self.data.lock:
            return self._state(host, now)

    def requests_on(self, host: str, day: date) -> int:
        with self.data.lock:
            return self.data.counts.get((host, day), 0)

    def row(self, row_id: int) -> RequestRow:
        with self.data.lock:
            return self.data.rows[row_id]

    def rows(
        self, *, host: str | None = None, state: RowState | None = None
    ) -> list[RequestRow]:
        with self.data.lock:
            return [
                r
                for _, r in sorted(self.data.rows.items())
                if (host is None or r.host == host)
                and (state is None or r.state is state)
            ]

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
        with self.data.lock:
            state = self._state(host, now)
            refusal = check_admission(state, now, daily_cap)
            if refusal is not None:
                raise refusal
            promise = check_cautious(now, cautious(state))
            # Nothing has been written yet: everything below cannot fail.
            row = RequestRow(
                id=self.data.next_id,
                host=host,
                url=url,
                opened_at=now,
                state=RowState.IN_FLIGHT,
            )
            self.data.next_id += 1
            self.data.rows[row.id] = row
            key = (host, now.date())
            self.data.counts[key] = self.data.counts.get(key, 0) + 1
            self.data.hosts[host] = replace(
                state, not_before=promise, not_before_reason=RefusalReason.BACKOFF
            )
            return row

    def close_request(
        self,
        row_id: int,
        *,
        now: datetime,
        outcome: Outcome,
        update: Callable[[HostState], HostUpdate],
    ) -> RequestRow:
        now = as_utc(now, "now")
        with self.data.lock:
            row = self.data.rows[row_id]
            if row.state is not RowState.IN_FLIGHT:
                return row
            state = self._state(row.host, now)
            new_state = apply_close(state, outcome, update(state))
            closed = replace(row, state=RowState.CLOSED, closed_at=now, outcome=outcome)
            self.data.rows[row_id] = closed
            self.data.hosts[row.host] = new_state
            return closed

    def reconcile(self, *, now: datetime, older_than: timedelta) -> list[RequestRow]:
        now = as_utc(now, "now")
        check_threshold(older_than)
        cutoff = now - older_than
        abandoned = Outcome(ok=False, failure=ABANDONED)
        with self.data.lock:
            reconciled: list[RequestRow] = []
            for row_id, row in sorted(self.data.rows.items()):
                if row.state is not RowState.IN_FLIGHT or row.opened_at > cutoff:
                    continue
                closed = replace(
                    row, state=RowState.RECONCILED, closed_at=now, outcome=abandoned
                )
                self.data.rows[row_id] = closed
                state = self._state(row.host, now)
                self.data.hosts[row.host] = replace(state, failures=state.failures + 1)
                reconciled.append(closed)
            return reconciled

    def unfreeze(
        self, host: str, *, now: datetime, by: str, note: str = ""
    ) -> Unfreeze | None:
        now = as_utc(now, "now")
        check_unfreezer(by)
        with self.data.lock:
            stored = self.data.hosts.get(host)
            if stored is None or stored.freeze is None:
                return None
            trace = Unfreeze(
                id=len(self.data.unfreezes) + 1,
                host=host,
                at=now,
                by=by,
                note=note,
                freeze=stored.freeze,
            )
            self.data.unfreezes.append(trace)
            self.data.hosts[host] = replace(stored, freeze=None)
            return trace

    def unfreezes(self, *, host: str | None = None) -> list[Unfreeze]:
        with self.data.lock:
            return [u for u in self.data.unfreezes if host is None or u.host == host]

    # --- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        pass

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
