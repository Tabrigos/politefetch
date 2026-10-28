"""Processes sharing a store apply the lowest cap (#31, D-058).

Each process opens a host with the caps of its own policy. The store remembers
them for a day (`CAP_MEMORY`) and every process applies the lowest one seen:
a rollout that lowers a cap holds at once, one that raises it a day later.
"""

import logging
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from politefetch import FakeClock, Gate, HostPolicy
from politefetch.errors import RefusalReason, Refused
from politefetch.store import HostState, HostUpdate, MemoryStore, Outcome, Store
from politefetch.store.base import CAP_MEMORY

from .stores import KINDS, Harness, needs_postgres

T = datetime(2026, 10, 16, 12, tzinfo=UTC)
HOST = "example.org"
URL = "https://example.org/data.json"
TINY = timedelta(microseconds=1)
SECOND = timedelta(seconds=1)


@pytest.fixture(params=KINDS)
def harness(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Harness]:
    h = Harness(request.param, tmp_path)
    yield h
    h.close()


@pytest.fixture
def other(harness: Harness) -> Iterator[Store]:
    """A second process on the same store."""
    store = harness.another()
    yield store
    store.close()


def request(
    store: Store,
    now: datetime,
    *,
    cap: int,
    budget: int | None = None,
    wire: int | None = None,
) -> None:
    """One request opened and closed at `now`; the next may start at once."""
    row = store.open_request(
        HOST,
        URL,
        now=now,
        daily_cap=cap,
        byte_budget=budget,
        cautious=lambda _s: now + TINY,
    )
    store.close_request(
        row.id,
        now=now,
        outcome=Outcome(ok=True, status=200, wire_bytes=wire),
        update=lambda _s: HostUpdate(now + TINY, RefusalReason.MIN_INTERVAL),
    )


def refusal(store: Store, now: datetime, **caps: int | None) -> Refused:
    with pytest.raises(Refused) as refused:
        request(store, now, **caps)  # type: ignore[arg-type]
    return refused.value


# --- the store ------------------------------------------------------------------


def test_a_lower_cap_from_another_process_holds(harness: Harness, other: Store) -> None:
    request(harness.store, T, cap=2)
    request(other, T + SECOND, cap=10)
    assert refusal(other, T + 2 * SECOND, cap=10).reason is RefusalReason.DAILY_CAP
    assert other.requests_on(HOST, T.date()) == 2


def test_the_state_tells_the_lowest_caps_and_a_restart_keeps_them(
    harness: Harness, other: Store
) -> None:
    request(harness.store, T, cap=10, budget=5000)
    request(other, T + SECOND, cap=4, budget=9000)
    request(harness.store, T + 2 * SECOND, cap=7)
    state = harness.restart().host_state(HOST, T + 3 * SECOND)
    assert (state.lowest_daily_cap, state.lowest_byte_budget) == (4, 5000)


def test_a_store_that_saw_no_request_knows_no_cap(harness: Harness) -> None:
    state = harness.store.host_state(HOST, T)
    assert (state.lowest_daily_cap, state.lowest_byte_budget) == (None, None)


def test_a_cap_is_forgotten_a_day_after_it_was_last_seen(
    harness: Harness, other: Store
) -> None:
    request(harness.store, T, cap=1)
    nearly = T + CAP_MEMORY - SECOND  # the next UTC day: its count is 0
    request(other, nearly, cap=10)
    assert refusal(other, nearly + SECOND / 2, cap=10).reason is (
        RefusalReason.DAILY_CAP
    )
    # Cap 1 was last seen a day ago: reading, as `status` does, forgets it too.
    assert other.host_state(HOST, T + CAP_MEMORY).lowest_daily_cap == 10
    request(other, T + CAP_MEMORY, cap=10)
    assert other.requests_on(HOST, nearly.date()) == 2


def test_a_cap_seen_again_is_remembered_from_then(
    harness: Harness, other: Store
) -> None:
    request(harness.store, T, cap=2)
    request(harness.store, T + timedelta(hours=13), cap=2)  # the next UTC day
    # A day after the first sighting, the second one still holds.
    nearly = T + CAP_MEMORY + SECOND
    request(other, nearly, cap=10)
    assert refusal(other, nearly + SECOND, cap=10).reason is RefusalReason.DAILY_CAP


