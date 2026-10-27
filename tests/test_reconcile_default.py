"""The default `reconcile_after` follows the fetchers' deadline (#32).

A process killed mid-request leaves its row in flight; the host is asked
again only once the row is past `reconcile_after`. A fixed hour cost two runs
to a source read every 30 minutes, while a fetcher's request ends, by its own
deadline and timeouts, in under six minutes. With fetchers, the default is
twice the longest of them; without, an hour still covers `gate.attempt` with
a client of your own, whose requests the gate cannot bound.
"""

from datetime import UTC, datetime, timedelta

import pytest

from politefetch.adapters.urllib import UrllibFetcher
from politefetch.clock import FakeClock
from politefetch.gate import Gate
from politefetch.policy import HostPolicy
from politefetch.store import MemoryStore

T = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
URL = "https://example.org/data.json"
POLICY = HostPolicy(daily_cap=10, min_interval="1m")
# The fetchers' default: a 5 minute deadline, 10 s to connect, 30 s to read.
LONGEST = timedelta(minutes=5, seconds=40)


def make_gate(clock: FakeClock | None = None, **options: object) -> Gate:
    return Gate(
        MemoryStore(),
        {"example.org": POLICY},
        clock=clock or FakeClock(T),
        **options,  # type: ignore[arg-type]
    )


def test_without_fetchers_the_default_is_an_hour() -> None:
    assert make_gate().reconcile_after == timedelta(hours=1)


def test_with_a_fetcher_the_default_is_twice_its_longest_request() -> None:
    gate = make_gate()
    UrllibFetcher(gate)
    assert gate.reconcile_after == 2 * LONGEST


def test_the_longest_fetcher_counts() -> None:
    gate = make_gate()
    UrllibFetcher(gate, deadline="1m", timeout=(5, 5))
    UrllibFetcher(gate)
    UrllibFetcher(gate, deadline="2m")
    assert gate.reconcile_after == 2 * LONGEST


def test_a_value_given_by_hand_wins() -> None:
    gate = make_gate(reconcile_after="15m")
    UrllibFetcher(gate)
    assert gate.reconcile_after == timedelta(minutes=15)
    with pytest.raises(ValueError, match="reconcile_after"):
        UrllibFetcher(gate, deadline="15m")


def test_a_crashed_request_frees_its_host_after_the_default() -> None:
    clock = FakeClock(T)
    gate = make_gate(clock)
    UrllibFetcher(gate)
    gate.attempt(URL).__enter__()  # the process dies mid-request
    clock.advance(2 * LONGEST - timedelta(seconds=1))
    assert not gate.may_fetch(URL)
    clock.advance(timedelta(seconds=1))
    assert gate.may_fetch(URL)
