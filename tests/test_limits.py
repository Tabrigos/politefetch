"""Limits over windows, for the host and for a key (#28, part 2, D-059).

A limit "N/P" lets at most N requests start in any span of P. It is counted
on the request log, from the start of each request: every row opened counts,
whatever its outcome. A host limit counts every request to the host; a key
limit counts the requests with the same key, and without a key given the
key is the URL. Processes sharing a store apply the lowest count asked for
each period in the last day, as with the caps (D-058).
"""

import logging
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from politefetch import FakeClock, Gate, HostPolicy, Limit
from politefetch.errors import RefusalReason, Refused
from politefetch.policy import parse_limit
from politefetch.status import to_json
from politefetch.store import HostUpdate, MemoryStore, Outcome, RowState, Store
from politefetch.store.base import CAP_MEMORY

from .adapter_kit import KINDS as FETCHERS
from .adapter_kit import getter
from .stores import KINDS, Harness, needs_postgres

T = datetime(2026, 10, 16, 12, tzinfo=UTC)
HOST = "example.org"
URL = "https://example.org/data.json"
OTHER_URL = "https://example.org/other.json"
TINY = timedelta(microseconds=1)
SECOND = timedelta(seconds=1)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
CAP = 1000


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


def host_limit(count: int, period: timedelta) -> Limit:
    return Limit(count, period)


def key_limit(count: int, period: timedelta) -> Limit:
    return Limit(count, period, per_key=True)


def request(
    store: Store,
    now: datetime,
    limits: Sequence[Limit] = (),
    *,
    url: str = URL,
    key: str | None = None,
    outcome: Outcome | None = None,
) -> None:
    """One request opened and closed at `now`; the host's promise ends at
    once, so only the limits can refuse the next one."""
    row = store.open_request(
        HOST,
        url,
        now=now,
        daily_cap=CAP,
        cautious=lambda _s: now + TINY,
        limits=limits,
        key=key,
    )
    store.close_request(
        row.id,
        now=now,
        outcome=outcome or Outcome(ok=True, status=200),
        update=lambda _s: HostUpdate(now + TINY, RefusalReason.MIN_INTERVAL),
    )


def refusal(
    store: Store,
    now: datetime,
    limits: Sequence[Limit] = (),
    *,
    url: str = URL,
    key: str | None = None,
) -> Refused:
    with pytest.raises(Refused) as refused:
        request(store, now, limits, url=url, key=key)
    return refused.value


# --- the store ------------------------------------------------------------------


def test_a_full_window_refuses_until_its_oldest_request_leaves_it(
    harness: Harness,
) -> None:
    limits = [host_limit(3, HOUR)]
    for i in range(3):
        request(harness.store, T + i * MINUTE, limits)
    refused = refusal(harness.store, T + 3 * MINUTE, limits)
    assert (refused.reason, refused.retry_after) == (
        RefusalReason.RATE_LIMIT,
        T + HOUR,
    )
    assert refusal(harness.store, T + HOUR - TINY, limits).retry_after == T + HOUR
    request(harness.store, T + HOUR, limits)  # the first one has left
    assert refusal(harness.store, T + HOUR + SECOND, limits).retry_after == (
        T + MINUTE + HOUR
    )


def test_a_host_window_counts_every_url_of_the_host(harness: Harness) -> None:
    limits = [host_limit(2, HOUR)]
    request(harness.store, T, limits, url=URL)
    request(harness.store, T + SECOND, limits, url=OTHER_URL)
    refused = refusal(
        harness.store, T + 2 * SECOND, limits, url="https://example.org/x"
    )
    assert refused.reason is RefusalReason.RATE_LIMIT


def test_every_request_opened_counts_whatever_its_outcome(harness: Harness) -> None:
    limits = [host_limit(2, HOUR)]
    request(harness.store, T, limits, outcome=Outcome(ok=False, failure="reset"))
    request(harness.store, T + SECOND, limits, outcome=Outcome(ok=False, status=500))
    assert refusal(harness.store, T + 2 * SECOND, limits).reason is (
        RefusalReason.RATE_LIMIT
    )


def test_a_request_left_in_flight_counts_after_the_reconciliation(
    harness: Harness,
) -> None:
    limits = [host_limit(1, HOUR)]
    harness.store.open_request(
        HOST, URL, now=T, daily_cap=CAP, cautious=lambda _s: T + TINY, limits=limits
    )
    harness.store.reconcile(now=T + 10 * MINUTE, older_than=5 * MINUTE)
    assert harness.store.rows(state=RowState.IN_FLIGHT) == []
    refused = refusal(harness.store, T + 20 * MINUTE, limits)
    assert (refused.reason, refused.retry_after) == (RefusalReason.RATE_LIMIT, T + HOUR)


