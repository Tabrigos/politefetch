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

from politefetch.errors import RefusalReason, Refused
from politefetch.store import HostState, HostUpdate, Outcome, SqliteStore


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


def _now(start_ns: int) -> datetime:
    # A clock shared by all processes, so their requests really interleave.
    return DAY_START + timedelta(microseconds=(time.monotonic_ns() - start_ns) // 1000)


def _promise_after(moment: datetime) -> Callable[[HostState], datetime]:
    return lambda _state: moment + TINY


def _next_after(moment: datetime) -> Callable[[HostState], HostUpdate]:
    return lambda _state: HostUpdate(moment + TINY, RefusalReason.MIN_INTERVAL)


def hammer(path: str, host: str, cap: int, attempts: int, start_ns: int) -> int:
    """Try `attempts` requests as fast as possible; return how many got in."""
    admitted = 0
    with SqliteStore(path) as store:
        for _ in range(attempts):
            now = _now(start_ns)
            try:
                row = store.open_request(
                    host,
                    "https://x/",
                    now=now,
                    daily_cap=cap,
                    cautious=_promise_after(now),
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


def open_and_die(path: str, host: str, now: datetime) -> None:
    """Open a request, then die before closing it."""
    store = SqliteStore(path)
    store.open_request(
        host,
        "https://x/",
        now=now,
        daily_cap=10,
        cautious=lambda _s: now + timedelta(minutes=5),
    )
    os._exit(3)


def hang_inside_open(path: str, host: str, now: datetime, marker: str) -> None:
    """Start opening a request and hang inside the transaction."""

    def cautious(_state: HostState) -> datetime:
        Path(marker).touch()
        time.sleep(600)
        return now + timedelta(minutes=5)

    with SqliteStore(path) as store:
        store.open_request(host, "https://x/", now=now, daily_cap=10, cautious=cautious)
