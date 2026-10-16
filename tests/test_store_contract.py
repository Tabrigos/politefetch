"""The store contract: every implementation must pass all of these.

A stage that adds a method to the store adds its tests here (D-002).
"""

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from politefetch.errors import RefusalReason, Refused
from politefetch.store import (
    Freeze,
    HostState,
    HostUpdate,
    Outcome,
    RequestRow,
    RowState,
    Store,
)
from politefetch.store.base import ABANDONED

from .stores import KINDS, Harness

T = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
HOST = "example.org"
URL = "https://example.org/data.json"
CAP = 5
OK = Outcome(ok=True, status=200)
FAIL = Outcome(ok=False, failure="connect_timeout")


@pytest.fixture(params=KINDS)
def harness(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Harness]:
    h = Harness(request.param, tmp_path)
    yield h
    h.close()


@pytest.fixture
def store(harness: Harness) -> Store:
    return harness.store


def after(delta: timedelta) -> Callable[[HostState], datetime]:
    """A cautious-promise function: `delta` after T."""
    return lambda _state: T + delta


def update(
    delta: timedelta,
    reason: RefusalReason = RefusalReason.MIN_INTERVAL,
    freeze: Freeze | None = None,
    *,
    base: datetime = T,
) -> Callable[[HostState], HostUpdate]:
    return lambda _state: HostUpdate(base + delta, reason, freeze)


def open_at(
    store: Store,
    now: datetime,
    *,
    host: str = HOST,
    cap: int = CAP,
    cautious: timedelta = timedelta(minutes=5),
) -> RequestRow:
    return store.open_request(
        host, URL, now=now, daily_cap=cap, cautious=lambda _s: now + cautious
    )


def close_at(
    store: Store,
    row: RequestRow,
    now: datetime,
    outcome: Outcome = OK,
    next_in: timedelta = timedelta(seconds=1),
) -> RequestRow:
    reason = RefusalReason.MIN_INTERVAL if outcome.ok else RefusalReason.BACKOFF
    return store.close_request(
        row.id,
        now=now,
        outcome=outcome,
        update=lambda _s: HostUpdate(now + next_in, reason),
    )


def snapshot(store: Store, now: datetime, host: str = HOST) -> object:
    return (store.host_state(host, now), store.rows())


# --- a host never seen ------------------------------------------------------


def test_unknown_host_has_an_empty_state(store: Store) -> None:
    assert store.host_state(HOST, T) == HostState(host=HOST, day=date(2026, 10, 16))
    assert store.rows() == []
    assert store.requests_on(HOST, date(2026, 10, 16)) == 0


# --- opening ----------------------------------------------------------------


def test_open_writes_the_cautious_promise_and_counts(store: Store) -> None:
    row = store.open_request(
        HOST, URL, now=T, daily_cap=CAP, cautious=after(timedelta(minutes=5))
    )
    assert row == RequestRow(
        id=row.id, host=HOST, url=URL, opened_at=T, state=RowState.IN_FLIGHT
    )
    state = store.host_state(HOST, T)
    assert state.not_before == T + timedelta(minutes=5)
    assert state.not_before_reason is RefusalReason.BACKOFF
    assert state.requests_today == 1
    assert state.in_flight == 1
    assert store.requests_on(HOST, T.date()) == 1
    assert store.row(row.id) == row
    assert store.rows() == [row]


def test_cautious_function_sees_the_current_state(store: Store) -> None:
    row = open_at(store, T)
    close_at(store, row, T, FAIL)
    seen: list[HostState] = []

    def cautious(state: HostState) -> datetime:
        seen.append(state)
        return T + timedelta(hours=2)

    store.open_request(
        HOST, URL, now=T + timedelta(hours=1), daily_cap=CAP, cautious=cautious
    )
    assert len(seen) == 1
    assert seen[0].failures == 1
    assert seen[0].requests_today == 1


def test_cautious_promise_must_be_in_the_future(store: Store) -> None:
    before = snapshot(store, T)
    for delta in (timedelta(0), timedelta(seconds=-1)):
        with pytest.raises(ValueError, match="cautious"):
            store.open_request(HOST, URL, now=T, daily_cap=CAP, cautious=after(delta))
    assert snapshot(store, T) == before


def test_failing_cautious_function_writes_nothing(store: Store) -> None:
    before = snapshot(store, T)

    def boom(_state: HostState) -> datetime:
        raise RuntimeError("policy bug")

    with pytest.raises(RuntimeError, match="policy bug"):
        store.open_request(HOST, URL, now=T, daily_cap=CAP, cautious=boom)
    assert snapshot(store, T) == before


