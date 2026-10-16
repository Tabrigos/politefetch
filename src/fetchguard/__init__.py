"""fetchguard: a polite, crash-safe gate between an ingest service and the
external sources it polls.

It keeps the source safe from the caller (one attempt per call, persistent
per-host promises and daily caps that survive restarts, freezing on 403/429/451)
and the caller safe from the source (truncated bodies, HTML instead of data,
data that stopped advancing). See SPEC.md for the rules.
"""

from fetchguard.clock import Clock, FakeClock, SystemClock
from fetchguard.errors import (
    ContentError,
    FetchGuardError,
    RefusalReason,
    Refused,
    SourceNotReady,
    StaleData,
    StuckSource,
    UnexpectedContent,
)

__all__ = [
    "Clock",
    "ContentError",
    "FakeClock",
    "FetchGuardError",
    "RefusalReason",
    "Refused",
    "SourceNotReady",
    "StaleData",
    "StuckSource",
    "SystemClock",
    "UnexpectedContent",
]
