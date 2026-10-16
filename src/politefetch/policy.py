"""Per-host policies: daily cap, minimum interval, growing pause after a
failure, freezing on 403/429/451, Retry-After as a minimum.

A policy only computes times; the store applies them and does the hard
checks (D-014). Everything here is a pure function of the host state, the
time and the outcome.
"""

import email.utils
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from politefetch.errors import RefusalReason
from politefetch.store.base import Freeze, HostState, HostUpdate, Outcome

__all__ = ["HostPolicy", "parse_duration", "parse_retry_after"]

_DURATION = re.compile(
    r"(?:(?P<d>\d+)d)?(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m(?!s))?"
    r"(?:(?P<s>\d+)s)?(?:(?P<ms>\d+)ms)?"
)
_UNITS = {
    "d": timedelta(days=1),
    "h": timedelta(hours=1),
    "m": timedelta(minutes=1),
    "s": timedelta(seconds=1),
    "ms": timedelta(milliseconds=1),
}

# A Retry-After further away than this is treated as this far: the header is
# a minimum, but an absurd value must not overflow the date arithmetic.
_LONGEST_RETRY_AFTER = timedelta(days=365)

# Statuses that freeze the host whatever the policy says (D-013).
_ALWAYS_FREEZE = frozenset({403, 451})
_TOO_MANY_REQUESTS = 429


def parse_duration(value: timedelta | str) -> timedelta:
    """Turn "90s", "15m", "2h", "1d", "1h30m" or "500ms" into a timedelta.

    Bare numbers are rejected: "60" could mean seconds or minutes, and a wrong
    guess could mean sixty times more requests.
    """
    if isinstance(value, timedelta):
        if value < timedelta(0):
            raise ValueError(f"a duration cannot be negative, got {value}")
        return value
    text = value.strip()
    match = _DURATION.fullmatch(text)
    if not text or match is None or not any(match.groupdict().values()):
        raise ValueError(f"not a duration: {value!r} (use e.g. '90s', '15m', '2h')")
    return sum(
        (
            _UNITS[unit] * int(amount)
            for unit, amount in match.groupdict().items()
            if amount
        ),
        timedelta(0),
    )


def parse_retry_after(value: str | None, now: datetime) -> datetime | None:
    """Return the time a Retry-After header asks to wait until, or `None` if
    the header is missing or unreadable.

    Both forms of RFC 9110 are read: delay in seconds and HTTP date. A date
    in the past means "now".
    """
    if value is None:
        return None
    text = value.strip()
    if text.isdigit():
        delay = min(timedelta(seconds=int(text[:12])), _LONGEST_RETRY_AFTER)
        return now + delay
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when.tzinfo is None:  # HTTP dates are GMT
        when = when.replace(tzinfo=UTC)
    when = when.astimezone(UTC)
    return min(max(when, now), now + _LONGEST_RETRY_AFTER)


