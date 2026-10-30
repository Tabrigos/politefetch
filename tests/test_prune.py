"""Pruning the log (#17, D-063).

`gate.prune(older_than)` removes the closed rows opened before
`now - older_than`, then the URL records, data-time records and daily
counts no row refers to any more. It never removes a row in flight, a row
the reconciliation closed (a late close still finds it), the rows of the
last day (the limits count them), a URL's pause still running, the hosts or
the unfreeze traces.
"""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from politefetch import FakeClock, Gate, HostPolicy
from politefetch.errors import RefusalReason, Refused
from politefetch.gate import Attempt
from politefetch.store import Pruned, RowState

from .stores import KINDS, Harness, needs_postgres

T = datetime(2026, 10, 16, 12, tzinfo=UTC)
HOST = "example.org"
URL = "https://example.org/data.json"
OTHER_URL = "https://example.org/other.json"
SECOND = timedelta(seconds=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
POLICY = HostPolicy(
    daily_cap=1000, min_interval="10s", backoff_base="1m", backoff_max="1h"
)
NOTHING = Pruned(rows=0, urls=0, data_times=0, days=0)


@pytest.fixture(params=KINDS)
def harness(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Harness]:
    h = Harness(request.param, tmp_path)
    yield h
    h.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T)


@pytest.fixture
def gate(harness: Harness, clock: FakeClock) -> Gate:
    return Gate(harness.store, {HOST: POLICY}, clock=clock)


def answer(gate: Gate, url: str = URL, status: int = 200) -> int:
    """One request to `url`, answered with `status` (accepted when good);
    returns its row."""
    with gate.attempt(url) as attempt:
        attempt.response(status, headers={"ETag": '"v1"'})
        if status < 300:
            attempt.accept()
    return attempt.row.id


def row_ids(gate: Gate) -> list[int]:
    return [r.id for r in gate.store.rows()]


# --- what it takes ---------------------------------------------------------------


@pytest.mark.parametrize("older_than", ["23h", DAY - SECOND, timedelta(0)])
def test_less_than_a_day_is_refused(
    gate: Gate, clock: FakeClock, older_than: str | timedelta
) -> None:
    answer(gate)
    clock.advance(3 * DAY)
    with pytest.raises(ValueError, match="a day"):
        gate.prune(older_than)
    with pytest.raises(ValueError, match="a day"):
        gate.store.prune(now=clock.now(), older_than=DAY - SECOND)
    assert len(row_ids(gate)) == 1


def test_a_day_and_a_text_are_taken(gate: Gate, clock: FakeClock) -> None:
    answer(gate)
    clock.advance(3 * DAY)
    assert gate.prune("1d").rows == 1
    assert gate.prune(DAY) == NOTHING


# --- the rows --------------------------------------------------------------------


def test_the_closed_rows_opened_before_the_cutoff_go(
    gate: Gate, clock: FakeClock
) -> None:
    answer(gate)
    clock.advance(DAY)
    kept = answer(gate)
    clock.advance(DAY)  # the cutoff is now the second row's opening
    assert gate.prune("1d") == Pruned(rows=1, urls=0, data_times=0, days=1)
    assert row_ids(gate) == [kept]
    assert gate.prune("1d") == NOTHING


def test_a_row_in_flight_stays(gate: Gate, clock: FakeClock) -> None:
    with gate.attempt(URL) as attempt:
        clock.advance(3 * DAY)
        assert gate.prune("1d") == NOTHING
        attempt.response(200)
        attempt.accept()
    assert attempt.result is not None
    assert attempt.result.state is RowState.CLOSED


def test_a_reconciled_row_stays_for_its_late_close(
    gate: Gate, clock: FakeClock
) -> None:
    with gate.attempt(URL) as attempt:
        clock.advance(3 * DAY)
        assert len(gate.reconcile()) == 1
        assert gate.prune("1d") == NOTHING
        attempt.response(429)  # the late answer still freezes the host
    late = gate.store.row(attempt.row.id)
    assert late.state is RowState.RECONCILED
    assert late.outcome is not None
    assert late.outcome.status == 429
    assert gate.store.host_state(HOST, clock.now()).freeze is not None


