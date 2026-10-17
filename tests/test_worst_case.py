"""The worst case, counted (rule 6 of SPEC.md).

A caller that tries every minute, whatever happens, against each scenario of
the source simulator, through each adapter. The simulator counts the requests
that actually reached it: that number, not the caller's intentions, is what
the source's firewall sees.

The policy is the same everywhere: 50 requests a day, 10 minutes after a good
answer, a pause after failures of 1 minute doubling up to 1 hour (never less
than the 10 minutes). Against a source that keeps failing, requests leave at
minutes 0, 10, 20, 30, 40, 56, 88, then every hour from 148: 29 in a day,
11 in six hours.
"""

import asyncio
import contextlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from politefetch.adapters.httpx import AsyncHttpxFetcher
from politefetch.clock import FakeClock
from politefetch.errors import PoliteFetchError
from politefetch.gate import Gate
from politefetch.policy import HostPolicy
from politefetch.store import MemoryStore, SqliteStore
from politefetch.testing.simulator import (
    Html,
    NoAnswer,
    Reset,
    Respond,
    Scenario,
    SourceSimulator,
    TooManyRequests,
    Truncated,
    closed_port_url,
    stuck_json,
)

from .adapter_kit import KINDS, TIMEOUT, getter

T = datetime(2026, 10, 17, 0, 0, tzinfo=UTC)  # midnight: one UTC day
HOST = "127.0.0.1"
POLICY = HostPolicy(
    daily_cap=50,
    min_interval="10m",
    backoff_base="1m",
    backoff_factor=2,
    backoff_max="1h",
)
ALWAYS_FAILING_PER_DAY = 29
ALWAYS_FAILING_PER_6_HOURS = 11


@pytest.fixture
def source() -> Iterator[SourceSimulator]:
    with SourceSimulator() as simulator:
        yield simulator


def insist(url: str, hours: int, kind: str, policy: HostPolicy = POLICY) -> Gate:
    """Try `url` every minute for `hours` fake hours; return the gate."""
    clock = FakeClock(T)
    gate = Gate(MemoryStore(), {HOST: policy}, clock=clock)
    minutes = hours * 60
    if kind == "httpx-async":

        async def run() -> None:
            async with AsyncHttpxFetcher(gate, timeout=TIMEOUT) as fetcher:
                for _ in range(minutes):
                    with contextlib.suppress(PoliteFetchError):
                        await fetcher.get(url)
                    clock.advance(timedelta(minutes=1))

        asyncio.run(run())
        return gate
    get = getter(kind, gate)
    for _ in range(minutes):
        with contextlib.suppress(PoliteFetchError):
            get(url)
        clock.advance(timedelta(minutes=1))
    return gate


@pytest.fixture(params=KINDS)
def kind(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        pytest.param(Respond(), POLICY.daily_cap, id="answers"),
        pytest.param(stuck_json(T), POLICY.daily_cap, id="stuck data"),
        pytest.param(Respond(503, b"busy"), ALWAYS_FAILING_PER_DAY, id="503"),
        pytest.param(Reset(), ALWAYS_FAILING_PER_DAY, id="reset"),
        pytest.param(
            Truncated(b'{"time": "20', declared_length=100),
            ALWAYS_FAILING_PER_DAY,
            id="truncated",
        ),
        pytest.param(Html(), ALWAYS_FAILING_PER_DAY, id="html"),
        pytest.param(TooManyRequests(retry_after="60"), 1, id="429"),
        pytest.param(Respond(403, b"no"), 1, id="403"),
        pytest.param(Respond(451, b"no"), 1, id="451"),
    ],
)
def test_requests_reaching_the_source_in_a_day(
    source: SourceSimulator, scenario: Scenario, expected: int, kind: str
) -> None:
    source.scenario = scenario
    gate = insist(source.url(), hours=24, kind=kind)
    assert source.counter.count == expected
    assert len(gate.store.rows()) == expected  # every one of them is in the log


def test_a_source_that_never_answers(source: SourceSimulator, kind: str) -> None:
    source.scenario = NoAnswer()
    insist(source.url(), hours=6, kind=kind)
    assert source.counter.count == ALWAYS_FAILING_PER_6_HOURS


def test_a_source_that_refuses_connections(kind: str) -> None:
    # A closed port: nothing reaches a server, so the log does the counting.
    gate = insist(closed_port_url(), hours=24, kind=kind)
    assert len(gate.store.rows()) == ALWAYS_FAILING_PER_DAY


def test_429_honoured_once_with_the_option(source: SourceSimulator, kind: str) -> None:
    source.scenario = TooManyRequests(retry_after="60")
    policy = HostPolicy(
        daily_cap=50,
        min_interval="10m",
        backoff_base="1m",
        respect_retry_after_429=True,
    )
    insist(source.url(), hours=24, kind=kind, policy=policy)
    assert source.counter.count == 2  # the second 429 freezes


def test_restarting_in_a_loop(
    source: SourceSimulator, tmp_path: Path, kind: str
) -> None:
    # A service that crashes and restarts a hundred times in the same minute,
    # each time trying the source as soon as it is up.
    path = tmp_path / "gate.db"
    clock = FakeClock(T)
    for _ in range(100):
        with SqliteStore(path) as store:
            gate = Gate(store, {HOST: POLICY}, clock=clock)
            with contextlib.suppress(PoliteFetchError):
                if kind == "httpx-async":
                    asyncio.run(_once(gate, source.url()))
                else:
                    getter(kind, gate)(source.url())
    assert source.counter.count == 1


async def _once(gate: Gate, url: str) -> None:
    async with AsyncHttpxFetcher(gate, timeout=TIMEOUT) as fetcher:
        await fetcher.get(url)
