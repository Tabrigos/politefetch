"""politefetch: a polite, crash-safe gate between an ingest service and the
external sources it polls.

It keeps the source safe from the caller (one attempt per call, persistent
per-host promises and daily caps that survive restarts, freezing on 403/429/451)
and the caller safe from the source (truncated bodies, HTML instead of data,
data that stopped advancing). See SPEC.md for the rules.
"""

from politefetch.clock import Clock, FakeClock, SystemClock
from politefetch.errors import (
    ContentError,
    FetchFailed,
    OutcomeNotSaved,
    PoliteFetchError,
    RefusalReason,
    Refused,
    SourceNotReady,
    StaleData,
    StuckSource,
    UnexpectedContent,
)
from politefetch.gate import Gate
from politefetch.policy import HostPolicy

__version__ = "0.1.0"

__all__ = [
    "Clock",
    "ContentError",
    "FakeClock",
    "FetchFailed",
    "Gate",
    "HostPolicy",
    "OutcomeNotSaved",
    "PoliteFetchError",
    "RefusalReason",
    "Refused",
    "SourceNotReady",
    "StaleData",
    "StuckSource",
    "SystemClock",
    "UnexpectedContent",
    "__version__",
]