def test_one_request_at_a_time_per_host(store: Store) -> None:
    open_at(store, T, cautious=timedelta(seconds=1))
    later = T + timedelta(minutes=10)  # the cautious promise has expired
    before = snapshot(store, later)
    with pytest.raises(Refused) as refused:
        open_at(store, later)
    assert refused.value.reason is RefusalReason.IN_FLIGHT
    assert refused.value.retry_after == later
    assert snapshot(store, later) == before


def test_in_flight_refusal_points_at_the_cautious_promise(store: Store) -> None:
    open_at(store, T, cautious=timedelta(minutes=5))
    with pytest.raises(Refused) as refused:
        open_at(store, T + timedelta(minutes=1))
    assert refused.value.reason is RefusalReason.IN_FLIGHT
    assert refused.value.retry_after == T + timedelta(minutes=5)


def test_promise_refuses_until_it_expires(store: Store) -> None:
    row = open_at(store, T)
    close_at(store, row, T, OK, next_in=timedelta(minutes=10))
    with pytest.raises(Refused) as refused:
        open_at(store, T + timedelta(minutes=9))
    assert refused.value.reason is RefusalReason.MIN_INTERVAL
    assert refused.value.retry_after == T + timedelta(minutes=10)
    open_at(store, T + timedelta(minutes=10))


def test_hosts_are_independent(store: Store) -> None:
    open_at(store, T, host="a.example")
    open_at(store, T, host="b.example")
    assert store.host_state("a.example", T).requests_today == 1
    assert store.host_state("b.example", T).requests_today == 1


# --- the daily cap ----------------------------------------------------------


def use_up_cap(store: Store, cap: int, start: datetime = T) -> datetime:
    now = start
    for _ in range(cap):
        row = open_at(store, now, cap=cap)
        close_at(store, row, now)
        now += timedelta(seconds=2)
    return now


def test_daily_cap_refuses_until_utc_midnight(store: Store) -> None:
    now = use_up_cap(store, 3)
    before = snapshot(store, now)
    with pytest.raises(Refused) as refused:
        open_at(store, now, cap=3)
    assert refused.value.reason is RefusalReason.DAILY_CAP
    assert refused.value.retry_after == datetime(2026, 10, 17, tzinfo=UTC)
    assert snapshot(store, now) == before


def test_daily_cap_starts_again_on_the_next_utc_day(store: Store) -> None:
    use_up_cap(store, 3)
    tomorrow = datetime(2026, 10, 17, 0, 0, tzinfo=UTC)
    open_at(store, tomorrow, cap=3)
    assert store.requests_on(HOST, date(2026, 10, 16)) == 3
    assert store.requests_on(HOST, date(2026, 10, 17)) == 1
    assert store.host_state(HOST, tomorrow).requests_today == 1


def test_zero_cap_never_admits(store: Store) -> None:
    with pytest.raises(Refused) as refused:
        open_at(store, T, cap=0)
    assert refused.value.reason is RefusalReason.DAILY_CAP


def test_negative_cap_is_rejected(store: Store) -> None:
    with pytest.raises(ValueError, match="daily_cap"):
        open_at(store, T, cap=-1)


def test_refusals_do_not_consume_the_cap(store: Store) -> None:
    open_at(store, T)
    for minute in range(1, 4):
        with pytest.raises(Refused):
            open_at(store, T + timedelta(minutes=minute))
    assert store.host_state(HOST, T).requests_today == 1


def test_refusal_reports_the_constraint_that_ends_last(store: Store) -> None:
    # Cap used up (ends at midnight) and a promise ending at 08:00 tomorrow.
    row = open_at(store, T, cap=1)
    close_at(store, row, T, OK, next_in=timedelta(hours=20))
    with pytest.raises(Refused) as refused:
        open_at(store, T + timedelta(hours=1), cap=1)
    assert refused.value.reason is RefusalReason.MIN_INTERVAL
    assert refused.value.retry_after == datetime(2026, 10, 17, 8, tzinfo=UTC)


def test_cap_wins_over_a_shorter_promise(store: Store) -> None:
    row = open_at(store, T, cap=1)
    close_at(store, row, T, OK, next_in=timedelta(minutes=10))
    with pytest.raises(Refused) as refused:
        open_at(store, T + timedelta(minutes=1), cap=1)
    assert refused.value.reason is RefusalReason.DAILY_CAP
    assert refused.value.retry_after == datetime(2026, 10, 17, tzinfo=UTC)


# --- closing ----------------------------------------------------------------