def test_keys_have_windows_of_their_own(harness: Harness) -> None:
    limits = [key_limit(1, 30 * MINUTE)]
    request(harness.store, T, limits, key="a")
    request(harness.store, T + SECOND, limits, key="b")
    refused = refusal(harness.store, T + 2 * SECOND, limits, key="a")
    assert (refused.reason, refused.retry_after) == (
        RefusalReason.RATE_LIMIT,
        T + 30 * MINUTE,
    )
    request(harness.store, T + 30 * MINUTE, limits, key="a")


def test_without_a_key_the_url_is_the_key(harness: Harness) -> None:
    limits = [key_limit(1, 30 * MINUTE)]
    request(harness.store, T, limits, url=URL)
    request(harness.store, T + SECOND, limits, url=OTHER_URL)
    assert refusal(harness.store, T + 2 * SECOND, limits, url=URL).reason is (
        RefusalReason.RATE_LIMIT
    )
    assert [row.key for row in harness.store.rows()] == [URL, OTHER_URL]


def test_a_given_key_is_kept_in_the_row(harness: Harness) -> None:
    request(harness.store, T, key="catalogue-a")
    assert harness.restart().rows()[0].key == "catalogue-a"


def test_a_key_window_and_a_host_window_hold_together(harness: Harness) -> None:
    limits = [host_limit(2, HOUR), key_limit(1, 10 * MINUTE)]
    request(harness.store, T, limits, key="a")
    assert refusal(harness.store, T + SECOND, limits, key="a").retry_after == (
        T + 10 * MINUTE
    )
    request(harness.store, T + 2 * SECOND, limits, key="b")
    # Key c is free, the host window is not: the later end is reported.
    refused = refusal(harness.store, T + 3 * SECOND, limits, key="c")
    assert refused.retry_after == T + HOUR


def test_a_window_survives_a_restart(harness: Harness) -> None:
    limits = [host_limit(1, HOUR)]
    request(harness.store, T, limits)
    assert refusal(harness.restart(), T + MINUTE, limits).retry_after == T + HOUR


def test_a_clock_gone_back_still_counts_the_later_requests(harness: Harness) -> None:
    limits = [host_limit(1, HOUR)]
    request(harness.store, T, limits)
    refused = refusal(harness.store, T - 30 * MINUTE, limits)
    assert refused.retry_after == T + HOUR


def test_a_window_counts_the_starts_whatever_order_the_rows_were_written(
    harness: Harness,
) -> None:
    limits = [host_limit(2, HOUR)]

    def opened(at: datetime, closed: datetime) -> None:
        row = harness.store.open_request(
            HOST,
            URL,
            now=at,
            daily_cap=CAP,
            cautious=lambda _s: at + TINY,
            limits=limits,
        )
        harness.store.close_request(
            row.id,
            now=closed,
            outcome=Outcome(ok=True, status=200),
            update=lambda _s: HostUpdate(closed + TINY, RefusalReason.MIN_INTERVAL),
        )

    # The clock goes back while the first request runs: the rows are written
    # in an order that is not the order of their starts.
    opened(T, T - 3 * HOUR)
    opened(T - 170 * MINUTE, T - 170 * MINUTE)
    opened(T - HOUR, T - HOUR)
    # T and T - 1h are inside the hour before T - 50m; the second row is not.
    refused = refusal(harness.store, T - 50 * MINUTE, limits)
    assert (refused.reason, refused.retry_after) == (RefusalReason.RATE_LIMIT, T)


def test_without_limits_the_windows_refuse_nothing(harness: Harness) -> None:
    for i in range(5):
        request(harness.store, T + i * TINY * 2)


def test_a_lower_limit_from_another_process_holds(
    harness: Harness, other: Store
) -> None:
    request(harness.store, T, [host_limit(2, HOUR)])
    request(other, T + SECOND, [host_limit(10, HOUR)])
    refused = refusal(other, T + 2 * SECOND, [host_limit(10, HOUR)])
    assert refused.reason is RefusalReason.RATE_LIMIT
    state = other.host_state(HOST, T + 3 * SECOND)
    assert state.lowest_limits == (host_limit(2, HOUR),)


