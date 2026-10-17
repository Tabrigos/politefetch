"""The scenarios of stage T2, against the source simulator with real HTTP.

The gate keeps a fake clock; only the simulator and urllib use real time, and
only for a few tenths of a second when the source does not answer.
"""

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from politefetch.clock import FakeClock
from politefetch.errors import RefusalReason
from politefetch.gate import Gate
from politefetch.policy import HostPolicy
from politefetch.store import MemoryStore, RowState, SqliteStore
from politefetch.testing.processes import ChildFailed, spawn
from politefetch.testing.simulator import NoAnswer, Respond, SourceSimulator

from . import children
from .fetching import fetch

T = datetime(2026, 10, 17, 0, 0, tzinfo=UTC)
HOST = "127.0.0.1"


@pytest.fixture
def source() -> Iterator[SourceSimulator]:
    with SourceSimulator() as simulator:
        yield simulator


def promise(gate: Gate, clock: FakeClock) -> datetime | None:
    return gate.store.host_state(HOST, clock.now()).not_before


def test_the_pause_grows(source: SourceSimulator) -> None:
    source.scenario = Respond(503, b"busy")
    clock = FakeClock(T)
    policy = HostPolicy(
        daily_cap=50, min_interval="0s", backoff_base="1m", backoff_max="10m"
    )
    gate = Gate(MemoryStore(), {HOST: policy}, clock=clock)
    pauses = []
    for _ in range(6):
        fetch(gate, source.url())
        until = promise(gate, clock)
        assert until is not None
        pauses.append(until - clock.now())
        clock.set(until)
    assert pauses == [timedelta(minutes=m) for m in (1, 2, 4, 8, 10, 10)]
    assert source.counter.count == 6


def test_a_good_answer_shortens_the_promise(source: SourceSimulator) -> None:
    clock = FakeClock(T)
    policy = HostPolicy(daily_cap=50, min_interval="10s", backoff_base="1h")
    gate = Gate(MemoryStore(), {HOST: policy}, clock=clock)
    attempt = fetch(gate, source.url())
    assert attempt.result is not None
    assert attempt.result.outcome is not None
    assert attempt.result.outcome.status == 200
    assert promise(gate, clock) == T + timedelta(seconds=10)  # not the cautious hour


def test_a_crash_leaves_the_promise_cautious(
    source: SourceSimulator, tmp_path: Path
) -> None:
    path = str(tmp_path / "gate.db")
    source.scenario = NoAnswer()
    child = spawn(children.fetch_through_gate, path, source.url(), T)
    deadline = time.monotonic() + 60
    while source.counter.count == 0:
        assert time.monotonic() < deadline, "the request never reached the source"
        time.sleep(0.02)
    child.kill()  # the process dies with its request open at the source
    with pytest.raises(ChildFailed):
        child.join(timeout=60)

    source.scenario = Respond()
    clock = FakeClock(T)
    with SqliteStore(path) as store:
        gate = Gate(
            store,
            {HOST: HostPolicy(daily_cap=50, min_interval="10m")},
            clock=clock,
            reconcile_after="1h",
        )
        for _ in range(59):
            clock.advance(timedelta(minutes=1))
            refusal = gate.may_fetch(source.url()).refusal
            assert refusal is not None
            assert refusal.reason is RefusalReason.IN_FLIGHT
        assert source.counter.count == 1  # nothing else reached the source
        clock.advance(timedelta(minutes=1))
        assert gate.may_fetch(source.url())  # reconciled as failed
        assert store.rows()[0].state is RowState.RECONCILED
        assert store.host_state(HOST, clock.now()).failures == 1
