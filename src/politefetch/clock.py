"""Injectable time.

Nothing in politefetch reads the wall clock directly: every component that
needs the time receives a `Clock`. Production code uses `SystemClock`; tests
use `FakeClock` and move time by hand. All times are timezone-aware UTC.
"""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "FakeClock", "SleepingClock", "SystemClock", "as_utc"]


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


@runtime_checkable
class SleepingClock(Clock, Protocol):
    """A clock that can also wait: what `wait_up_to` needs (D-061).

    The gate waits through the clock it reads, so a fake clock that moves
    when it sleeps keeps the wait and its bound in step. A `Clock` without
    these methods still serves for everything else.
    """

    def sleep(self, delta: timedelta) -> None:
        """Wait for `delta`."""
        ...

    async def sleep_async(self, delta: timedelta) -> None:
        """Wait for `delta` without blocking the event loop."""
        ...


class SystemClock:
    """The real wall clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)  # noqa: TID251 - the one place allowed to

    def sleep(self, delta: timedelta) -> None:
        time.sleep(max(0.0, delta.total_seconds()))

    async def sleep_async(self, delta: timedelta) -> None:
        await asyncio.sleep(max(0.0, delta.total_seconds()))


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

    def sleep(self, delta: timedelta) -> None:
        """Move forward by `delta` at once: nothing waits."""
        self.advance(max(delta, timedelta(0)))

    async def sleep_async(self, delta: timedelta) -> None:
        """Move forward by `delta`, letting other tasks run once."""
        self.advance(max(delta, timedelta(0)))
        await asyncio.sleep(0)