def test_a_process_without_limits_applies_those_of_the_others(
    harness: Harness, other: Store
) -> None:
    request(harness.store, T, [host_limit(1, HOUR), key_limit(1, 10 * MINUTE)])
    assert refusal(other, T + SECOND).retry_after == T + HOUR
    state = other.host_state(HOST, T + SECOND)
    assert set(state.lowest_limits) == {host_limit(1, HOUR), key_limit(1, 10 * MINUTE)}


def test_limits_of_different_periods_all_hold(harness: Harness, other: Store) -> None:
    request(harness.store, T, [host_limit(3, HOUR)])
    request(other, T + 11 * MINUTE, [host_limit(1, 10 * MINUTE)])
    # 2 of 3 in the hour, but the 10-minute window of the other is full.
    assert refusal(
        harness.store, T + 12 * MINUTE, [host_limit(3, HOUR)]
    ).retry_after == (T + 21 * MINUTE)


def test_a_limit_is_forgotten_a_day_after_it_was_last_seen(
    harness: Harness, other: Store
) -> None:
    request(harness.store, T, [host_limit(1, MINUTE)])
    state = other.host_state(HOST, T + CAP_MEMORY - SECOND)
    assert state.lowest_limits == (host_limit(1, MINUTE),)
    assert other.host_state(HOST, T + CAP_MEMORY).lowest_limits == ()


def test_a_refused_open_records_its_limits_too(harness: Harness, other: Store) -> None:
    request(other, T, [host_limit(10, HOUR)])
    request(other, T + SECOND, [host_limit(10, HOUR)])
    refusal(harness.store, T + 2 * SECOND, [host_limit(1, HOUR)])
    refused = refusal(other, T + 3 * SECOND, [host_limit(10, HOUR)])
    assert refused.reason is RefusalReason.RATE_LIMIT


# --- the gate -------------------------------------------------------------------


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T)


def limited_gate(store: Store, clock: FakeClock, **limits: object) -> Gate:
    policy = HostPolicy(daily_cap=CAP, min_interval="1s", **limits)  # type: ignore[arg-type]
    return Gate(store, {HOST: policy}, clock=clock)


def fetch(gate: Gate, clock: FakeClock, url: str = URL, key: str | None = None) -> None:
    clock.advance(MINUTE)
    with gate.attempt(url, key=key) as attempt:
        attempt.response(200)
        attempt.accept()


def test_the_gate_applies_the_policy_limits(harness: Harness, clock: FakeClock) -> None:
    gate = limited_gate(harness.store, clock, limits=["2/h"])
    fetch(gate, clock)
    fetch(gate, clock, OTHER_URL)
    with pytest.raises(Refused) as refused:
        fetch(gate, clock)
    assert refused.value.reason is RefusalReason.RATE_LIMIT
    assert refused.value.retry_after == T + MINUTE + HOUR


def test_the_gate_passes_the_key(harness: Harness, clock: FakeClock) -> None:
    gate = limited_gate(harness.store, clock, key_limits=["1/30m"])
    fetch(gate, clock, URL, key="a")
    fetch(gate, clock, URL, key="b")
    clock.advance(SECOND)  # past min_interval
    assert not gate.may_fetch(OTHER_URL, key="a")
    assert gate.may_fetch(URL)  # its key is the URL, not seen yet
    with pytest.raises(Refused):
        fetch(gate, clock, OTHER_URL, key="a")
    assert [row.key for row in gate.store.rows()] == ["a", "b"]


def test_may_fetch_and_status_see_a_full_window(
    harness: Harness, clock: FakeClock
) -> None:
    gate = limited_gate(harness.store, clock, limits=["1/h"], key_limits=["1/10m"])
    fetch(gate, clock)
    clock.advance(SECOND)
    verdict = gate.may_fetch(URL)
    assert verdict.refusal is not None
    assert verdict.refusal.reason is RefusalReason.RATE_LIMIT
    status = gate.status(HOST)
    assert (status.may_fetch, status.reason) == (False, RefusalReason.RATE_LIMIT)
    assert status.next_allowed == T + MINUTE + HOUR
    assert status.limits == (Limit(1, HOUR), Limit(1, 10 * MINUTE, per_key=True))
    assert '"limits": [{"count": 1, "period": "1h", "per": "host"}' in to_json(status)


