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

    `may_fetch` is what `Gate.may_fetch` would answer; if false, `reason` says
    why and `next_allowed` when to ask again (`None` while the host is frozen
    until a person unlocks it). If true, `next_allowed` is `at`. The answer is
    advisory: `Gate.attempt` decides again, atomically.

    `frozen` is the freeze in force, if any. `failures` counts consecutive
    failures and `throttled` the 429 answers since the last good answer.
    The `*_today` counts are for `day`, the UTC day of `at`; `daily_cap` and
    `byte_budget` come from the host's policy. `recent` holds the latest log
    rows of the host, newest first.
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
