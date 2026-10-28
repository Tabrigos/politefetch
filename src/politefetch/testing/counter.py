"""Worst-case counter: the requests that actually reached the simulated
source, and the scenario that was playing when each one arrived.

It counts on the source's side, like the source's firewall would: what the
caller meant to do does not matter, only what arrived (rule 6 of SPEC.md).
A connection refused by a closed port never arrives, so it is not counted
here; the gate's log still has it.
"""

import threading
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType

from politefetch.clock import SystemClock, as_utc

__all__ = ["RequestCounter", "SourceRequest"]


@dataclass(frozen=True, slots=True)
class SourceRequest:
    """One request as the source saw it.

    Attributes:
        number: Its place among the requests that reached the source, from 1.
        method: The HTTP method.
        path: The request target as it arrived, query included.
        headers: The request headers, names in lowercase; a header sent more
            than once is recorded once, its values joined by ", ".
        scenario: The name of the scenario playing when it arrived.
        arrived: When it arrived, on the simulator's clock.
    """

    number: int
    method: str
    path: str
    headers: Mapping[str, str] = field(default_factory=dict)
    scenario: str = ""
    arrived: datetime = field(default_factory=SystemClock().now)


class RequestCounter:
    """Thread-safe record of the requests that reached the source."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: list[SourceRequest] = []

    def record(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        scenario: str,
        *,
        arrived: datetime | None = None,
    ) -> SourceRequest:
        """Record one arrived request and return it, numbered from 1.
        `arrived` defaults to the system time."""
        if arrived is None:
            arrived = SystemClock().now()
        with self._lock:
            request = SourceRequest(
                number=len(self._requests) + 1,
                method=method,
                path=path,
                headers=MappingProxyType(dict(headers)),
                scenario=scenario,
                arrived=as_utc(arrived, "arrived"),
            )
            self._requests.append(request)
            return request

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._requests)

    @property
    def requests(self) -> list[SourceRequest]:
        with self._lock:
            return list(self._requests)

    def by_scenario(self) -> dict[str, int]:
        """How many requests arrived while each scenario was playing."""
        with self._lock:
            return dict(Counter(r.scenario for r in self._requests))

    def reset(self) -> None:
        with self._lock:
            self._requests.clear()