def test_the_rows_a_limit_counts_stay(harness: Harness, clock: FakeClock) -> None:
    policy = HostPolicy(daily_cap=1000, min_interval="10s", limits="2/d")
    gate = Gate(harness.store, {HOST: policy}, clock=clock)
    answer(gate)
    clock.advance(HOUR)
    answer(gate)
    clock.advance(DAY - HOUR - SECOND)  # the first row opened a day ago, less 1s
    assert gate.prune("1d") == NOTHING
    refusal = gate.may_fetch(URL).refusal
    assert refusal is not None
    assert (refusal.reason, refusal.retry_after) == (RefusalReason.RATE_LIMIT, T + DAY)


def test_pruning_lasts_after_a_restart(
    harness: Harness, gate: Gate, clock: FakeClock
) -> None:
    answer(gate)
    clock.advance(3 * DAY)
    gate.prune("1d")
    assert harness.restart().rows() == []


# --- the records of the URLs -----------------------------------------------------


def test_a_url_no_row_refers_to_is_forgotten(gate: Gate, clock: FakeClock) -> None:
    answer(gate, URL)
    gate.note_data_time(URL, T)
    clock.advance(2 * DAY)
    answer(gate, OTHER_URL)
    gate.note_data_time(OTHER_URL, T)
    assert gate.prune("1d") == Pruned(rows=1, urls=1, data_times=1, days=1)
    assert gate.store.url_record(URL) is None
    assert gate.store.data_time_record(URL) is None
    assert gate.store.url_record(OTHER_URL) is not None
    assert gate.store.data_time_record(OTHER_URL) is not None
    # What is forgotten: the next request asks for the whole body.
    clock.advance(DAY)
    with gate.attempt(URL) as attempt:
        assert attempt.conditional_headers == {}
        attempt.reject("not_sent")


def test_a_url_a_row_still_refers_to_stays(gate: Gate, clock: FakeClock) -> None:
    answer(gate, URL)
    gate.note_data_time(URL, T)
    clock.advance(2 * DAY)
    with gate.attempt(URL) as attempt:  # the records are older than the cutoff
        assert gate.prune("1d") == Pruned(rows=1, urls=0, data_times=0, days=1)
        assert gate.store.url_record(URL) is not None
        assert gate.store.data_time_record(URL) is not None
        attempt.response(200)
        attempt.reject("not_ready")
    status = gate.status(URL).url
    assert status is not None
    assert (status.last_good, status.not_ready) == (T, 1)


def test_a_url_written_since_the_cutoff_stays(gate: Gate, clock: FakeClock) -> None:
    with gate.attempt(URL) as attempt:
        clock.advance(DAY + HOUR)  # a long request, closed after the cutoff
        attempt.response(200, headers={"ETag": '"v1"'})
        attempt.accept()
    gate.note_data_time(URL, T)
    clock.advance(DAY - HOUR)
    assert gate.prune("1d") == Pruned(rows=1, urls=0, data_times=0, days=1)
    assert gate.store.url_record(URL) is not None
    assert gate.store.data_time_record(URL) is not None


def test_a_url_paused_stays_until_its_pause_ends(
    harness: Harness, clock: FakeClock
) -> None:
    policy = HostPolicy(
        daily_cap=1000, min_interval="10s", backoff_base="3d", backoff_max="30d"
    )
    gate = Gate(harness.store, {HOST: policy}, clock=clock)
    answer(gate, URL, 404)
    clock.advance(2 * DAY)
    assert gate.prune("1d") == Pruned(rows=1, urls=0, data_times=0, days=1)
    record = gate.store.url_record(URL)
    assert record is not None
    assert record.not_before == T + 3 * DAY
    assert not gate.may_fetch(URL)
    clock.set(T + 3 * DAY)
    assert gate.prune("1d").urls == 1
    # The cost of forgetting it (D-063): its next 404 pauses it as the first.
    answer(gate, URL, 404)
    status = gate.status(URL).url
    assert status is not None
    assert (status.failures, status.paused_until) == (1, T + 6 * DAY)


