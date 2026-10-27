"""Readable state per host: next allowed request, reason for any block,
today's counts and bytes, latest log rows; serialisable to JSON.

    status = gate.status("celestrak.org")      # or a URL, or gate.statuses()
    status.may_fetch, status.reason, status.next_allowed
    print(to_json(gate.statuses(), indent=2))  # for a dashboard

`Gate.status` builds a `HostStatus` with the same decision `Gate.may_fetch`
takes; this module holds the record and its JSON form.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from politefetch.errors import RefusalReason
from politefetch.store.base import Freeze, RequestRow

__all__ = ["HostStatus", "to_json"]

Json = dict[str, object]


def _time(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


def _freeze(freeze: Freeze | None) -> Json | None:
    if freeze is None:
        return None
    return {
        "since": _time(freeze.since),
        "until": _time(freeze.until),
        "status": freeze.status,
        "note": freeze.note,
    }


def _row(row: RequestRow) -> Json:
    outcome = row.outcome
    return {
        "id": row.id,
        "url": row.url,
        "opened_at": _time(row.opened_at),
        "state": row.state.value,
        "closed_at": _time(row.closed_at),
        "ok": None if outcome is None else outcome.ok,
        "status": None if outcome is None else outcome.status,
        "failure": None if outcome is None else outcome.failure,
        "wire_bytes": None if outcome is None else outcome.wire_bytes,
        "decoded_bytes": None if outcome is None else outcome.decoded_bytes,
        "reserved_bytes": row.reserved_bytes,
    }


@dataclass(frozen=True, slots=True)
class HostStatus:
    """One host as of `at`.

    The answer of `may_fetch` is advisory: `Gate.attempt` decides again,
    atomically.

    Attributes:
        host: The host name, normalized.
        at: When the status was read; the moment every other field is for.
        may_fetch: What `Gate.may_fetch` would answer.
        reason: Why a request may not start now; `None` when it may.
        next_allowed: When to ask again; `at` when a request may start now,
            `None` while the host is frozen until a person unlocks it.
        frozen: The freeze in force, if any.
        failures: Consecutive failures since the last good answer.
        throttled: The 429 answers since the last good answer.
        in_flight: Requests to the host still open, the abandoned ones left
            out.
        day: The UTC day of `at`, the day of the `*_today` counts.
        requests_today: Requests opened on `day`, counted against
            `daily_cap`.
        daily_cap: The requests per UTC day the host's policy allows.
        wire_bytes_today: Bytes on the wire for the requests opened on
            `day` (real bytes, or the reservation of requests that never
            reported theirs), counted against `byte_budget`.
        decoded_bytes_today: Bytes after decoding, for the requests opened
            on `day`.
        byte_budget: The wire bytes per UTC day the host's policy allows;
            `None` for no limit.
        recent: The latest log rows of the host, newest first.
        abandoned: Rows in flight for longer than `reconcile_after`: already
            left out of `in_flight` and counted in `failures`, as the next
            request to the host will count them when it closes them.
    """

    host: str
    at: datetime
    may_fetch: bool
    reason: RefusalReason | None
    next_allowed: datetime | None
    frozen: Freeze | None
    failures: int
    throttled: int
    in_flight: int
    day: date
    requests_today: int
    daily_cap: int
    wire_bytes_today: int
    decoded_bytes_today: int
    byte_budget: int | None
    recent: tuple[RequestRow, ...]
    abandoned: int = 0

    def to_dict(self) -> Json:
        """The status as JSON-ready values: times as ISO 8601 strings with
        their UTC offset, reasons and row states as their stored names."""
        return {
            "host": self.host,
            "at": _time(self.at),
            "may_fetch": self.may_fetch,
            "reason": None if self.reason is None else self.reason.value,
            "next_allowed": _time(self.next_allowed),
            "frozen": _freeze(self.frozen),
            "failures": self.failures,
            "throttled": self.throttled,
            "in_flight": self.in_flight,
            "abandoned": self.abandoned,
            "today": {
                "day": self.day.isoformat(),
                "requests": self.requests_today,
                "daily_cap": self.daily_cap,
                "wire_bytes": self.wire_bytes_today,
                "decoded_bytes": self.decoded_bytes_today,
                "byte_budget": self.byte_budget,
            },
            "recent": [_row(row) for row in self.recent],
        }

    def to_json(self, *, indent: int | None = None) -> str:
        """`to_dict` serialised."""
        return json.dumps(self.to_dict(), indent=indent)


def to_json(
    statuses: HostStatus | Sequence[HostStatus], *, indent: int | None = None
) -> str:
    """One status as a JSON object, or several as a JSON array."""
    if isinstance(statuses, HostStatus):
        return statuses.to_json(indent=indent)
    return json.dumps([s.to_dict() for s in statuses], indent=indent)
