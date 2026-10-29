"""The store interface, its records, and the rules every store applies.

The hard checks live here, inside the store's own transaction, and do not
depend on any other logic (rule 4 of SPEC.md): a frozen host, a request
already in flight, a promise not yet expired and a used-up daily cap all
refuse the request, whatever the caller computed. The caller only supplies
the times it is responsible for (the cautious promise before a request, the
next promise after it) as pure functions of the host state, which the store
calls inside the transaction so they always see the current state.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from types import TracebackType
from typing import Protocol, Self

from politefetch.clock import as_utc
from politefetch.errors import RefusalReason, Refused

__all__ = [
    "ABANDONED",
    "CAP_MEMORY",
    "REJECTIONS",
    "DataTimeRecord",
    "Freeze",
    "HostState",
    "HostUpdate",
    "Limit",
    "Outcome",
    "RequestRow",
    "RowState",
    "Store",
    "Unfreeze",
    "UrlRecord",
    "Validators",
    "apply_close",
    "apply_late_close",
    "caps_in_force",
    "check_admission",
    "is_late",
    "limits_in_force",
    "log_abandoned",
    "next_utc_midnight",
    "rejected",
]

_log = logging.getLogger("politefetch.store")

ABANDONED = "abandoned"
"""Failure shape recorded on rows closed by reconciliation."""

REJECTIONS = ("not_ready", "not_sent", "unusable")
"""Why a caller did not take an answer (`Attempt.reject`): the source
answered (or nothing was sent), so the host is neither punished nor
cleared. `not_ready`: complete, but not ready (empty, half a JSON);
`unusable`: the caller's parser refused it; `not_sent`: nothing left."""

REJECTED = "rejected:"
"""Prefix of the failure shape a rejection is stored with
(`"rejected:not_ready"`): apart from the failure shapes, such as the
`"not_ready"` of a transfer cut short, which is a failure."""


CAP_MEMORY = timedelta(days=1)
"""How long the store remembers the caps a host was opened with: every
process applies the lowest one seen in this time (#31)."""


_UNITS = (
    ("d", timedelta(days=1)),
    ("h", timedelta(hours=1)),
    ("m", timedelta(minutes=1)),
    ("s", timedelta(seconds=1)),
    ("ms", timedelta(milliseconds=1)),
)


def _period_text(period: timedelta) -> str:
    """The period in the largest unit that holds it whole: "1h", "90m"."""
    for unit, size in _UNITS:
        if period % size == timedelta(0):
            return f"{period // size}{unit}"
    return str(period)


@dataclass(frozen=True, slots=True)
class Limit:
    """At most `count` requests started in any span of `period`, counted on
    the request log for the host or, with `per_key`, for each key.

    Attributes:
        count: How many requests may start in one window, at least 1.
        period: The length of the window, from more than zero to a day: past
            a day the daily cap holds.
        per_key: True for a limit of each key (`key=`, the URL when none is
            given), False for one of the whole host.
    """

    count: int
    period: timedelta
    per_key: bool = False

    def __post_init__(self) -> None:
        if (
            isinstance(self.count, bool)
            or not isinstance(self.count, int)
            or self.count < 1
        ):
            raise ValueError(
                f"a limit allows at least 1 request, got {self.count!r}:"
                " use daily_cap=0 to stop a host"
            )
        if not timedelta(0) < self.period <= CAP_MEMORY:
            raise ValueError(
                f"a limit's period is more than zero and at most a day, got"
                f" {self.period}: past a day the daily cap holds"
            )

    def __str__(self) -> str:
        return f"{self.count}/{_period_text(self.period)}"

    @property
    def scope(self) -> tuple[bool, timedelta]:
        """What the limit applies to: its kind and its period."""
        return (self.per_key, self.period)


def rejected(outcome: "Outcome") -> bool:
    """True for an outcome the caller rejected: an answer, not a failure."""
    return (
        not outcome.ok
        and outcome.failure is not None
        and outcome.failure.startswith(REJECTED)
        and outcome.failure.removeprefix(REJECTED) in REJECTIONS
    )


