"""What the library logs, and what it does not (#30).

A freeze, a row closed as abandoned and an outcome the store could not save
are the events a person must hear about without polling `status`: the first
two at WARNING, the third at ERROR, since the store failed. Business
outcomes (a refusal, a good answer, a failure of the source) log nothing.
"""

import asyncio
import logging
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from politefetch.clock import FakeClock
from politefetch.errors import OutcomeNotSaved, Refused
from politefetch.gate import Gate
from politefetch.policy import HostPolicy
from politefetch.store import MemoryStore, RequestRow, Store

from .stores import KINDS, Harness

T = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
URL = "https://example.org/data.json"
HOST = "example.org"
POLICY = HostPolicy(daily_cap=10, min_interval="1m")


@pytest.fixture(params=KINDS)
def harness(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Harness]:
    h = Harness(request.param, tmp_path)
    yield h
    h.close()


def make_gate(store: Store, clock: FakeClock) -> Gate:
    return Gate(store, {HOST: POLICY}, clock=clock, reconcile_after="1h")


def logged(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == level and r.name.startswith("politefetch")
    ]


def test_a_freeze_is_logged_once(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    gate = make_gate(harness.store, FakeClock(T))
    with caplog.at_level(logging.INFO, logger="politefetch"):
        with gate.attempt(URL) as attempt:
            attempt.response(429)
        with pytest.raises(Refused):
            gate.attempt(URL).__enter__()
    [message] = logged(caplog, logging.WARNING)
    assert HOST in message
    assert "429" in message
    assert "unfreeze" in message


def test_a_row_closed_as_abandoned_is_logged(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    clock = FakeClock(T)
    gate = make_gate(harness.store, clock)
    gate.attempt(URL).__enter__()  # the process dies
    clock.advance(timedelta(hours=2))
    with caplog.at_level(logging.INFO, logger="politefetch"):
        gate.status(HOST)  # reading closes nothing, and logs nothing
        assert logged(caplog, logging.WARNING) == []
        with gate.attempt(URL) as attempt:
            attempt.response(200)
    [message] = logged(caplog, logging.WARNING)
    assert HOST in message
    assert "abandoned" in message


def test_a_reconciliation_by_hand_is_logged(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    clock = FakeClock(T)
    gate = make_gate(harness.store, clock)
    gate.attempt(URL).__enter__()
    clock.advance(timedelta(hours=2))
    with caplog.at_level(logging.INFO, logger="politefetch"):
        gate.reconcile()
    assert len(logged(caplog, logging.WARNING)) == 1


def test_business_outcomes_log_nothing(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    clock = FakeClock(T)
    gate = make_gate(harness.store, clock)
    with caplog.at_level(logging.DEBUG, logger="politefetch"):
        with gate.attempt(URL) as attempt:
            attempt.response(200)
        with pytest.raises(Refused):
            gate.attempt(URL).__enter__()
        clock.advance(timedelta(minutes=1))
        with gate.attempt(URL) as attempt:
            attempt.failure("connect_timeout")
        gate.status(HOST)
    assert [r for r in caplog.records if r.name.startswith("politefetch")] == []


class FailingClose(MemoryStore):
    """Every close fails, as SQLite does when its lock stays taken."""

    def close_request(self, row_id: int, **kwargs: Any) -> RequestRow:
        raise sqlite3.OperationalError("database is locked")


def test_an_outcome_not_saved_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    gate = make_gate(FailingClose(), FakeClock(T))
    with (
        caplog.at_level(logging.INFO, logger="politefetch"),
        pytest.raises(OutcomeNotSaved),
        gate.attempt(URL) as attempt,
    ):
        attempt.response(403)
    [message] = logged(caplog, logging.ERROR)
    assert "403" in message
    assert URL in message


def test_an_outcome_lost_during_a_cancellation_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gate = make_gate(FailingClose(), FakeClock(T))
    entered = asyncio.Event()

    async def worker() -> None:
        async with gate.attempt(URL):
            entered.set()
            await asyncio.sleep(3600)

    async def main() -> None:
        task = asyncio.create_task(worker())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with caplog.at_level(logging.INFO, logger="politefetch"):
        asyncio.run(main())
    [message] = logged(caplog, logging.ERROR)
    assert "cancelled" in message


# --- statuses ---------------------------------------------------------------------


def test_statuses_include_the_hosts_under_the_default_policy(
    harness: Harness,
) -> None:
    clock = FakeClock(T)
    gate = Gate(harness.store, {HOST: POLICY}, clock=clock, default=POLICY)
    for url in ("https://b.example/x", "https://a.example/y"):
        with gate.attempt(url) as attempt:
            attempt.response(200)
    hosts = [status.host for status in gate.statuses()]
    assert hosts == ["a.example", "b.example", HOST]


def test_statuses_without_a_default_skip_the_hosts_without_a_policy(
    harness: Harness,
) -> None:
    clock = FakeClock(T)
    open_gate = Gate(harness.store, {}, clock=clock, default=POLICY)
    with open_gate.attempt("https://a.example/y") as attempt:
        attempt.response(200)
    gate = Gate(harness.store, {HOST: POLICY}, clock=clock)
    assert [status.host for status in gate.statuses()] == [HOST]


def test_the_store_lists_the_hosts_it_knows(harness: Harness) -> None:
    clock = FakeClock(T)
    gate = Gate(harness.store, {}, clock=clock, default=POLICY)
    assert harness.store.hosts() == []
    for url in ("https://b.example/x", "https://a.example/y"):
        with gate.attempt(url) as attempt:
            attempt.response(200)
    assert harness.store.hosts() == ["a.example", "b.example"]
