"""Reading the gate writes nothing; opening reconciles its own host (#29).

`status`, `statuses` and `may_fetch` are what a dashboard or a monitor calls,
often from another process: they must not compete for the write lock with
the process that fetches, nor need the right to write. The rows left in
flight past `reconcile_after` show as abandoned, and the next request to
their host closes them, in the transaction that opens it.
"""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from politefetch.clock import FakeClock
from politefetch.errors import RefusalReason, Refused
from politefetch.gate import Gate
from politefetch.policy import HostPolicy
from politefetch.store import RowState, SqliteStore

from .stores import KINDS, Harness

T = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
URL = "https://example.org/data.json"
HOST = "example.org"
OTHER = "https://other.example/feed"


@pytest.fixture(params=KINDS)
def harness(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Harness]:
    h = Harness(request.param, tmp_path)
    yield h
    h.close()


def make_gate(store: object, clock: FakeClock, **policy: object) -> Gate:
    settings: dict[str, object] = {"daily_cap": 100, "min_interval": "10s", **policy}
    host_policy = HostPolicy(**settings)  # type: ignore[arg-type]
    return Gate(
        store,  # type: ignore[arg-type]
        {HOST: host_policy, "other.example": host_policy},
        clock=clock,
        reconcile_after=timedelta(hours=1),
    )


def abandon(gate: Gate, url: str = URL) -> None:
    """A request whose process died: its row stays in flight."""
    gate.attempt(url).__enter__()


def states(gate: Gate) -> list[RowState]:
    return [row.state for row in gate.store.rows()]


def test_status_shows_an_abandoned_row_without_closing_it(harness: Harness) -> None:
    clock = FakeClock(T)
    gate = make_gate(harness.store, clock)
    abandon(gate)
    clock.advance(timedelta(hours=2))
    status = gate.status(HOST)
    assert (status.in_flight, status.abandoned, status.failures) == (0, 1, 1)
    assert status.may_fetch
    assert gate.may_fetch(URL)
    gate.statuses()
    assert states(gate) == [RowState.IN_FLIGHT]  # nothing written


def test_a_row_before_the_threshold_is_still_in_flight(harness: Harness) -> None:
    clock = FakeClock(T)
    gate = make_gate(harness.store, clock)
    abandon(gate)
    clock.advance(timedelta(minutes=59))
    status = gate.status(HOST)
    assert (status.in_flight, status.abandoned, status.failures) == (1, 0, 0)
    assert status.reason is RefusalReason.IN_FLIGHT


def test_the_next_request_closes_the_abandoned_row_of_its_host(
    harness: Harness,
) -> None:
    clock = FakeClock(T)
    gate = make_gate(harness.store, clock)
    abandon(gate)
    abandon(gate, OTHER)
    clock.advance(timedelta(hours=2))
    with gate.attempt(URL) as attempt:
        attempt.response(200)
    rows = {row.id: row for row in gate.store.rows()}
    assert rows[1].state is RowState.RECONCILED
    assert rows[2].state is RowState.IN_FLIGHT  # another host, untouched
    assert gate.status("other.example").abandoned == 1


def test_a_refused_request_still_closes_the_abandoned_row(harness: Harness) -> None:
    clock = FakeClock(T)
    gate = make_gate(harness.store, clock, daily_cap=1)
    abandon(gate)
    clock.advance(timedelta(hours=2))
    with pytest.raises(Refused) as refused, gate.attempt(URL) as attempt:
        attempt.response(200)
    assert refused.value.reason is RefusalReason.DAILY_CAP
    assert states(gate) == [RowState.RECONCILED]
    assert gate.status(HOST).abandoned == 0


def test_status_answers_while_another_process_holds_the_write_lock(
    tmp_path: Path,
) -> None:
    path = tmp_path / "store.db"
    clock = FakeClock(T)
    with SqliteStore(path, busy_timeout=0.2) as store:
        gate = make_gate(store, clock)
        with gate.attempt(URL) as attempt:
            attempt.response(200)
        writer = sqlite3.connect(path, isolation_level=None)
        try:
            writer.execute("BEGIN IMMEDIATE")
            assert gate.status(HOST).requests_today == 1
            assert not gate.may_fetch(URL)  # min_interval
            assert len(gate.statuses()) == 2
        finally:
            writer.close()