class RowState(StrEnum):
    """Where a request row is in its life. Stored: do not rename."""

    IN_FLIGHT = "in_flight"
    CLOSED = "closed"
    RECONCILED = "reconciled"


@dataclass(frozen=True, slots=True)
class Outcome:
    """How a request ended, as the caller records it.

    Attributes:
        ok: True for a good answer (a 2xx or a 304, and data stale or stuck,
            which came from a source that answered well).
        status: The HTTP status, when an answer came.
        failure: The shape of the failure (connect timeout, reset, refusal,
            TLS, `http` for an error status...); `None` for a good answer.
            The adapters record a `FailureShape` or the `shape` of a
            `ContentError`; the gate records `unrecorded`, `cancelled` or
            `exception:<name>` for an attempt that ended without an outcome,
            and a row the reconciliation closed has `abandoned`.
        wire_bytes: The body as transferred (compressed, if it was); `None`
            when unknown.
        decoded_bytes: The body after decoding; `None` when unknown.
    """

    ok: bool
    status: int | None = None
    failure: str | None = None
    wire_bytes: int | None = None
    decoded_bytes: int | None = None

    def __post_init__(self) -> None:
        if self.ok and self.failure is not None:
            raise ValueError("a successful outcome has no failure shape")
        for amount in (self.wire_bytes, self.decoded_bytes):
            if amount is not None and amount < 0:
                raise ValueError(f"bytes cannot be negative, got {amount}")


@dataclass(frozen=True, slots=True)
class Freeze:
    """A host frozen after 403, 429 or 451.

    Attributes:
        since: When the freeze started.
        until: When it expires; `None` for a freeze that lasts until a
            person unlocks the host.
        status: The HTTP status of the answer that froze the host.
        note: Why, in words: `HTTP 403`, or the Retry-After that asked for a
            pause longer than any of the policy's.
    """

    since: datetime
    until: datetime | None = None
    status: int | None = None
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "since", as_utc(self.since, "since"))
        if self.until is not None:
            until = as_utc(self.until, "until")
            if until <= self.since:
                raise ValueError("a freeze must end after it starts")
            object.__setattr__(self, "until", until)

    def active(self, now: datetime) -> bool:
        return self.until is None or now < self.until


@dataclass(frozen=True, slots=True)
class Validators:
    """What a source said about a resource's version: raw `ETag` and
    `Last-Modified` header values, sent back as `If-None-Match` and
    `If-Modified-Since`.

    Attributes:
        etag: The raw `ETag` header value, if the source sent one.
        last_modified: The raw `Last-Modified` header value, if the source
            sent one.
    """

    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True, slots=True)
class UrlRecord:
    """What the store remembers about one URL: its validators and the wire
    size of its last full (200) answer, used to reserve bytes.

    Attributes:
        url: The URL.
        validators: The validators of its last 200, refreshed by a 304 that
            carries some.
        size: The wire size of its last full (200) answer, reserved against
            the byte budget by the next request; `None` when unknown.
        updated_at: When the record was last written.
    """

    url: str
    validators: Validators
    size: int | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DataTimeRecord:
    """The data-time history of one URL.

    Attributes:
        url: The URL.
        latest: The latest data time seen.
        reads: How many consecutive reads have not gone past `latest`, the
            read that brought it included.
        updated_at: When the last of those reads happened.
        since: When the first of those reads happened, the one that brought
            `latest`.
    """

    url: str
    latest: datetime
    reads: int
    updated_at: datetime
    since: datetime


