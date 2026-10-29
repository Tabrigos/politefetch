"""Targets run in child processes by the tests.

They live in a module of their own because a child started with the "spawn"
method imports its target by name.
"""

import os
import socket
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from politefetch.clock import FakeClock
from politefetch.errors import RefusalReason, Refused
from politefetch.gate import Gate
from politefetch.policy import HostPolicy
from politefetch.store import HostState, HostUpdate, Limit, Outcome, SqliteStore, Store
from politefetch.store.postgres import PostgresStore

from .fetching import fetch


def add(a: int, b: int) -> int:
    return a + b


def fail(message: str) -> None:
    raise ValueError(message)


def connect(host: str, port: int) -> str:
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(b"hello from the child")
    return "connected"


def exit_now(code: int) -> None:
    os._exit(code)


def sleep(seconds: float) -> None:
    time.sleep(seconds)


# --- store targets ------------------------------------------------------------

DAY_START = datetime(2026, 10, 16, tzinfo=UTC)
TINY = timedelta(microseconds=1)


# A clock shared by all processes, so their requests really interleave:
# CLOCK_MONOTONIC on Linux, QueryPerformanceCounter on Windows. Not
# `monotonic`: with Python 3.12 on Windows it ticks every 15.6 ms.
SHARED_CLOCK: Final = "perf_counter"


def clock_ns() -> int:
    ticks: int = getattr(time, f"{SHARED_CLOCK}_ns")()
    return ticks


def _now(start_ns: int) -> datetime:
    return DAY_START + timedelta(microseconds=(clock_ns() - start_ns) // 1000)


def _promise_after(moment: datetime) -> Callable[[HostState], datetime]:
    return lambda _state: moment + TINY


def _next_after(moment: datetime) -> Callable[[HostState], HostUpdate]:
    return lambda _state: HostUpdate(moment + TINY, RefusalReason.MIN_INTERVAL)


StoreSpec = tuple[str, str, str]
"""("sqlite", path, "") or ("postgres", conninfo, schema): picklable."""


def open_store(spec: StoreSpec, *, wait: float = 30.0) -> Store:
    kind, where, schema = spec
    if kind == "sqlite":
        return SqliteStore(where, busy_timeout=wait)
    return PostgresStore(where, schema=schema, lock_timeout=wait)


def hammer(
    spec: StoreSpec,
    host: str,
    cap: int,
    attempts: int,
    start_ns: int,
    limits: tuple[Limit, ...] = (),
) -> int:
    """Try `attempts` requests as fast as possible; return how many got in."""
    admitted = 0
    with open_store(spec) as store:
        for _ in range(attempts):
            now = _now(start_ns)
            try:
                row = store.open_request(
                    host,
                    "https://x/",
                    now=now,
                    daily_cap=cap,
                    cautious=_promise_after(now),
                    limits=limits,
                )
            except Refused:
                continue
            admitted += 1
            done = _now(start_ns)
            store.close_request(
                row.id,
                now=done,
                outcome=Outcome(ok=True, status=200),
                update=_next_after(done),
            )
    return admitted


def open_and_die(spec: StoreSpec, host: str, now: datetime) -> None:
    """Open a request, then die before closing it."""
    store = open_store(spec)
    store.open_request(
        host,
        "https://x/",
        now=now,
        daily_cap=10,
        cautious=lambda _s: now + timedelta(minutes=5),
    )
    os._exit(3)


def hang_inside_open(spec: StoreSpec, host: str, now: datetime, marker: str) -> None:
    """Start opening a request and hang inside the transaction."""

    def cautious(_state: HostState) -> datetime:
        Path(marker).touch()
        time.sleep(600)
        return now + timedelta(minutes=5)

    with open_store(spec) as store:
        store.open_request(host, "https://x/", now=now, daily_cap=10, cautious=cautious)


def fetch_through_gate(path: str, url: str, start: datetime) -> None:
    """Fetch `url` through a gate on the SQLite store at `path`."""
    with SqliteStore(path) as store:
        gate = Gate(
            store,
            {"127.0.0.1": HostPolicy(daily_cap=50, min_interval="10m")},
            clock=FakeClock(start),
        )
        fetch(gate, url, timeout=600)