# --- the daily counts, the hosts, the unfreezes -------------------------------------


def test_the_count_of_a_day_with_no_row_left_goes(gate: Gate, clock: FakeClock) -> None:
    answer(gate)
    clock.advance(2 * DAY)
    answer(gate)
    assert gate.prune("1d").days == 1
    assert gate.store.requests_on(HOST, T.date()) == 0
    assert gate.store.requests_on(HOST, (T + 2 * DAY).date()) == 1


def test_the_count_of_a_day_with_a_row_left_stays(gate: Gate, clock: FakeClock) -> None:
    crashed = gate.attempt(URL)
    crashed.__enter__()  # the process dies with the row in flight
    clock.advance(3 * DAY)
    gate.reconcile()
    assert gate.prune("1d") == NOTHING
    assert gate.store.requests_on(HOST, T.date()) == 1


def test_the_hosts_and_the_unfreezes_stay(gate: Gate, clock: FakeClock) -> None:
    answer(gate)
    clock.advance(HOUR)
    answer(gate, URL, 429)
    gate.unfreeze(HOST, by="ops", note="asked them")
    clock.advance(3 * DAY)
    assert gate.prune("1d").rows == 2
    state = gate.store.host_state(HOST, clock.now())
    assert (state.last_good, state.failures, state.throttled) == (T, 1, 1)
    assert gate.store.hosts() == [HOST]
    assert [u.by for u in gate.store.unfreezes()] == ["ops"]


# --- the invariant ---------------------------------------------------------------

OLDER_THAN = 2 * DAY
URLS = [URL, OTHER_URL]
LIMITED = HostPolicy(
    daily_cap=1000,
    min_interval="10s",
    backoff_base="1m",
    backoff_max="1h",
    freeze_for="2h",
    limits="20/d",
    key_limits="3/h",
)


def ending(status: int, verdict: str | None) -> Callable[[Attempt], None]:
    def end(attempt: Attempt) -> None:
        attempt.response(status)
        if verdict == "accept":
            attempt.accept()
        elif verdict is not None:
            attempt.reject(verdict)

    end.__name__ = f"{status}-{verdict}"
    return end


def lost(attempt: Attempt) -> None:
    attempt.failure("reset")


def verdict(gate: Gate, url: str) -> tuple[RefusalReason, datetime | None] | None:
    refusal = gate.may_fetch(url).refusal
    return None if refusal is None else (refusal.reason, refusal.retry_after)


def windows(gate: Gate, url: str, now: datetime) -> list[datetime | None]:
    """The start of each limit's full window still open at `now`: what
    refuses a request (`limit_starts`); a window over decides nothing."""
    limits = LIMITED.limits + LIMITED.key_limits
    starts = gate.store.limit_starts(HOST, limits, key=url)
    return [
        start if start is not None and start + limit.period > now else None
        for limit, start in zip(limits, starts, strict=True)
    ]


# No 404: forgetting a URL restarts its pause on purpose (D-063), tested above.
ENDINGS = [
    ending(200, "accept"),
    ending(200, None),
    ending(200, "not_ready"),
    ending(503, None),
    ending(429, None),
    lost,
]