@dataclass(frozen=True, slots=True)
class HostUpdate:
    """What the caller wants written after a request: the next promise, and a
    freeze if the answer calls for one.

    Attributes:
        not_before: The next promise: no request to the host before then.
        reason: What the promise is for: `min_interval` after a good answer,
            `backoff` after a failure.
        freeze: The freeze the answer calls for, if any.
    """

    not_before: datetime
    reason: RefusalReason
    freeze: Freeze | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "not_before", as_utc(self.not_before, "not_before"))
        if self.reason not in (RefusalReason.MIN_INTERVAL, RefusalReason.BACKOFF):
            raise ValueError(
                f"a promise is kept for min_interval or backoff, not {self.reason}"
            )


@dataclass(frozen=True, slots=True)
class HostState:
    """Everything the store knows about one host, as of one moment.

    Attributes:
        host: The host name.
        day: The UTC day of the moment the state was read for.
        requests_today: The requests opened on `day`.
        not_before: The promise: no request to the host before then; `None`
            before the first request.
        not_before_reason: What the promise is for: `backoff` for the
            cautious promise written before a request, else the reason
            written when a request closed (`min_interval` or `backoff`).
        failures: Consecutive failures since the last good answer.
        freeze: The host's freeze, if any; it may have expired.
        in_flight: Requests to the host still open.
        throttled: The 429 answers since the last good answer.
        wire_bytes_today: Bytes on the wire for the requests opened on `day`:
            real bytes, or the reservation of requests that never reported
            theirs.
        decoded_bytes_today: Bytes after decoding, for the requests opened
            on `day`.
        lowest_daily_cap: The lowest daily cap a process sharing the store
            asked to open the host with in the last day (`CAP_MEMORY`);
            `None` if none did. Every process applies it when it is lower
            than its own.
        lowest_byte_budget: The lowest byte budget asked for the same way;
            `None` if none was (a process without a budget sets none).
        lowest_limits: The lowest count asked for each kind and period of
            `Limit` the same way, sorted by kind (the host's first) and
            period.
    """

    host: str
    day: date
    requests_today: int = 0
    not_before: datetime | None = None
    not_before_reason: RefusalReason | None = None
    failures: int = 0
    freeze: Freeze | None = None
    in_flight: int = 0
    throttled: int = 0
    wire_bytes_today: int = 0
    decoded_bytes_today: int = 0
    lowest_daily_cap: int | None = None
    lowest_byte_budget: int | None = None
    lowest_limits: tuple[Limit, ...] = ()


@dataclass(frozen=True, slots=True)
class Unfreeze:
    """The trace left when a person lifts a freeze.

    Attributes:
        id: The number of the trace in the store.
        host: The host unfrozen.
        at: When the freeze was lifted.
        by: Who lifted it.
        note: Why, in the words of who lifted it.
        freeze: The freeze that was lifted.
    """

    id: int
    host: str
    at: datetime
    by: str
    note: str
    freeze: Freeze


@dataclass(frozen=True, slots=True)
class RequestRow:
    """One request in the log.

    Attributes:
        id: The number of the row in the store.
        host: The host of the request.
        url: The URL of the request.
        opened_at: When the row opened, before the request was sent.
        state: Where the row is: in flight, closed, or closed by the
            reconciliation.
        closed_at: When the row closed; `None` while in flight.
        outcome: How the request ended; `None` while in flight. The JSON of
            `politefetch.status.to_json` puts its fields directly in the row.
        reserved_bytes: The bytes reserved against the byte budget when the
            row opened: the wire size of the URL's last full answer.
        key: The key the per-key limits count the request under: the one
            given, or the URL.
    """

    id: int
    host: str
    url: str
    opened_at: datetime
    state: RowState
    closed_at: datetime | None = None
    outcome: Outcome | None = field(default=None)
    reserved_bytes: int = 0
    key: str | None = None


