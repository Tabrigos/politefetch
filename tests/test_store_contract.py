"""The store contract: every implementation must pass all of these.

A stage that adds a method to the store adds its tests here (D-002).
"""

from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from politefetch.errors import RefusalReason, Refused
from politefetch.store import (
    DataTimeRecord,
    Freeze,
    HostState,
    HostUpdate,
    Outcome,
    RequestRow,
    RowState,
    Store,
    Unfreeze,
    UrlRecord,
    Validators,
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


def snapshot(
    store: Store, now: datetime, host: str = HOST
) -> tuple[HostState, list[RequestRow]]:
    return (store.host_state(host, now), store.rows())


# --- a host never seen ----------------------------------------------------------------


def test_unknown_host_has_an_empty_state(store: Store) -> None:
    assert store.host_state(HOST, T) == HostState(host=HOST, day=date(2026, 10, 16))
    assert store.rows() == []
    assert store.requests_on(HOST, date(2026, 10, 16)) == 0


# --- opening --------------------------------------------------------------------------


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


# --- the daily cap --------------------------------------------------------------------


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


# --- closing --------------------------------------------------------------------------


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


def test_success_cannot_freeze_for_ever(store: Store) -> None:
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


def test_success_may_freeze_until_a_retry_after(store: Store) -> None:
    row = open_at(store, T)
    freeze = Freeze(since=T, until=T + timedelta(days=30), status=200)
    store.close_request(
        row.id, now=T, outcome=OK, update=update(timedelta(minutes=1), freeze=freeze)
    )
    state = store.host_state(HOST, T)
    assert (state.freeze, state.failures) == (freeze, 0)


# --- freezing -------------------------------------------------------------------------


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


# --- reconciliation -------------------------------------------------------------------


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


def test_opening_closes_the_abandoned_rows_of_its_host(store: Store) -> None:
    mine = open_at(store, T)
    other = open_at(store, T, host="other.example")
    now = T + timedelta(hours=2)
    row = store.open_request(
        HOST,
        URL,
        now=now,
        daily_cap=CAP,
        cautious=lambda _s: now + timedelta(minutes=5),
        abandon_before=now - timedelta(hours=1),
    )
    closed = store.row(mine.id)
    assert closed.state is RowState.RECONCILED
    assert closed.closed_at == now
    assert closed.outcome == Outcome(ok=False, failure=ABANDONED)
    state = store.host_state(HOST, now)
    assert (state.in_flight, state.failures) == (1, 1)
    assert store.row(row.id).state is RowState.IN_FLIGHT
    # Another host's abandoned row waits for a request to that host.
    assert store.row(other.id).state is RowState.IN_FLIGHT


def test_a_refused_opening_still_closes_the_abandoned_rows(store: Store) -> None:
    abandoned = open_at(store, T, cap=1)
    now = T + timedelta(hours=2)
    with pytest.raises(Refused) as refused:
        store.open_request(
            HOST,
            URL,
            now=now,
            daily_cap=1,
            cautious=lambda _s: now + timedelta(minutes=5),
            abandon_before=now - timedelta(hours=1),
        )
    assert refused.value.reason is RefusalReason.DAILY_CAP
    assert store.row(abandoned.id).state is RowState.RECONCILED
    state = store.host_state(HOST, now)
    assert (state.in_flight, state.failures, state.requests_today) == (0, 1, 1)


def test_an_abandoned_row_does_not_block_the_opening(store: Store) -> None:
    open_at(store, T)
    now = T + timedelta(hours=2)
    store.open_request(
        HOST,
        URL,
        now=now,
        daily_cap=CAP,
        cautious=lambda _s: now + timedelta(minutes=5),
        abandon_before=now - timedelta(hours=1),
    )


# --- a late close: the answer came after the reconciliation (R1) ----------------------

LATE = T + timedelta(hours=3)


def reconciled_row(
    store: Store, cautious: timedelta = timedelta(minutes=5)
) -> RequestRow:
    row = open_at(store, T, cautious=cautious)
    store.reconcile(now=T + timedelta(hours=2), older_than=timedelta(hours=1))
    return row


def test_a_late_good_answer_loosens_nothing(store: Store) -> None:
    row = reconciled_row(store, cautious=timedelta(hours=10))
    late = close_at(store, row, LATE, OK, next_in=timedelta(seconds=1))
    state = store.host_state(HOST, LATE)
    assert state.not_before == T + timedelta(hours=10)
    assert state.failures == 1
    assert late.state is RowState.RECONCILED
    assert (late.closed_at, late.outcome) == (LATE, OK)
    assert store.row(row.id) == late


def test_a_late_failure_may_lengthen_the_promise(store: Store) -> None:
    row = reconciled_row(store)
    close_at(store, row, LATE, FAIL, next_in=timedelta(hours=1))
    state = store.host_state(HOST, LATE)
    assert state.not_before == LATE + timedelta(hours=1)
    assert state.not_before_reason is RefusalReason.BACKOFF
    assert state.failures == 1  # the reconciliation already counted this request


def test_a_late_403_freezes_the_host(store: Store) -> None:
    row = reconciled_row(store)
    freeze = Freeze(since=LATE, status=403, note="HTTP 403")
    store.close_request(
        row.id,
        now=LATE,
        outcome=Outcome(ok=False, status=403, failure="http"),
        update=update(timedelta(minutes=1), RefusalReason.BACKOFF, freeze, base=LATE),
    )
    assert store.host_state(HOST, LATE).freeze == freeze
    with pytest.raises(Refused) as refused:
        open_at(store, LATE + timedelta(days=30))
    assert refused.value.reason is RefusalReason.FROZEN


def test_a_late_429_counts_as_throttled(store: Store) -> None:
    row = reconciled_row(store)
    close_at(store, row, LATE, Outcome(ok=False, status=429, failure="http"))
    assert store.host_state(HOST, LATE).throttled == 1


def test_a_late_close_keeps_the_longer_freeze(store: Store) -> None:
    row = reconciled_row(store)
    for_ever = Freeze(since=T + timedelta(hours=2), status=403)
    freeze_host(store, for_ever, now=T + timedelta(hours=2))
    store.close_request(
        row.id,
        now=LATE,
        outcome=Outcome(ok=False, status=429, failure="http"),
        update=update(
            timedelta(minutes=1),
            RefusalReason.BACKOFF,
            Freeze(since=LATE, until=LATE + timedelta(hours=1), status=429),
            base=LATE,
        ),
    )
    assert store.host_state(HOST, LATE).freeze == for_ever


def test_a_second_late_close_changes_nothing(store: Store) -> None:
    row = reconciled_row(store)
    first = close_at(store, row, LATE, FAIL, next_in=timedelta(minutes=1))
    before = snapshot(store, LATE)
    second = store.close_request(
        row.id,
        now=LATE + timedelta(hours=1),
        outcome=Outcome(ok=False, status=403, failure="http"),
        update=update(
            timedelta(days=1), RefusalReason.BACKOFF, Freeze(since=LATE), base=LATE
        ),
    )
    assert second == first
    assert snapshot(store, LATE) == before


def test_a_late_close_adds_bytes_and_never_removes_them(store: Store) -> None:
    answer(store, T - timedelta(hours=1), Outcome(ok=True, status=200, wire_bytes=1000))
    small = reconciled_row(store)  # reserves 1000
    close_at(store, small, LATE, Outcome(ok=False, failure="reset", wire_bytes=10))
    assert store.host_state(HOST, T).wire_bytes_today == 2000
    big = open_at(store, LATE + timedelta(hours=1), cap=100)  # reserves 1000 more
    store.reconcile(now=LATE + timedelta(hours=3), older_than=timedelta(hours=1))
    close_at(
        store,
        big,
        LATE + timedelta(hours=4),
        Outcome(ok=False, failure="reset", wire_bytes=5000, decoded_bytes=7000),
    )
    state = store.host_state(HOST, T)
    assert state.wire_bytes_today == 2000 + 5000
    assert state.decoded_bytes_today == 7000


def test_a_late_close_teaches_no_validators(store: Store) -> None:
    row = reconciled_row(store)
    store.close_request(
        row.id,
        now=LATE,
        outcome=Outcome(ok=True, status=200, wire_bytes=10),
        update=update(timedelta(minutes=1), base=LATE),
        validators=Validators(etag='"late"'),
    )
    assert store.url_record(URL) is None


def test_reconcile_threshold_must_be_positive(store: Store) -> None:
    for bad in (timedelta(0), timedelta(seconds=-1)):
        with pytest.raises(ValueError, match="older_than"):
            store.reconcile(now=T, older_than=bad)


def test_reconcile_with_nothing_open_does_nothing(store: Store) -> None:
    assert store.reconcile(now=T, older_than=timedelta(hours=1)) == []


# --- persistence ----------------------------------------------------------------------


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


# --- queries and validation -----------------------------------------------------------


def test_rows_filter_by_host_and_state(store: Store) -> None:
    a = open_at(store, T, host="a.example")
    b = open_at(store, T, host="b.example")
    a = close_at(store, a, T)
    assert store.rows(host="a.example") == [a]
    assert store.rows(state=RowState.IN_FLIGHT) == [b]
    assert store.rows(host="a.example", state=RowState.IN_FLIGHT) == []
    assert [r.id for r in store.rows()] == sorted([a.id, b.id])


def test_rows_latest_keeps_the_newest_oldest_first(store: Store) -> None:
    made = []
    for i in range(4):
        moment = T + timedelta(minutes=10 * i)
        made.append(close_at(store, open_at(store, moment), moment))
    other = open_at(store, T + timedelta(hours=1), host="b.example")
    assert store.rows(latest=2) == [made[3], other]
    assert store.rows(host=HOST, latest=3) == made[1:]
    assert store.rows(host=HOST, latest=10) == made
    assert store.rows(state=RowState.IN_FLIGHT, latest=1) == [other]
    for wrong in (0, -1):
        with pytest.raises(ValueError, match="latest"):
            store.rows(latest=wrong)


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


# --- 429 answers since the last good one (T2) -----------------------------------------


def close_with(store: Store, now: datetime, outcome: Outcome) -> None:
    row = open_at(store, now, cap=100)
    reason = RefusalReason.MIN_INTERVAL if outcome.ok else RefusalReason.BACKOFF
    store.close_request(
        row.id,
        now=now,
        outcome=outcome,
        update=lambda _s: HostUpdate(now + timedelta(seconds=1), reason),
    )


def test_throttled_counts_429_answers_since_the_last_good_one(store: Store) -> None:
    too_many = Outcome(ok=False, status=429, failure="http")
    steps = [
        (too_many, 1),
        (FAIL, 1),  # a timeout is not a 429, and is not a good answer either
        (Outcome(ok=False, status=503, failure="http"), 1),
        (too_many, 2),
        (OK, 0),
        (too_many, 1),
    ]
    now = T
    for outcome, expected in steps:
        close_with(store, now, outcome)
        assert store.host_state(HOST, now).throttled == expected
        now += timedelta(hours=1)


def test_reconcile_does_not_touch_throttled(store: Store) -> None:
    close_with(store, T, Outcome(ok=False, status=429, failure="http"))
    open_at(store, T + timedelta(hours=1))
    store.reconcile(now=T + timedelta(hours=3), older_than=timedelta(hours=1))
    assert store.host_state(HOST, T + timedelta(hours=3)).throttled == 1


def test_opening_keeps_throttled(store: Store) -> None:
    close_with(store, T, Outcome(ok=False, status=429, failure="http"))
    open_at(store, T + timedelta(hours=1))
    assert store.host_state(HOST, T + timedelta(hours=1)).throttled == 1


# --- manual unfreeze (T2) -------------------------------------------------------------


def freeze_host(store: Store, freeze: Freeze, now: datetime = T) -> None:
    row = open_at(store, now)
    store.close_request(
        row.id,
        now=now,
        outcome=Outcome(ok=False, status=freeze.status, failure="http"),
        update=update(timedelta(minutes=1), RefusalReason.BACKOFF, freeze, base=now),
    )


def test_unfreeze_lifts_the_freeze_and_leaves_a_trace(harness: Harness) -> None:
    store = harness.store
    freeze = Freeze(since=T, status=403, note="Forbidden")
    freeze_host(store, freeze)
    later = T + timedelta(days=2)
    trace = store.unfreeze(HOST, now=later, by="ops@example.org", note="asked them")
    assert trace is not None
    assert trace == Unfreeze(
        id=trace.id,
        host=HOST,
        at=later,
        by="ops@example.org",
        note="asked them",
        freeze=freeze,
    )
    state = store.host_state(HOST, later)
    assert state.freeze is None
    open_at(store, later)
    assert store.unfreezes() == [trace]
    assert harness.restart().unfreezes() == [trace]


def test_unfreeze_keeps_failures_and_promise(store: Store) -> None:
    freeze_host(store, Freeze(since=T, status=451))
    before = store.host_state(HOST, T)
    store.unfreeze(HOST, now=T, by="ops")
    after_unfreeze = store.host_state(HOST, T)
    assert after_unfreeze.failures == before.failures
    assert after_unfreeze.not_before == before.not_before
    assert after_unfreeze.throttled == before.throttled


def test_unfreeze_of_a_host_not_frozen_does_nothing(store: Store) -> None:
    open_at(store, T)
    assert store.unfreeze(HOST, now=T, by="ops") is None
    assert store.unfreeze("never-seen.example", now=T, by="ops") is None
    assert store.unfreezes() == []


def test_unfreeze_removes_an_expired_freeze_too(store: Store) -> None:
    freeze = Freeze(since=T, until=T + timedelta(hours=1), status=429)
    freeze_host(store, freeze)
    trace = store.unfreeze(HOST, now=T + timedelta(hours=2), by="ops")
    assert trace is not None
    assert trace.freeze == freeze
    assert store.host_state(HOST, T + timedelta(hours=2)).freeze is None


def test_unfreeze_needs_someone(store: Store) -> None:
    freeze_host(store, Freeze(since=T, status=403))
    with pytest.raises(ValueError, match="by"):
        store.unfreeze(HOST, now=T, by="  ")
    assert store.host_state(HOST, T).freeze is not None


def test_unfreezes_filter_by_host(store: Store) -> None:
    for host in ("a.example", "b.example"):
        row = open_at(store, T, host=host)
        store.close_request(
            row.id,
            now=T,
            outcome=Outcome(ok=False, status=403, failure="http"),
            update=update(timedelta(1), RefusalReason.BACKOFF, Freeze(T, None, 403)),
        )
        store.unfreeze(host, now=T, by="ops")
    assert [u.host for u in store.unfreezes(host="b.example")] == ["b.example"]
    assert [u.host for u in store.unfreezes()] == ["a.example", "b.example"]


# --- validators per URL (T4) ----------------------------------------------------------


def answer(
    store: Store,
    now: datetime,
    outcome: Outcome,
    validators: Validators | None = None,
    *,
    url: str = URL,
    cap: int = 100,
    budget: int | None = None,
) -> RequestRow:
    row = store.open_request(
        HOST,
        url,
        now=now,
        daily_cap=cap,
        byte_budget=budget,
        cautious=lambda _s: now + timedelta(minutes=5),
    )
    reason = RefusalReason.MIN_INTERVAL if outcome.ok else RefusalReason.BACKOFF
    return store.close_request(
        row.id,
        now=now,
        outcome=outcome,
        update=lambda _s: HostUpdate(now + timedelta(seconds=1), reason),
        validators=validators,
    )


TAG = Validators(etag='"v1"', last_modified="Fri, 16 Oct 2026 10:00:00 GMT")


def test_unknown_url_has_no_record(store: Store) -> None:
    assert store.url_record(URL) is None


def test_a_full_answer_saves_validators_and_size(store: Store) -> None:
    answer(
        store, T, Outcome(ok=True, status=200, wire_bytes=1200, decoded_bytes=5000), TAG
    )
    assert store.url_record(URL) == UrlRecord(
        url=URL, validators=TAG, size=1200, updated_at=T
    )


def test_not_modified_keeps_validators_and_size(store: Store) -> None:
    answer(store, T, Outcome(ok=True, status=200, wire_bytes=1200), TAG)
    later = T + timedelta(hours=1)
    answer(store, later, Outcome(ok=True, status=304, wire_bytes=0), None)
    assert store.url_record(URL) == UrlRecord(
        url=URL, validators=TAG, size=1200, updated_at=T
    )


def test_not_modified_may_refresh_one_validator(store: Store) -> None:
    answer(store, T, Outcome(ok=True, status=200, wire_bytes=1200), TAG)
    later = T + timedelta(hours=1)
    answer(store, later, Outcome(ok=True, status=304), Validators(etag='"v2"'))
    record = store.url_record(URL)
    assert record is not None
    assert record.validators == Validators(etag='"v2"', last_modified=TAG.last_modified)
    assert record.size == 1200
    assert record.updated_at == later


def test_a_full_answer_without_validators_forgets_the_old_ones(store: Store) -> None:
    answer(store, T, Outcome(ok=True, status=200, wire_bytes=1200), TAG)
    later = T + timedelta(hours=1)
    answer(store, later, Outcome(ok=True, status=200, wire_bytes=900), None)
    assert store.url_record(URL) == UrlRecord(
        url=URL, validators=Validators(), size=900, updated_at=later
    )


@pytest.mark.parametrize(
    "outcome",
    [
        Outcome(ok=False, status=503, failure="http"),
        Outcome(ok=False, failure="reset"),
        Outcome(ok=True, status=204),
    ],
)
def test_other_answers_leave_the_record_alone(store: Store, outcome: Outcome) -> None:
    answer(store, T, Outcome(ok=True, status=200, wire_bytes=1200), TAG)
    answer(store, T + timedelta(hours=1), outcome, Validators(etag='"other"'))
    assert store.url_record(URL) == UrlRecord(
        url=URL, validators=TAG, size=1200, updated_at=T
    )


def test_url_records_survive_a_restart(harness: Harness) -> None:
    answer(harness.store, T, Outcome(ok=True, status=200, wire_bytes=1200), TAG)
    assert harness.restart().url_record(URL) == UrlRecord(
        url=URL, validators=TAG, size=1200, updated_at=T
    )


# --- bytes per host and day (T4) ------------------------------------------------------


def test_bytes_are_counted_per_host_and_day(store: Store) -> None:
    answer(store, T, Outcome(ok=True, status=200, wire_bytes=1000, decoded_bytes=4000))
    answer(
        store,
        T + timedelta(hours=1),
        Outcome(ok=True, status=200, wire_bytes=500, decoded_bytes=2000),
        url="https://example.org/other",
    )
    state = store.host_state(HOST, T)
    assert state.wire_bytes_today == 1500
    assert state.decoded_bytes_today == 6000
    assert store.host_state(HOST, T + timedelta(days=1)).wire_bytes_today == 0


def test_rows_carry_their_bytes(store: Store) -> None:
    row = answer(
        store, T, Outcome(ok=True, status=200, wire_bytes=1000, decoded_bytes=4000)
    )
    assert row.outcome is not None
    assert (row.outcome.wire_bytes, row.outcome.decoded_bytes) == (1000, 4000)
    assert store.row(row.id) == row


def test_opening_reserves_the_last_full_size(store: Store) -> None:
    answer(store, T, Outcome(ok=True, status=200, wire_bytes=1000))
    row = open_at(store, T + timedelta(hours=1), cap=100)
    assert row.reserved_bytes == 1000
    assert (
        store.host_state(HOST, T).wire_bytes_today == 2000
    )  # 1000 used + 1000 reserved


def test_closing_replaces_the_reservation_with_the_real_bytes(store: Store) -> None:
    answer(store, T, Outcome(ok=True, status=200, wire_bytes=1000))
    answer(store, T + timedelta(hours=1), Outcome(ok=True, status=304, wire_bytes=0))
    assert store.host_state(HOST, T).wire_bytes_today == 1000


def test_a_close_without_bytes_keeps_the_reservation(store: Store) -> None:
    answer(store, T, Outcome(ok=True, status=200, wire_bytes=1000))
    answer(store, T + timedelta(hours=1), Outcome(ok=False, failure="timeout"))
    assert store.host_state(HOST, T).wire_bytes_today == 2000


def test_a_reconciled_row_keeps_its_reservation(store: Store) -> None:
    answer(store, T, Outcome(ok=True, status=200, wire_bytes=1000))
    open_at(store, T + timedelta(hours=1), cap=100)
    store.reconcile(now=T + timedelta(hours=3), older_than=timedelta(hours=1))
    assert store.host_state(HOST, T).wire_bytes_today == 2000


def test_bytes_count_on_the_day_the_request_opened(store: Store) -> None:
    late = datetime(2026, 10, 16, 23, 59, tzinfo=UTC)
    row = open_at(store, late, cap=100)
    store.close_request(
        row.id,
        now=late + timedelta(minutes=2),
        outcome=Outcome(ok=True, status=200, wire_bytes=700),
        update=update(timedelta(1), base=late),
    )
    assert store.host_state(HOST, late).wire_bytes_today == 700
    assert store.host_state(HOST, late + timedelta(minutes=2)).wire_bytes_today == 0


def test_byte_budget_refuses_once_used_up(store: Store) -> None:
    answer(
        store, T, Outcome(ok=True, status=200, wire_bytes=1000), url="https://e.org/a"
    )
    before = snapshot(store, T + timedelta(hours=1))
    with pytest.raises(Refused) as refused:
        store.open_request(
            HOST,
            "https://example.org/new",
            now=T + timedelta(hours=1),
            daily_cap=100,
            byte_budget=1000,
            cautious=after(timedelta(hours=2)),
        )
    assert refused.value.reason is RefusalReason.BYTE_BUDGET
    assert refused.value.retry_after == datetime(2026, 10, 17, tzinfo=UTC)
    # Nothing written but the budget it asked for, which the first request
    # did not have (#31).
    state, rows = snapshot(store, T + timedelta(hours=1))
    assert state.lowest_byte_budget == 1000
    assert (replace(state, lowest_byte_budget=None), rows) == before


def test_byte_budget_refuses_a_reservation_that_does_not_fit(store: Store) -> None:
    answer(store, T, Outcome(ok=True, status=200, wire_bytes=600))
    with pytest.raises(Refused) as refused:
        answer(store, T + timedelta(hours=1), OK, budget=1000)  # 600 used + 600 > 1000
    assert refused.value.reason is RefusalReason.BYTE_BUDGET
    answer(
        store, T + timedelta(hours=1), OK, url="https://example.org/small", budget=1000
    )


def test_no_budget_means_no_byte_limit(store: Store) -> None:
    answer(store, T, Outcome(ok=True, status=200, wire_bytes=10**9))
    answer(store, T + timedelta(hours=1), OK)


def test_negative_byte_budget_is_rejected(store: Store) -> None:
    with pytest.raises(ValueError, match="byte_budget"):
        answer(store, T, OK, budget=-1)


def test_negative_bytes_are_rejected() -> None:
    with pytest.raises(ValueError, match="bytes"):
        Outcome(ok=True, status=200, wire_bytes=-1)


# --- data times, for stuck sources (T3) -----------------------------------------------


def test_unknown_url_has_no_data_time(store: Store) -> None:
    assert store.data_time_record(URL) is None


def test_first_data_time_is_one_read(store: Store) -> None:
    data_time = T - timedelta(hours=1)
    first = DataTimeRecord(url=URL, latest=data_time, reads=1, updated_at=T, since=T)
    assert store.note_data_time(URL, data_time, now=T) == first
    assert store.data_time_record(URL) == first


def test_the_same_data_time_again_counts_reads(store: Store) -> None:
    data_time = T - timedelta(hours=1)
    for reads in (1, 2, 3):
        record = store.note_data_time(URL, data_time, now=T + timedelta(minutes=reads))
        assert record.reads == reads
        assert record.latest == data_time
        assert record.since == T + timedelta(minutes=1)  # the read that brought it


def test_an_older_data_time_is_not_progress(store: Store) -> None:
    store.note_data_time(URL, T, now=T)
    record = store.note_data_time(
        URL, T - timedelta(days=1), now=T + timedelta(minutes=1)
    )
    assert (record.latest, record.reads) == (T, 2)


def test_a_newer_data_time_starts_over(store: Store) -> None:
    store.note_data_time(URL, T, now=T)
    store.note_data_time(URL, T, now=T + timedelta(minutes=1))
    record = store.note_data_time(
        URL, T + timedelta(hours=1), now=T + timedelta(hours=1)
    )
    assert (record.latest, record.reads) == (T + timedelta(hours=1), 1)
    assert record.since == T + timedelta(hours=1)


def test_data_times_are_per_url(store: Store) -> None:
    store.note_data_time(URL, T, now=T)
    store.note_data_time(URL, T, now=T)
    assert store.note_data_time("https://example.org/other", T, now=T).reads == 1


def test_data_times_survive_a_restart(harness: Harness) -> None:
    harness.store.note_data_time(URL, T, now=T)
    harness.store.note_data_time(URL, T, now=T + timedelta(minutes=1))
    record = harness.restart().data_time_record(URL)
    assert record == DataTimeRecord(
        url=URL, latest=T, reads=2, updated_at=T + timedelta(minutes=1), since=T
    )


def test_naive_data_times_are_rejected(store: Store) -> None:
    with pytest.raises(ValueError, match="timezone"):
        store.note_data_time(URL, datetime(2026, 10, 16), now=T)  # noqa: DTZ001
