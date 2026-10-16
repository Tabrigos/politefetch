"""Injectable time.

Nothing in politefetch reads the wall clock directly: every component that
needs the time receives a `Clock`. Production code uses `SystemClock`; tests
use `FakeClock` and move time by hand. All times are timezone-aware UTC.
"""

from datetime import UTC, datetime, timedelta
from typing import Protocol

__all__ = ["Clock", "FakeClock", "SystemClock", "as_utc"]


def as_utc(value: datetime, name: str = "time") -> datetime:
    """Return `value` with `UTC` as its tzinfo, or raise `ValueError`.

    Naive datetimes are rejected, and so are aware ones whose offset is not
    zero: a non-UTC time reaching the library is a bug at the call site, and
    silently converting it would hide that bug.
    """
    offset = value.utcoffset()
    if offset is None:
        raise ValueError(f"{name} must be timezone-aware UTC, got a naive datetime")
    if offset:
        raise ValueError(f"{name} must be UTC, got offset {offset}")
    return value.replace(tzinfo=UTC)


class Clock(Protocol):
    """A source of the current time."""

    def now(self) -> datetime:
        """Return the current time as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    """The real wall clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)  # noqa: TID251 - the one place allowed to


class FakeClock:
    """A clock that only moves when told to."""

    def __init__(self, start: datetime) -> None:
        self._now = as_utc(start, "start")

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        """Move forward by `delta`; use `set` to make the clock jump back."""
        if delta < timedelta(0):
            raise ValueError("advance() only moves forward; use set() to jump back")
        self._now += delta

    def set(self, when: datetime) -> None:
        """Jump to `when`, forward or backward, as a real wall clock can."""
        self._now = as_utc(when, "when")