class Store(Protocol):
    """Persistent state behind the gate.

    `open_request`, `close_request` and `reconcile` are atomic, across
    processes too for stores that can be shared.

    The `cautious` and `update` functions run inside the store's transaction,
    with the store locked: they must be pure and fast, and must not call the
    store. If one raises, the operation writes nothing.
    """

    def host_state(self, host: str, now: datetime) -> HostState:
        """Return the state of `host`, with today's count for `now`'s UTC day."""
        ...

    def requests_on(self, host: str, day: date) -> int:
        """Return how many requests to `host` were opened on the UTC `day`."""
        ...

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
        limits: Sequence[Limit] = (),
        key: str | None = None,
    ) -> RequestRow:
        """Admit a request, or raise `Refused` having written nothing else.

        With `abandon_before`, the host's rows in flight since that time or
        earlier are first closed as abandoned, as `reconcile` does, in the
        same transaction; that close is kept even when the request is then
        refused, since those rows may be what refuses it.

        `daily_cap`, `byte_budget` and `limits` are remembered for
        `CAP_MEMORY`, a refused request's too, and the lowest ones
        remembered for the host apply in their place when lower
        (`caps_in_force`, `limits_in_force`): processes sharing the store
        with different caps do not exceed the lower one together.

        Each limit in force refuses the request while its window already
        holds `count` requests (`limit_starts`); the per-key ones count the
        rows with the same `key`, the URL when `key` is `None`.

        On admission, in one transaction: the cautious promise returned by
        `cautious(state)` is written (it must be later than `now`), today's
        count grows by one, the wire size of the URL's last full answer is
        reserved against `byte_budget` (bytes per UTC day, `None` for no
        limit), and a row is opened in flight under its key.
        """
        ...

    def limit_starts(
        self, host: str, limits: Sequence[Limit], *, key: str | None
    ) -> list[datetime | None]:
        """For each limit, when the `count`-th latest request of its scope
        started (the host's, or the requests with `key` for a per-key
        limit), or `None` if there are fewer. A per-key limit has `None`
        when `key` is `None`."""
        ...

    def close_request(
        self,
        row_id: int,
        *,
        now: datetime,
        outcome: Outcome,
        update: Callable[[HostState], HostUpdate],
        validators: Validators | None = None,
    ) -> RequestRow:
        """Close an in-flight row with its outcome and update the host.

        `update` receives the host state before this close. A good outcome
        may shorten the promise; a failure only lengthens it. The outcome's
        wire bytes replace the row's reservation, on the day the row opened.
        `validators` are those of the answer; see `apply_validators`.

        A row the reconciliation closed as abandoned takes its late outcome,
        stays `RECONCILED`, and changes the host only as `apply_late_close`
        says: never in a way that allows more requests. Closing a row closed
        already, or late already, changes nothing and returns the row as it
        is. Raises `KeyError` for an unknown row.
        """
        ...

    def url_record(self, url: str) -> UrlRecord | None:
        """Return what the store remembers about `url`, if anything."""
        ...

    def note_data_time(
        self, url: str, data_time: datetime, *, now: datetime
    ) -> DataTimeRecord:
        """Record the data time a read of `url` returned; see
        `apply_data_time`. Atomic."""
        ...

    def data_time_record(self, url: str) -> DataTimeRecord | None:
        """Return the data-time history of `url`, if any."""
        ...

    def reconcile(self, *, now: datetime, older_than: timedelta) -> list[RequestRow]:
        """Close as failed every row in flight since `now - older_than` or
        earlier, and return them. The cautious promise written when they
        opened stays."""
        ...

    def unfreeze(
        self, host: str, *, now: datetime, by: str, note: str = ""
    ) -> Unfreeze | None:
        """Lift the freeze of `host`, active or expired, and leave a trace.

        `by` says who did it. Failures, 429 count and promise stay as they
        are. Returns `None`, writing nothing, if the host has no freeze.
        """
        ...

    def unfreezes(self, *, host: str | None = None) -> list[Unfreeze]:
        """Return the unfreeze traces, oldest first."""
        ...

    def row(self, row_id: int) -> RequestRow:
        """Return one row; raise `KeyError` if it does not exist."""
        ...

    def hosts(self) -> list[str]:
        """Return the hosts the store has state for, sorted by name."""
        ...

    def rows(
        self,
        *,
        host: str | None = None,
        state: RowState | None = None,
        latest: int | None = None,
    ) -> list[RequestRow]:
        """Return the rows matching the filters, oldest first; with `latest`,
        only the newest `latest` of them (still oldest first)."""
        ...

    def close(self) -> None:
        """Release the store's resources. The persistent state stays."""
        ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