def test_status_of_a_host_leaves_the_key_windows_out(
    harness: Harness, clock: FakeClock
) -> None:
    gate = limited_gate(harness.store, clock, key_limits=["1/10m"])
    fetch(gate, clock)
    clock.advance(SECOND)
    assert gate.status(HOST).may_fetch  # no key: no key window to count
    assert not gate.status(URL).may_fetch  # the URL is the key
    assert gate.status(OTHER_URL).may_fetch


def test_status_shows_the_limits_in_force(clock: FakeClock) -> None:
    store = MemoryStore()
    low = limited_gate(store, clock, limits=["1/h"])
    high = limited_gate(MemoryStore(store.data), clock, limits=["5/h", "3/10m"])
    fetch(low, clock)
    assert high.status(HOST).limits == (Limit(3, 10 * MINUTE), Limit(1, HOUR))


def test_a_process_with_a_higher_limit_is_warned_once(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    store = MemoryStore()
    low = limited_gate(store, clock, limits=["1/h"])
    high = limited_gate(MemoryStore(store.data), clock, limits=["5/h"])
    with caplog.at_level(logging.INFO, logger="politefetch"):
        fetch(low, clock)
        for _ in range(2):
            with pytest.raises(Refused):
                fetch(high, clock)
    [message] = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert HOST in message
    assert "limit 5/1h" in message
    assert "1/1h" in message


@pytest.mark.parametrize("kind", FETCHERS)
def test_the_fetchers_pass_the_key(kind: str, clock: FakeClock) -> None:
    from politefetch.testing.simulator import SourceSimulator, Validated

    policy = HostPolicy(daily_cap=CAP, min_interval="1s", key_limits=["1/h"])
    gate = Gate(MemoryStore(), {"127.0.0.1": policy}, clock=clock)
    with SourceSimulator(Validated(b'{"a": 1}')) as source:
        get = getter(kind, gate)
        get(source.url(), key="a")
        clock.advance(MINUTE)
        with pytest.raises(Refused) as refused:
            get(source.url("/other"), key="a")
        get(source.url("/other"), key="b")
    assert refused.value.reason is RefusalReason.RATE_LIMIT
    assert [row.key for row in gate.store.rows()] == ["a", "b"]


# --- the policy -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "count", "period"),
    [
        ("30/h", 30, HOUR),
        ("30/1h", 30, HOUR),
        ("1/30m", 1, 30 * MINUTE),
        ("3/10s", 3, 10 * SECOND),
        ("2/1h30m", 2, 90 * MINUTE),
        ("100/d", 100, timedelta(days=1)),
        (" 5/m ", 5, MINUTE),
    ],
)
def test_a_limit_is_read_from_its_text(
    text: str, count: int, period: timedelta
) -> None:
    assert parse_limit(text) == Limit(count, period)
    assert parse_limit(text, per_key=True) == Limit(count, period, per_key=True)


@pytest.mark.parametrize(
    "text",
    ["30", "30/", "/h", "0/h", "-1/h", "1.5/h", "30/0s", "30/2d", "30/60", "a/h"],
)
def test_a_limit_that_cannot_be_read_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="limit"):
        parse_limit(text)


@pytest.mark.parametrize(
    ("count", "period"),
    [(0, HOUR), (-1, HOUR), (1, timedelta(0)), (1, timedelta(days=1, seconds=1))],
)
def test_a_limit_out_of_range_is_refused(count: int, period: timedelta) -> None:
    with pytest.raises(ValueError, match="limit"):
        Limit(count, period)


def test_a_limit_is_written_as_it_is_read() -> None:
    assert [str(parse_limit(t)) for t in ("30/h", "1/30m", "2/90m", "3/1500ms")] == [
        "30/1h",
        "1/30m",
        "2/90m",
        "3/1500ms",
    ]


def test_the_policy_takes_texts_limits_and_a_single_text() -> None:
    policy = HostPolicy(
        daily_cap=10,
        min_interval="1s",
        limits="30/h",
        key_limits=[Limit(1, 30 * MINUTE, per_key=True), "2/h"],
    )
    assert policy.limits == (Limit(30, HOUR),)
    assert policy.key_limits == (
        Limit(1, 30 * MINUTE, per_key=True),
        Limit(2, HOUR, per_key=True),
    )
    assert HostPolicy(daily_cap=10, min_interval="1s").limits == ()


def test_the_policy_refuses_two_limits_of_one_period() -> None:
    with pytest.raises(ValueError, match="same period"):
        HostPolicy(daily_cap=10, min_interval="1s", limits=["3/h", "5/60m"])
    # The same period for the host and for each key is two limits.
    HostPolicy(daily_cap=10, min_interval="1s", limits="3/h", key_limits="1/h")