def test_a_clock_gone_back_keeps_the_later_sighting(
    harness: Harness, other: Store
) -> None:
    request(harness.store, T, cap=1)
    refusal(harness.store, T - timedelta(hours=1), cap=1)  # the clock went back
    nearly = T + CAP_MEMORY - timedelta(minutes=30)  # the next UTC day
    request(other, nearly, cap=10)
    assert refusal(other, nearly + SECOND, cap=10).reason is RefusalReason.DAILY_CAP


def test_a_refused_open_records_its_cap_too(harness: Harness, other: Store) -> None:
    for i in range(3):
        request(other, T + i * SECOND, cap=10)
    # Refused by its own cap, it still lowers the cap of the others.
    refusal(harness.store, T + 3 * SECOND, cap=2)
    assert refusal(other, T + 4 * SECOND, cap=10).reason is RefusalReason.DAILY_CAP


def test_an_open_that_fails_records_no_cap(harness: Harness, other: Store) -> None:
    def broken(_state: HostState) -> datetime:
        raise RuntimeError("crash inside the transaction")

    with pytest.raises(RuntimeError):
        harness.store.open_request(
            HOST, URL, now=T, daily_cap=1, byte_budget=10, cautious=broken
        )
    assert harness.store.host_state(HOST, T).lowest_daily_cap is None
    request(other, T, cap=10)
    request(other, T + SECOND, cap=10)


def test_a_lower_byte_budget_holds_over_no_budget(
    harness: Harness, other: Store
) -> None:
    request(harness.store, T, cap=10, budget=100, wire=60)
    # 60 spent and 60 reserved for the URL: over 100, though this process
    # has no budget of its own.
    refused = refusal(other, T + SECOND, cap=10, budget=None)
    assert refused.reason is RefusalReason.BYTE_BUDGET


# --- the gate -------------------------------------------------------------------


def gates(low: HostPolicy, high: HostPolicy) -> tuple[Gate, Gate, FakeClock]:
    """Two processes on one store, with different policies for the host."""
    clock = FakeClock(T)
    store = MemoryStore()
    return (
        Gate(store, {HOST: low}, clock=clock),
        Gate(MemoryStore(store.data), {HOST: high}, clock=clock),
        clock,
    )


def fetch(gate: Gate, clock: FakeClock, *, wire: int | None = None) -> int | None:
    """One good answer through the gate; returns `bytes_left`."""
    clock.advance(timedelta(minutes=2))
    with gate.attempt(URL) as attempt:
        attempt.response(200, wire_bytes=wire)
        attempt.accept()
    return attempt.bytes_left


def warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name.startswith("politefetch") and r.levelno == logging.WARNING
    ]


def test_a_process_with_a_higher_cap_is_warned_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    low, high, clock = gates(
        HostPolicy(daily_cap=2, min_interval="1m"),
        HostPolicy(daily_cap=10, min_interval="1m"),
    )
    with caplog.at_level(logging.INFO, logger="politefetch"):
        fetch(low, clock)
        assert warnings(caplog) == []  # its own cap is the one in force
        fetch(high, clock)
        for _ in range(2):
            with pytest.raises(Refused):
                fetch(high, clock)
    [message] = warnings(caplog)
    assert HOST in message
    assert "daily_cap 10" in message
    assert "2 " in message


def test_a_process_refused_by_a_lower_cap_is_warned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    low, high, clock = gates(
        HostPolicy(daily_cap=1, min_interval="1m"),
        HostPolicy(daily_cap=10, min_interval="1m"),
    )
    fetch(low, clock)
    with (
        caplog.at_level(logging.INFO, logger="politefetch"),
        pytest.raises(Refused) as refused,
    ):
        fetch(high, clock)
    assert refused.value.reason is RefusalReason.DAILY_CAP
    [message] = warnings(caplog)
    assert "daily_cap 10" in message


def test_status_shows_the_caps_in_force() -> None:
    low, high, clock = gates(
        HostPolicy(daily_cap=1, min_interval="1m", byte_budget=500),
        HostPolicy(daily_cap=10, min_interval="1m"),
    )
    fetch(low, clock)
    clock.advance(timedelta(minutes=2))
    status = high.status(HOST)
    assert (status.daily_cap, status.byte_budget) == (1, 500)
    assert status.reason is RefusalReason.DAILY_CAP
    assert not high.may_fetch(URL)