# --- the rules, shared by every store -------------------------------------------


def next_utc_midnight(now: datetime) -> datetime:
    return datetime.combine(now.date(), datetime.min.time(), UTC) + timedelta(days=1)


def check_admission(
    state: HostState,
    now: datetime,
    daily_cap: int,
    byte_budget: int | None = None,
    reserve: int = 0,
    windows: Sequence[tuple[Limit, datetime | None]] = (),
) -> Refused | None:
    """Return why a request to the host must be refused now, or `None`.

    `reserve` is the number of bytes the request would reserve against
    `byte_budget`. `windows` pairs each limit in force with the start of
    the `count`-th latest request of its scope (`Store.limit_starts`): the
    window is full until that start is `period` old. When several
    constraints apply, the one that ends last is reported, so a caller that
    waits for `retry_after` does not run into the next one.
    """
    if daily_cap < 0:
        raise ValueError(f"daily_cap must not be negative, got {daily_cap}")
    if byte_budget is not None and byte_budget < 0:
        raise ValueError(f"byte_budget must not be negative, got {byte_budget}")
    daily_cap, byte_budget = caps_in_force(state, daily_cap, byte_budget)
    blocks: list[tuple[RefusalReason, datetime | None]] = []
    if state.freeze is not None and state.freeze.active(now):
        blocks.append((RefusalReason.FROZEN, state.freeze.until))
    if state.in_flight > 0:
        promise = state.not_before if state.not_before is not None else now
        blocks.append((RefusalReason.IN_FLIGHT, max(promise, now)))
    if state.not_before is not None and now < state.not_before:
        reason = state.not_before_reason or RefusalReason.BACKOFF
        blocks.append((reason, state.not_before))
    for limit, start in windows:
        if start is not None and start + limit.period > now:
            blocks.append((RefusalReason.RATE_LIMIT, start + limit.period))
    if state.requests_today >= daily_cap:
        blocks.append((RefusalReason.DAILY_CAP, next_utc_midnight(now)))
    if byte_budget is not None:
        used = state.wire_bytes_today
        if used >= byte_budget or used + reserve > byte_budget:
            blocks.append((RefusalReason.BYTE_BUDGET, next_utc_midnight(now)))
    if not blocks:
        return None
    # A freeze without end beats everything; otherwise the latest end wins,
    # and on a tie the earlier entry (the more specific reason).
    reason, until = max(
        blocks,
        key=lambda b: (b[1] is None, b[1] or now, -blocks.index(b)),
    )
    return Refused(state.host, reason, until)


def caps_in_force(
    state: HostState, daily_cap: int, byte_budget: int | None
) -> tuple[int, int | None]:
    """The daily cap and byte budget that hold for the host: a process's own,
    or the lower ones another process sharing the store asked for in the
    last day."""
    if state.lowest_daily_cap is not None:
        daily_cap = min(daily_cap, state.lowest_daily_cap)
    if state.lowest_byte_budget is not None:
        byte_budget = (
            state.lowest_byte_budget
            if byte_budget is None
            else min(byte_budget, state.lowest_byte_budget)
        )
    return daily_cap, byte_budget


def limits_in_force(state: HostState, limits: Sequence[Limit]) -> tuple[Limit, ...]:
    """The limits that hold for the host: for each kind and period, the
    lowest count among a process's own and those another process sharing
    the store asked for in the last day; sorted as `lowest_limits`."""
    lowest: dict[tuple[bool, timedelta], int] = {}
    for limit in (*limits, *state.lowest_limits):
        lowest[limit.scope] = min(limit.count, lowest.get(limit.scope, limit.count))
    return tuple(
        Limit(count, period, per_key)
        for (per_key, period), count in sorted(lowest.items())
    )