class PruneMachine(RuleBasedStateMachine):
    """Two stores fed the same requests, with crashes, late closes, clock
    jumps and restarts; one of them pruned now and then. Pruning never
    changes a decision: every URL's verdict, the host's state and the
    windows of its limits stay those of the store never pruned, and the
    rows it kept are the same."""

    kind = ""

    def __init__(self) -> None:
        super().__init__()
        self.clock = FakeClock(T)
        self.kept = Harness(self.kind)
        self.pruned = Harness(self.kind)
        self.crashed: list[tuple[Attempt, Attempt]] = []
        self.pruned_at: datetime | None = None

    def teardown(self) -> None:
        self.kept.close()
        self.pruned.close()

    def gates(self) -> tuple[Gate, Gate]:
        return (
            Gate(self.kept.store, {HOST: LIMITED}, clock=self.clock),
            Gate(self.pruned.store, {HOST: LIMITED}, clock=self.clock),
        )

    def opened(self, url: str) -> tuple[Attempt, Attempt] | None:
        """Open the same attempt on both stores: both open, or both refuse."""
        attempts = [gate.attempt(url) for gate in self.gates()]
        reasons: list[tuple[RefusalReason, datetime | None] | None] = []
        for attempt in attempts:
            try:
                attempt.__enter__()
                reasons.append(None)
            except Refused as refusal:
                reasons.append((refusal.reason, refusal.retry_after))
        assert reasons[0] == reasons[1]
        return None if reasons[0] is not None else (attempts[0], attempts[1])

    @rule(
        url=st.sampled_from(URLS),
        end=st.sampled_from(ENDINGS),
        step=st.timedeltas(min_value=SECOND, max_value=6 * HOUR),
    )
    def request(
        self, url: str, end: Callable[[Attempt], None], step: timedelta
    ) -> None:
        self.clock.advance(step)
        pair = self.opened(url)
        if pair is not None:
            for attempt in pair:
                end(attempt)
                attempt.__exit__(None, None, None)

    @rule(url=st.sampled_from(URLS))
    def crash(self, url: str) -> None:
        pair = self.opened(url)
        if pair is not None:
            self.crashed.append(pair)

    @rule(end=st.sampled_from(ENDINGS))
    def late_close(self, end: Callable[[Attempt], None]) -> None:
        if self.crashed:
            for attempt in self.crashed.pop(0):
                end(attempt)
                attempt.__exit__(None, None, None)

    @rule(step=st.timedeltas(min_value=HOUR, max_value=2 * DAY))
    def time_passes(self, step: timedelta) -> None:
        self.clock.advance(step)

    @rule(back=st.timedeltas(min_value=SECOND, max_value=DAY))
    def clock_goes_back(self, back: timedelta) -> None:
        # Pruning holds while the clock stays within `older_than` less a day
        # of the latest prune: the rows the limits count are still there.
        floor = None
        if self.pruned_at is not None:
            floor = self.pruned_at - (OLDER_THAN - DAY)
        target = self.clock.now() - back
        if floor is not None:
            target = max(target, floor)
        if target < self.clock.now():
            self.clock.set(target)

    @rule()
    def prune(self) -> None:
        _, gate = self.gates()
        gate.prune(OLDER_THAN)
        now = self.clock.now()
        self.pruned_at = now if self.pruned_at is None else max(self.pruned_at, now)

    @rule()
    def restart(self) -> None:
        self.crashed.clear()  # their process is gone; the rows stay in flight
        self.kept.restart()
        self.pruned.restart()

    @invariant()
    def same_decisions(self) -> None:
        kept, pruned = self.gates()
        now = self.clock.now()
        for url in URLS:
            assert verdict(kept, url) == verdict(pruned, url)
            assert windows(kept, url, now) == windows(pruned, url, now)
        assert kept.store.host_state(HOST, now) == pruned.store.host_state(HOST, now)
        left = {r.id: r for r in pruned.store.rows()}
        for row in kept.store.rows():
            if row.id in left:
                assert left[row.id] == row
            else:
                assert row.state is RowState.CLOSED
                assert self.pruned_at is not None
                assert row.opened_at < self.pruned_at - OLDER_THAN


class MemoryPrune(PruneMachine):
    kind = "memory"


class SqlitePrune(PruneMachine):
    kind = "sqlite"


class PostgresPrune(PruneMachine):
    kind = "postgres"


TestMemoryPrune = MemoryPrune.TestCase
TestMemoryPrune.settings = settings(deadline=None)
TestSqlitePrune = SqlitePrune.TestCase
TestSqlitePrune.settings = settings(max_examples=40, deadline=None)
TestPostgresPrune = needs_postgres(PostgresPrune.TestCase)
TestPostgresPrune.settings = settings(max_examples=20, deadline=None)