@dataclass(frozen=True, slots=True, init=False)
class HostPolicy:
    """How carefully to treat one host.

    - `daily_cap`: requests per UTC day, the safety net (rule 4).
    - `min_interval`: pause after a good answer, counted from the end of the
      request.
    - `backoff_base`, `backoff_factor`, `backoff_max`: the pause after the
      n-th consecutive failure is `base * factor**(n-1)`, at most `max`.
    - `freeze_for`: how long a 403/429/451 freeze lasts; `None` (default)
      means until a person unlocks the host. Never shorter than Retry-After.
    - `respect_retry_after_429`: off by default. When on, the first 429 since
      the last good answer that carries a Retry-After only pauses; any other
      429 freezes.

    Durations accept a `timedelta` or a string such as "90s" or "2h".
    """

    daily_cap: int
    min_interval: timedelta
    backoff_base: timedelta
    backoff_factor: float
    backoff_max: timedelta
    freeze_for: timedelta | None
    respect_retry_after_429: bool

    def __init__(
        self,
        daily_cap: int,
        min_interval: timedelta | str,
        *,
        backoff_base: timedelta | str = "1m",
        backoff_factor: float = 2.0,
        backoff_max: timedelta | str = "6h",
        freeze_for: timedelta | str | None = None,
        respect_retry_after_429: bool = False,
    ) -> None:
        if (
            isinstance(daily_cap, bool)
            or not isinstance(daily_cap, int)
            or daily_cap < 0
        ):
            raise ValueError(
                f"daily_cap must be a non-negative integer, got {daily_cap!r}"
            )
        base = parse_duration(backoff_base)
        if base <= timedelta(0):
            raise ValueError("backoff_base must be positive: a failure always pauses")
        if not math.isfinite(backoff_factor) or backoff_factor < 1:
            raise ValueError(f"backoff_factor must be at least 1, got {backoff_factor}")
        cap = parse_duration(backoff_max)
        if cap < base:
            raise ValueError("backoff_max must not be shorter than backoff_base")
        freeze = None if freeze_for is None else parse_duration(freeze_for)
        if freeze is not None and freeze <= timedelta(0):
            raise ValueError("freeze_for must be positive, or None for a manual unlock")
        set_field = object.__setattr__
        set_field(self, "daily_cap", daily_cap)
        set_field(self, "min_interval", parse_duration(min_interval))
        set_field(self, "backoff_base", base)
        set_field(self, "backoff_factor", float(backoff_factor))
        set_field(self, "backoff_max", cap)
        set_field(self, "freeze_for", freeze)
        set_field(self, "respect_retry_after_429", bool(respect_retry_after_429))

    def backoff(self, failures: int) -> timedelta:
        """The pause after the `failures`-th consecutive failure."""
        if failures < 1:
            raise ValueError(f"failures must be at least 1, got {failures}")
        base = self.backoff_base.total_seconds()
        ceiling = self.backoff_max.total_seconds()
        if self.backoff_factor == 1:
            return self.backoff_base
        # Compare in log space so that huge failure counts cannot overflow.
        if (failures - 1) * math.log(self.backoff_factor) >= math.log(ceiling / base):
            return self.backoff_max
        return min(
            timedelta(seconds=base * self.backoff_factor ** (failures - 1)),
            self.backoff_max,
        )

    def cautious_until(self, state: HostState, now: datetime) -> datetime:
        """The promise written before a request: as if it were going to fail."""
        return now + max(self.min_interval, self.backoff(state.failures + 1))

    def after(
        self,
        state: HostState,
        now: datetime,
        outcome: Outcome,
        retry_at: datetime | None,
    ) -> HostUpdate:
        """The promise (and freeze, if any) after a request ended at `now`.

        `state` is the host state before this request is closed. `retry_at`
        is the time the source's Retry-After asked for, if any: a minimum.
        """
        if outcome.ok:
            until = now + self.min_interval
            if retry_at is not None:
                until = max(until, retry_at)
            return HostUpdate(until, RefusalReason.MIN_INTERVAL)
        pause = max(self.min_interval, self.backoff(state.failures + 1))
        until = now + pause
        if retry_at is not None:
            until = max(until, retry_at)
        freeze = None
        if self._freezes(state, outcome.status, retry_at):
            freeze = self._freeze(now, outcome.status, retry_at)
        return HostUpdate(until, RefusalReason.BACKOFF, freeze)

    def _freezes(
        self, state: HostState, status: int | None, retry_at: datetime | None
    ) -> bool:
        if status in _ALWAYS_FREEZE:
            return True
        if status != _TOO_MANY_REQUESTS:
            return False
        if not self.respect_retry_after_429 or retry_at is None:
            return True
        return state.throttled > 0  # a second 429 since the last good answer

    def _freeze(
        self, now: datetime, status: int | None, retry_at: datetime | None
    ) -> Freeze:
        until = None
        if self.freeze_for is not None:
            until = now + self.freeze_for
            if retry_at is not None:
                until = max(until, retry_at)
        return Freeze(since=now, until=until, status=status, note=f"HTTP {status}")