_LIMIT = "limit:"
"""Prefix of the name a limit is remembered under: `limit:host:<microseconds>`."""


def _limit_name(limit: Limit) -> str:
    kind = "key" if limit.per_key else "host"
    return f"{_LIMIT}{kind}:{limit.period // timedelta(microseconds=1)}"


def asked_caps(
    daily_cap: int, byte_budget: int | None, limits: Sequence[Limit] = ()
) -> list[tuple[str, int]]:
    """The caps an open asks for, as the store remembers them: a process
    without a budget asks for none, and each limit is its count under a
    name that holds its kind and period."""
    asked = [("daily_cap", daily_cap)]
    if byte_budget is not None:
        asked.append(("byte_budget", byte_budget))
    asked.extend((_limit_name(limit), limit.count) for limit in limits)
    return asked


def remembered_caps(
    lowest: Mapping[str, int],
) -> tuple[int | None, int | None, tuple[Limit, ...]]:
    """The lowest daily cap, byte budget and limits, from the lowest value
    remembered under each name of `asked_caps`."""
    limits = []
    for name, count in lowest.items():
        if name.startswith(_LIMIT):
            kind, micros = name.removeprefix(_LIMIT).split(":")
            period = timedelta(microseconds=int(micros))
            limits.append(Limit(count, period, per_key=kind == "key"))
    limits.sort(key=lambda limit: limit.scope)
    return lowest.get("daily_cap"), lowest.get("byte_budget"), tuple(limits)


def check_cautious(now: datetime, cautious: datetime) -> datetime:
    cautious = as_utc(cautious, "cautious promise")
    if cautious <= now:
        raise ValueError(
            "the cautious promise must be later than now: it assumes the request fails"
        )
    return cautious


def apply_close(state: HostState, outcome: Outcome, update: HostUpdate) -> HostState:
    """Return the host state after closing one of its requests.

    A good answer may freeze the host only until a time the source asked for
    (a Retry-After longer than the policy's own pauses), never for ever. A
    rejected answer (`REJECTIONS`) takes the promise as a good one does, and
    leaves the failure counts as they were.
    """
    if outcome.ok or rejected(outcome):
        if update.freeze is not None and update.freeze.until is None:
            raise ValueError("a successful outcome cannot freeze the host for ever")
        return replace(
            state,
            not_before=update.not_before,
            not_before_reason=update.reason,
            failures=0 if outcome.ok else state.failures,
            throttled=0 if outcome.ok else state.throttled,
            freeze=_longer_freeze(state.freeze, update.freeze),
            in_flight=max(state.in_flight - 1, 0),
        )
    not_before, reason = _later_promise(state, update)
    return replace(
        state,
        not_before=not_before,
        not_before_reason=reason,
        failures=state.failures + 1,
        throttled=state.throttled + (1 if outcome.status == 429 else 0),
        freeze=update.freeze if update.freeze is not None else state.freeze,
        in_flight=max(state.in_flight - 1, 0),
    )


def log_abandoned(row: RequestRow) -> None:
    """Say that a row was closed as abandoned: its process died, or did not
    close it, and the host paused for it."""
    _log.warning(
        "%s: row %d (%s), in flight since %s, closed as abandoned and counted"
        " as a failure",
        row.host,
        row.id,
        row.url,
        row.opened_at.isoformat(),
    )


def is_late(row: RequestRow) -> bool:
    """True for a row the reconciliation closed as abandoned, whose request
    has not reported back yet: its outcome can still arrive, late."""
    return (
        row.state is RowState.RECONCILED
        and row.outcome is not None
        and row.outcome.failure == ABANDONED
    )