def test_bytes_left_follows_the_budget_in_force() -> None:
    low, high, clock = gates(
        HostPolicy(daily_cap=10, min_interval="1m", byte_budget=100),
        HostPolicy(daily_cap=10, min_interval="1m"),
    )
    fetch(low, clock, wire=30)
    assert fetch(high, clock, wire=30) == 70


# --- the invariant --------------------------------------------------------------

START = datetime(2026, 10, 16, 20, tzinfo=UTC)  # near midnight: days change
WIRE = 100  # every answer is this size, so a URL reserves it once seen


class LowestCapMachine(RuleBasedStateMachine):
    """Two processes with caps of their own, on one store: a request is
    admitted exactly when the lowest cap and budget seen in the last day
    leave room for it, so together they never exceed the lower one."""

    kind = ""

    def __init__(self) -> None:
        super().__init__()
        self.harness = Harness(self.kind)
        self.second = self.harness.another()
        self.now = START
        self.seen: dict[tuple[str, int], datetime] = {}
        self.count: dict[date, int] = {}
        self.wire: dict[date, int] = {}
        self.promise: datetime | None = None
        self.size_known = False

    def teardown(self) -> None:
        self.second.close()
        self.harness.close()

    def lowest(self, name: str, own: int | None) -> int | None:
        found = [
            value
            for (kind, value), at in self.seen.items()
            if kind == name and at > self.now - CAP_MEMORY
        ]
        if own is not None:
            found.append(own)
        return min(found) if found else None

    def note(self, name: str, value: int | None) -> None:
        if value is not None:
            key = (name, value)
            self.seen[key] = max(self.seen.get(key, self.now), self.now)

    @rule(
        process=st.sampled_from([0, 1]),
        cap=st.integers(min_value=0, max_value=4),
        budget=st.sampled_from([None, 150, 300]),
        step=st.timedeltas(min_value=TINY, max_value=timedelta(hours=8)),
    )
    def request(
        self, process: int, cap: int, budget: int | None, step: timedelta
    ) -> None:
        self.now += step
        now, day = self.now, self.now.date()
        daily_cap = self.lowest("daily_cap", cap)
        byte_budget = self.lowest("byte_budget", budget)
        assert daily_cap is not None
        count, used = self.count.get(day, 0), self.wire.get(day, 0)
        reserve = WIRE if self.size_known else 0
        expected = (
            (self.promise is None or now >= self.promise)
            and count < daily_cap
            and (byte_budget is None or used + reserve <= byte_budget)
            and (byte_budget is None or used < byte_budget)
        )
        self.note("daily_cap", cap)
        self.note("byte_budget", budget)
        store = self.harness.store if process == 0 else self.second
        try:
            request(store, now, cap=cap, budget=budget, wire=WIRE)
        except Refused:
            assert not expected
            return
        assert expected
        self.count[day] = count + 1
        self.wire[day] = used + WIRE
        self.promise = now + TINY
        self.size_known = True

    @precondition(lambda self: self.now > START)
    @rule(step=st.timedeltas(min_value=SECOND, max_value=timedelta(hours=3)))
    def clock_jumps_back(self, step: timedelta) -> None:
        self.now -= step

    @rule()
    def restart(self) -> None:
        self.harness.restart()

    @invariant()
    def counts_are_the_admitted_requests(self) -> None:
        for day, count in self.count.items():
            assert self.harness.store.requests_on(HOST, day) == count


class MemoryLowestCap(LowestCapMachine):
    kind = "memory"


class SqliteLowestCap(LowestCapMachine):
    kind = "sqlite"


class PostgresLowestCap(LowestCapMachine):
    kind = "postgres"


TestMemoryLowestCap = MemoryLowestCap.TestCase
TestMemoryLowestCap.settings = settings(deadline=None)
TestSqliteLowestCap = SqliteLowestCap.TestCase
TestSqliteLowestCap.settings = settings(max_examples=40, deadline=None)
TestPostgresLowestCap = needs_postgres(PostgresLowestCap.TestCase)
TestPostgresLowestCap.settings = settings(max_examples=20, deadline=None)