def test_the_policy_refuses_a_key_limit_given_as_a_host_one_and_back() -> None:
    with pytest.raises(ValueError, match="key_limits"):
        HostPolicy(daily_cap=10, min_interval="1s", limits=[Limit(1, HOUR, True)])
    with pytest.raises(ValueError, match="key_limits"):
        HostPolicy(daily_cap=10, min_interval="1s", key_limits=[Limit(1, HOUR)])


# --- the invariant --------------------------------------------------------------

START = datetime(2026, 10, 16, 20, tzinfo=UTC)
ALWAYS = host_limit(3, HOUR)  # every process asks for it: never exceeded
EXTRA = [
    host_limit(1, 10 * MINUTE),
    host_limit(2, HOUR),
    host_limit(5, 2 * HOUR),
    key_limit(1, 30 * MINUTE),
    key_limit(2, HOUR),
]


class LimitMachine(RuleBasedStateMachine):
    """Two processes with limits of their own on one store, two keys: a
    request is admitted exactly when every limit in force (the lowest count
    asked for each period in the last day) leaves room in its window, and
    no span of an hour ever holds more than three starts."""

    kind = ""

    def __init__(self) -> None:
        super().__init__()
        self.harness = Harness(self.kind)
        self.second = self.harness.another()
        self.now = START
        self.seen: dict[Limit, datetime] = {}
        self.starts: list[tuple[datetime, str]] = []
        self.promise: datetime | None = None

    def teardown(self) -> None:
        self.second.close()
        self.harness.close()

    def in_force(self, own: Sequence[Limit]) -> list[Limit]:
        lowest: dict[tuple[bool, timedelta], int] = {}
        recent = [lim for lim, at in self.seen.items() if at > self.now - CAP_MEMORY]
        for limit in [*own, *recent]:
            scope = (limit.per_key, limit.period)
            lowest[scope] = min(limit.count, lowest.get(scope, limit.count))
        return [Limit(n, period, key) for (key, period), n in lowest.items()]

    def room(self, limit: Limit, key: str) -> bool:
        inside = [
            at
            for at, k in self.starts
            if at > self.now - limit.period and (not limit.per_key or k == key)
        ]
        return len(inside) < limit.count

    @rule(
        process=st.sampled_from([0, 1]),
        extra=st.lists(st.sampled_from(EXTRA), max_size=2, unique=True),
        key=st.sampled_from(["a", "b"]),
        step=st.timedeltas(min_value=TINY, max_value=timedelta(minutes=40)),
    )
    def request(
        self, process: int, extra: list[Limit], key: str, step: timedelta
    ) -> None:
        self.now += step
        own = [ALWAYS, *extra]
        promised = self.promise is None or self.now >= self.promise
        room = all(self.room(limit, key) for limit in self.in_force(own))
        for limit in own:
            self.seen[limit] = max(self.seen.get(limit, self.now), self.now)
        store = self.harness.store if process == 0 else self.second
        reason = None
        try:
            request(store, self.now, own, key=key)
        except Refused as refused:
            reason = refused.reason
        if reason is not None:
            assert not (promised and room)
            assert not promised or reason is RefusalReason.RATE_LIMIT
            return
        assert promised
        assert room
        self.starts.append((self.now, key))
        self.promise = self.now + TINY

    @precondition(lambda self: self.now > START)
    @rule(step=st.timedeltas(min_value=SECOND, max_value=timedelta(hours=2)))
    def clock_jumps_back(self, step: timedelta) -> None:
        self.now -= step

    @rule()
    def restart(self) -> None:
        self.harness.restart()

    @invariant()
    def no_hour_holds_more_than_three_starts(self) -> None:
        times = sorted(at for at, _ in self.starts)
        for first, fourth in zip(times, times[3:], strict=False):
            assert fourth - first >= HOUR


class MemoryLimits(LimitMachine):
    kind = "memory"


class SqliteLimits(LimitMachine):
    kind = "sqlite"


class PostgresLimits(LimitMachine):
    kind = "postgres"


TestMemoryLimits = MemoryLimits.TestCase
TestMemoryLimits.settings = settings(deadline=None)
TestSqliteLimits = SqliteLimits.TestCase
TestSqliteLimits.settings = settings(max_examples=40, deadline=None)
TestPostgresLimits = needs_postgres(PostgresLimits.TestCase)
TestPostgresLimits.settings = settings(max_examples=20, deadline=None)