def test_a_good_answer_may_shorten_the_promise(store: Store) -> None:
    row = open_at(store, T, cautious=timedelta(hours=1))
    closed = close_at(
        store, row, T + timedelta(seconds=3), OK, next_in=timedelta(seconds=60)
    )
    assert closed.state is RowState.CLOSED
    assert closed.closed_at == T + timedelta(seconds=3)
    assert closed.outcome == OK
    state = store.host_state(HOST, T)
    assert state.not_before == T + timedelta(seconds=63)
    assert state.not_before_reason is RefusalReason.MIN_INTERVAL
    assert state.failures == 0
    assert state.in_flight == 0


def test_a_failure_never_shortens_the_promise(store: Store) -> None:
    row = open_at(store, T, cautious=timedelta(hours=1))
    close_at(store, row, T, FAIL, next_in=timedelta(minutes=1))
    state = store.host_state(HOST, T)
    assert state.not_before == T + timedelta(hours=1)
    assert state.not_before_reason is RefusalReason.BACKOFF
    assert state.failures == 1


def test_a_failure_may_lengthen_the_promise(store: Store) -> None:
    row = open_at(store, T, cautious=timedelta(minutes=1))
    close_at(store, row, T, FAIL, next_in=timedelta(hours=4))
    assert store.host_state(HOST, T).not_before == T + timedelta(hours=4)


def test_failures_accumulate_and_a_success_resets_them(store: Store) -> None:
    now = T
    for expected in (1, 2, 3):
        row = open_at(store, now)
        close_at(store, row, now, FAIL, next_in=timedelta(minutes=10))
        assert store.host_state(HOST, now).failures == expected
        now += timedelta(hours=1)
    row = open_at(store, now)
    close_at(store, row, now, OK)
    assert store.host_state(HOST, now).failures == 0


def test_update_function_sees_the_state_before_the_close(store: Store) -> None:
    row = open_at(store, T)
    close_at(store, row, T, FAIL)
    row = open_at(store, T + timedelta(hours=1))
    seen: list[HostState] = []

    def upd(state: HostState) -> HostUpdate:
        seen.append(state)
        return HostUpdate(T + timedelta(hours=2), RefusalReason.BACKOFF)

    store.close_request(row.id, now=T + timedelta(hours=1), outcome=FAIL, update=upd)
    assert seen[0].failures == 1
    assert seen[0].in_flight == 1


def test_failing_update_function_leaves_the_row_open(store: Store) -> None:
    row = open_at(store, T)
    before = snapshot(store, T)

    def boom(_state: HostState) -> HostUpdate:
        raise RuntimeError("policy bug")

    with pytest.raises(RuntimeError, match="policy bug"):
        store.close_request(row.id, now=T, outcome=OK, update=boom)
    assert snapshot(store, T) == before
    assert store.row(row.id).state is RowState.IN_FLIGHT


def test_closing_twice_changes_nothing(store: Store) -> None:
    row = open_at(store, T)
    first = close_at(store, row, T, OK, next_in=timedelta(minutes=1))
    before = snapshot(store, T)
    second = close_at(
        store, row, T + timedelta(seconds=5), FAIL, next_in=timedelta(days=1)
    )
    assert second == first
    assert snapshot(store, T) == before


def test_closing_an_unknown_row_raises(store: Store) -> None:
    with pytest.raises(KeyError):
        store.close_request(12345, now=T, outcome=OK, update=update(timedelta(0)))
    with pytest.raises(KeyError):
        store.row(12345)


def test_success_cannot_freeze(store: Store) -> None:
    row = open_at(store, T)
    before = snapshot(store, T)
    with pytest.raises(ValueError, match="freeze"):
        store.close_request(
            row.id,
            now=T,
            outcome=OK,
            update=update(timedelta(1), freeze=Freeze(since=T)),
        )
    assert snapshot(store, T) == before


# --- freezing ---------------------------------------------------------------


def test_freeze_without_expiry_waits_for_a_person(store: Store) -> None:
    row = open_at(store, T)
    freeze = Freeze(since=T, status=429, note="Too Many Requests")
    store.close_request(
        row.id,
        now=T,
        outcome=Outcome(ok=False, status=429, failure="http"),
        update=update(timedelta(minutes=1), RefusalReason.BACKOFF, freeze),
    )
    assert store.host_state(HOST, T).freeze == freeze
    with pytest.raises(Refused) as refused:
        open_at(store, T + timedelta(days=30))
    assert refused.value.reason is RefusalReason.FROZEN
    assert refused.value.retry_after is None