def apply_late_close(
    state: HostState, outcome: Outcome, update: HostUpdate
) -> HostState:
    """Return the host state after the outcome of a row already reconciled.

    The request lasted longer than the reconciliation threshold, and other
    requests may have run meanwhile: only what makes the host more careful
    is kept (a later promise, the longer freeze, one more 429). The
    reconciliation has already counted the request as a failure, and a good
    answer does not undo that.
    """
    if outcome.ok and update.freeze is not None and update.freeze.until is None:
        raise ValueError("a successful outcome cannot freeze the host for ever")
    not_before, reason = _later_promise(state, update)
    return replace(
        state,
        not_before=not_before,
        not_before_reason=reason,
        throttled=state.throttled + (1 if outcome.status == 429 else 0),
        freeze=_longer_freeze(state.freeze, update.freeze),
    )


def _later_promise(
    state: HostState, update: HostUpdate
) -> tuple[datetime | None, RefusalReason | None]:
    if state.not_before is None or update.not_before > state.not_before:
        return update.not_before, update.reason
    return state.not_before, state.not_before_reason


def _longer_freeze(old: Freeze | None, new: Freeze | None) -> Freeze | None:
    if old is None or new is None:
        return new or old
    if old.until is None:
        return old
    if new.until is None or new.until > old.until:
        return new
    return old


def apply_validators(
    previous: UrlRecord | None,
    url: str,
    outcome: Outcome,
    validators: Validators | None,
    now: datetime,
) -> UrlRecord | None:
    """Return the URL record after an answer, or `None` to leave it as is.

    A 200 replaces the validators (an answer without them forgets the old
    ones) and records its wire size. A 304 keeps what was known, refreshing
    only the validators it carries. Anything else changes nothing.
    """
    if not outcome.ok:
        return None
    if outcome.status == 200:
        size = outcome.wire_bytes
        if size is None and previous is not None:
            size = previous.size
        return UrlRecord(url, validators or Validators(), size, now)
    if outcome.status == 304:
        if previous is None and validators is None:
            return None
        old = previous.validators if previous is not None else Validators()
        new = validators or Validators()
        merged = Validators(
            etag=new.etag if new.etag is not None else old.etag,
            last_modified=(
                new.last_modified
                if new.last_modified is not None
                else old.last_modified
            ),
        )
        if previous is not None and merged == previous.validators:
            return None
        return UrlRecord(url, merged, previous.size if previous else None, now)
    return None


def apply_data_time(
    previous: DataTimeRecord | None, url: str, data_time: datetime, now: datetime
) -> DataTimeRecord:
    """Return the history after a read that returned `data_time`.

    A later time than any seen starts over at one read. The same time, or an
    earlier one (a source going back is not progress), adds a read.
    """
    data_time = as_utc(data_time, "data_time")
    if previous is None or data_time > previous.latest:
        return DataTimeRecord(
            url=url, latest=data_time, reads=1, updated_at=now, since=now
        )
    return replace(previous, reads=previous.reads + 1, updated_at=now)


def byte_changes(
    row: RequestRow, outcome: Outcome, *, late: bool = False
) -> tuple[int, int]:
    """How a close changes the day's (wire, decoded) bytes: the real wire
    bytes replace the reservation; without them the reservation stays. A
    late close only adds: the reservation is never given back."""
    wire = 0 if outcome.wire_bytes is None else outcome.wire_bytes - row.reserved_bytes
    return (max(wire, 0) if late else wire), outcome.decoded_bytes or 0


def check_unfreezer(by: str) -> str:
    if not by.strip():
        raise ValueError("unfreeze needs `by`: who lifts the freeze")
    return by


def check_latest(latest: int | None) -> None:
    if latest is not None and latest < 1:
        raise ValueError(f"latest must be at least 1, got {latest}")


def check_threshold(older_than: timedelta) -> None:
    if older_than <= timedelta(0):
        raise ValueError(f"older_than must be positive, got {older_than}")