def test_freeze_with_expiry_lifts_by_itself(store: Store) -> None:
    row = open_at(store, T)
    until = T + timedelta(days=1)
    store.close_request(
        row.id,
        now=T,
        outcome=Outcome(ok=False, status=403, failure="http"),
        update=update(
            timedelta(minutes=1), RefusalReason.BACKOFF, Freeze(T, until, 403)
        ),
    )
    with pytest.raises(Refused) as refused:
        open_at(store, until - timedelta(seconds=1))
    assert refused.value.reason is RefusalReason.FROZEN
    assert refused.value.retry_after == until
    open_at(store, until)


# --- reconciliation ---------------------------------------------------------


def test_reconcile_closes_old_rows_as_failed(store: Store) -> None:
    old = open_at(store, T, host="old.example", cautious=timedelta(minutes=5))
    young = open_at(store, T + timedelta(minutes=50), host="young.example")
    now = T + timedelta(hours=1)
    reconciled = store.reconcile(now=now, older_than=timedelta(hours=1))
    assert [r.id for r in reconciled] == [old.id]
    row = store.row(old.id)
    assert row.state is RowState.RECONCILED
    assert row.closed_at == now
    assert row.outcome == Outcome(ok=False, failure=ABANDONED)
    assert reconciled == [row]
    state = store.host_state("old.example", now)
    assert state.in_flight == 0
    assert state.failures == 1
    assert state.requests_today == 1  # it still counts
    assert state.not_before == T + timedelta(minutes=5)  # the cautious one stays
    assert store.row(young.id).state is RowState.IN_FLIGHT


def test_reconcile_frees_the_host(store: Store) -> None:
    open_at(store, T)
    store.reconcile(now=T + timedelta(hours=2), older_than=timedelta(hours=1))
    open_at(store, T + timedelta(hours=2))


def test_a_late_close_does_not_overturn_the_reconciliation(store: Store) -> None:
    row = open_at(store, T, cautious=timedelta(minutes=5))
    store.reconcile(now=T + timedelta(hours=2), older_than=timedelta(hours=1))
    before = snapshot(store, T + timedelta(hours=2))
    late = close_at(store, row, T + timedelta(hours=3), OK, next_in=timedelta(0))
    assert late.state is RowState.RECONCILED
    assert snapshot(store, T + timedelta(hours=2)) == before


def test_reconcile_threshold_must_be_positive(store: Store) -> None:
    for bad in (timedelta(0), timedelta(seconds=-1)):
        with pytest.raises(ValueError, match="older_than"):
            store.reconcile(now=T, older_than=bad)


def test_reconcile_with_nothing_open_does_nothing(store: Store) -> None:
    assert store.reconcile(now=T, older_than=timedelta(hours=1)) == []


# --- persistence ------------------------------------------------------------


def test_everything_survives_a_restart(harness: Harness) -> None:
    store = harness.store
    row = open_at(store, T, host="a.example")
    close_at(store, row, T, FAIL, next_in=timedelta(hours=3))
    frozen = open_at(store, T, host="b.example")
    store.close_request(
        frozen.id,
        now=T,
        outcome=Outcome(ok=False, status=451, failure="http"),
        update=update(
            timedelta(1), RefusalReason.BACKOFF, Freeze(T, None, 451, "legal")
        ),
    )
    open_at(store, T, host="c.example")  # left in flight
    before = (
        [store.host_state(h, T) for h in ("a.example", "b.example", "c.example")],
        store.rows(),
    )
    store = harness.restart()
    after_restart = (
        [store.host_state(h, T) for h in ("a.example", "b.example", "c.example")],
        store.rows(),
    )
    assert after_restart == before


# --- queries and validation -------------------------------------------------


def test_rows_filter_by_host_and_state(store: Store) -> None:
    a = open_at(store, T, host="a.example")
    b = open_at(store, T, host="b.example")
    a = close_at(store, a, T)
    assert store.rows(host="a.example") == [a]
    assert store.rows(state=RowState.IN_FLIGHT) == [b]
    assert store.rows(host="a.example", state=RowState.IN_FLIGHT) == []
    assert [r.id for r in store.rows()] == sorted([a.id, b.id])


def test_naive_times_are_rejected(store: Store) -> None:
    naive = datetime(2026, 10, 16, 12)  # noqa: DTZ001
    with pytest.raises(ValueError, match="timezone"):
        store.host_state(HOST, naive)
    with pytest.raises(ValueError, match="timezone"):
        store.open_request(
            HOST, URL, now=naive, daily_cap=CAP, cautious=after(timedelta(1))
        )
    with pytest.raises(ValueError, match="timezone"):
        store.reconcile(now=naive, older_than=timedelta(hours=1))


def test_store_is_a_context_manager(harness: Harness) -> None:
    with harness.store as store:
        open_at(store, T)
    harness.restart()
    assert harness.store.host_state(HOST, T).requests_today == 1
