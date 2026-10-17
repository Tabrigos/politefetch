"""The gate: scenarios with a fake clock, on every store."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from politefetch.clock import FakeClock
from politefetch.errors import RefusalReason, Refused
from politefetch.gate import Gate
from politefetch.policy import HostPolicy
from politefetch.store import Outcome, RowState

from .stores import KINDS, Harness

T = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
URL = "https://example.org/data.json"
HOST = "example.org"


@pytest.fixture(params=KINDS)
def harness(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Harness]:
    h = Harness(request.param, tmp_path)
    yield h
    h.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T)


def make_gate(harness: Harness, clock: FakeClock, **policy: object) -> Gate:
    settings: dict[str, object] = {
        "daily_cap": 100,
        "min_interval": "10s",
        "backoff_base": "1m",
        "backoff_max": "1h",
        **policy,
    }
    return Gate(
        harness.store,
        {HOST: HostPolicy(**settings)},  # type: ignore[arg-type]
        clock=clock,
        reconcile_after=timedelta(hours=2),
    )


def promise(gate: Gate, clock: FakeClock) -> datetime | None:
    return gate.store.host_state(HOST, clock.now()).not_before


def respond(gate: Gate, status: int, retry_after: str | None = None) -> None:
    with gate.attempt(URL) as attempt:
        attempt.response(status, retry_after=retry_after)


def fail(gate: Gate, shape: str = "connect_timeout") -> None:
    with gate.attempt(URL) as attempt:
        attempt.failure(shape)


# --- the basic cycle ------------------------------------------------------------------


def test_an_allowed_request_opens_and_closes_a_row(
    harness: Harness, clock: FakeClock
) -> None:
    gate = make_gate(harness, clock)
    assert gate.may_fetch(URL)
    with gate.attempt(URL) as attempt:
        assert attempt.row.state is RowState.IN_FLIGHT
        assert attempt.row.url == URL
        clock.advance(timedelta(seconds=2))
        attempt.response(200)
    assert attempt.result is not None
    assert attempt.result.state is RowState.CLOSED
    assert attempt.result.outcome == Outcome(ok=True, status=200)
    assert attempt.result.closed_at == T + timedelta(seconds=2)
    # The minimum interval runs from the end of the request.
    assert promise(gate, clock) == T + timedelta(seconds=12)


def test_a_refused_attempt_never_runs_the_body(
    harness: Harness, clock: FakeClock
) -> None:
    gate = make_gate(harness, clock)
    respond(gate, 200)
    ran = False
    with pytest.raises(Refused) as refused, gate.attempt(URL):
        ran = True
    assert not ran
    assert refused.value.reason is RefusalReason.MIN_INTERVAL
    assert refused.value.retry_after == T + timedelta(seconds=10)


def test_may_fetch_explains_a_refusal(harness: Harness, clock: FakeClock) -> None:
    gate = make_gate(harness, clock)
    respond(gate, 200)
    verdict = gate.may_fetch(URL)
    assert not verdict
    assert verdict.refusal is not None
    assert verdict.refusal.reason is RefusalReason.MIN_INTERVAL
    clock.advance(timedelta(seconds=10))
    verdict = gate.may_fetch(URL)
    assert verdict
    assert verdict.refusal is None


def test_may_fetch_writes_no_request(harness: Harness, clock: FakeClock) -> None:
    gate = make_gate(harness, clock)
    for _ in range(5):
        gate.may_fetch(URL)
    assert gate.store.rows() == []
    assert gate.store.host_state(HOST, T).requests_today == 0


def test_daily_cap_through_the_gate(harness: Harness, clock: FakeClock) -> None:
    gate = make_gate(harness, clock, daily_cap=2)
    for _ in range(2):
        respond(gate, 200)
        clock.advance(timedelta(seconds=10))
    with pytest.raises(Refused) as refused, gate.attempt(URL):
        pass
    assert refused.value.reason is RefusalReason.DAILY_CAP


# --- scenarios required by T2 ---------------------------------------------------------


def test_the_pause_grows(harness: Harness, clock: FakeClock) -> None:
    gate = make_gate(
        harness, clock, min_interval="0s", backoff_base="1m", backoff_max="10m"
    )
    pauses = []
    for _ in range(6):
        fail(gate)
        until = promise(gate, clock)
        assert until is not None
        pauses.append(until - clock.now())
        clock.set(until)
    assert pauses == [timedelta(minutes=m) for m in (1, 2, 4, 8, 10, 10)]


def test_a_good_answer_shortens_the_promise(harness: Harness, clock: FakeClock) -> None:
    gate = make_gate(harness, clock, min_interval="10s", backoff_base="1h")
    with gate.attempt(URL) as attempt:
        assert promise(gate, clock) == T + timedelta(hours=1)  # cautious
        clock.advance(timedelta(seconds=2))
        attempt.response(200)
    assert promise(gate, clock) == T + timedelta(seconds=12)


def test_a_crash_leaves_the_promise_cautious(
    harness: Harness, clock: FakeClock
) -> None:
    gate = make_gate(harness, clock, min_interval="10s", backoff_base="1h")
    context = gate.attempt(URL)
    context.__enter__()  # the process dies inside the block: no exit, no close
    restarted = Gate(
        harness.restart(),
        {HOST: HostPolicy(daily_cap=100, min_interval="10s", backoff_base="1h")},
        clock=clock,
        reconcile_after=timedelta(hours=2),
    )
    clock.advance(timedelta(minutes=30))
    refusal = restarted.may_fetch(URL).refusal
    assert refusal is not None
    assert refusal.reason is RefusalReason.IN_FLIGHT
    assert refusal.retry_after == T + timedelta(hours=1)
    # Past the reconciliation threshold the row counts as failed.
    clock.set(T + timedelta(hours=2))
    assert restarted.may_fetch(URL)
    state = restarted.store.host_state(HOST, clock.now())
    assert state.in_flight == 0
    assert state.failures == 1
    assert state.not_before == T + timedelta(hours=1)
    assert restarted.store.rows()[0].state is RowState.RECONCILED


# --- how outcomes are recorded --------------------------------------------------------


def test_an_exception_is_recorded_as_a_failure(
    harness: Harness, clock: FakeClock
) -> None:
    gate = make_gate(harness, clock)
    with pytest.raises(TimeoutError), gate.attempt(URL) as attempt:
        raise TimeoutError("read timed out")
    assert attempt.result is not None
    assert attempt.result.outcome == Outcome(ok=False, failure="exception:TimeoutError")
    assert gate.store.host_state(HOST, T).failures == 1


def test_an_unrecorded_attempt_counts_as_failed(
    harness: Harness, clock: FakeClock
) -> None:
    gate = make_gate(harness, clock)
    with gate.attempt(URL) as attempt:
        pass
    assert attempt.result is not None
    assert attempt.result.outcome == Outcome(ok=False, failure="unrecorded")


def test_an_outcome_is_recorded_once(harness: Harness, clock: FakeClock) -> None:
    gate = make_gate(harness, clock)
    with gate.attempt(URL) as attempt:
        attempt.response(200)
        with pytest.raises(RuntimeError, match="already"):
            attempt.failure("reset")
    assert attempt.result is not None
    assert attempt.result.outcome == Outcome(ok=True, status=200)


def test_http_errors_are_failures(harness: Harness, clock: FakeClock) -> None:
    gate = make_gate(harness, clock)
    respond(gate, 503)
    row = gate.store.rows()[0]
    assert row.outcome == Outcome(ok=False, status=503, failure="http")


def test_not_modified_is_a_good_answer(harness: Harness, clock: FakeClock) -> None:
    gate = make_gate(harness, clock)
    respond(gate, 304)
    assert gate.store.rows()[0].outcome == Outcome(ok=True, status=304)


def test_retry_after_is_a_minimum(harness: Harness, clock: FakeClock) -> None:
    gate = make_gate(harness, clock, backoff_base="1m")
    respond(gate, 503, retry_after="3600")
    assert promise(gate, clock) == T + timedelta(hours=1)


def test_retry_after_as_a_date(harness: Harness, clock: FakeClock) -> None:
    gate = make_gate(harness, clock, backoff_base="1m")
    respond(gate, 503, retry_after="Fri, 16 Oct 2026 15:00:00 GMT")
    assert promise(gate, clock) == datetime(2026, 10, 16, 15, tzinfo=UTC)


# --- freezing and unfreezing ----------------------------------------------------------


@pytest.mark.parametrize("status", [403, 451, 429])
def test_these_answers_freeze_until_a_person_unlocks(
    harness: Harness, clock: FakeClock, status: int
) -> None:
    gate = make_gate(harness, clock)
    respond(gate, status, retry_after="60")
    clock.advance(timedelta(days=30))
    refusal = gate.may_fetch(URL).refusal
    assert refusal is not None
    assert refusal.reason is RefusalReason.FROZEN
    assert refusal.retry_after is None
    trace = gate.unfreeze(HOST, by="ops@example.org", note="talked to them")
    assert trace is not None
    assert trace.freeze.status == status
    assert trace.at == clock.now()
    assert gate.may_fetch(URL)


def test_unfreeze_accepts_a_url(harness: Harness, clock: FakeClock) -> None:
    gate = make_gate(harness, clock)
    respond(gate, 403)
    assert gate.unfreeze(URL, by="ops") is not None


def test_freeze_with_expiry(harness: Harness, clock: FakeClock) -> None:
    gate = make_gate(harness, clock, freeze_for="6h")
    respond(gate, 429, retry_after="60")
    clock.advance(timedelta(hours=5, minutes=59))
    assert not gate.may_fetch(URL)
    clock.advance(timedelta(minutes=1))
    assert gate.may_fetch(URL)


def test_option_respects_retry_after_once_then_freezes(
    harness: Harness, clock: FakeClock
) -> None:
    gate = make_gate(harness, clock, respect_retry_after_429=True)
    respond(gate, 429, retry_after="1800")
    refusal = gate.may_fetch(URL).refusal
    assert refusal is not None
    assert refusal.reason is RefusalReason.BACKOFF
    assert refusal.retry_after == T + timedelta(minutes=30)
    clock.set(T + timedelta(minutes=30))
    respond(gate, 429, retry_after="1800")
    refusal = gate.may_fetch(URL).refusal
    assert refusal is not None
    assert refusal.reason is RefusalReason.FROZEN


def test_option_starts_over_after_a_good_answer(
    harness: Harness, clock: FakeClock
) -> None:
    gate = make_gate(harness, clock, respect_retry_after_429=True)
    respond(gate, 429, retry_after="60")
    clock.advance(timedelta(minutes=1))
    respond(gate, 200)
    clock.advance(timedelta(minutes=1))
    respond(gate, 429, retry_after="60")
    refusal = gate.may_fetch(URL).refusal
    assert refusal is not None
    assert refusal.reason is not RefusalReason.FROZEN


# --- hosts and policies ---------------------------------------------------------------


def test_unknown_hosts_are_not_allowed(harness: Harness, clock: FakeClock) -> None:
    gate = make_gate(harness, clock)
    with pytest.raises(ValueError, match="no policy"):
        gate.may_fetch("https://elsewhere.example/")
    with (
        pytest.raises(ValueError, match="no policy"),
        gate.attempt("https://elsewhere.example/"),
    ):
        pass
    assert gate.store.rows() == []


def test_a_default_policy_covers_unknown_hosts(
    harness: Harness, clock: FakeClock
) -> None:
    gate = Gate(
        harness.store,
        {},
        clock=clock,
        default=HostPolicy(daily_cap=1, min_interval="1m"),
    )
    with gate.attempt("https://elsewhere.example/") as attempt:
        attempt.response(200)
    assert not gate.may_fetch("https://elsewhere.example/other")


def test_hosts_are_normalised(harness: Harness, clock: FakeClock) -> None:
    gate = make_gate(harness, clock)
    with gate.attempt("HTTPS://Example.ORG:8443/x") as attempt:
        attempt.response(200)
    assert attempt.row.host == HOST


def test_a_url_without_host_is_rejected(harness: Harness, clock: FakeClock) -> None:
    gate = make_gate(harness, clock)
    with pytest.raises(ValueError, match="host"):
        gate.may_fetch("/relative/path")


def test_policy_keys_are_normalised(harness: Harness, clock: FakeClock) -> None:
    gate = Gate(
        harness.store,
        {"Example.Org": HostPolicy(daily_cap=1, min_interval="1m")},
        clock=clock,
    )
    assert gate.may_fetch(URL)


def test_reconcile_threshold_must_be_positive(
    harness: Harness, clock: FakeClock
) -> None:
    with pytest.raises(ValueError, match="reconcile_after"):
        Gate(harness.store, {}, clock=clock, reconcile_after=timedelta(0))


# --- async ----------------------------------------------------------------------------


def test_async_attempt(harness: Harness, clock: FakeClock) -> None:
    gate = make_gate(harness, clock)

    async def main() -> None:
        async with gate.attempt(URL) as attempt:
            await asyncio.sleep(0)
            attempt.response(200)
        assert attempt.result is not None
        assert attempt.result.state is RowState.CLOSED

    asyncio.run(main())
    assert promise(gate, clock) == T + timedelta(seconds=10)


def test_async_exception_is_recorded(harness: Harness, clock: FakeClock) -> None:
    gate = make_gate(harness, clock)

    async def main() -> None:
        async with gate.attempt(URL):
            raise ConnectionResetError

    with pytest.raises(ConnectionResetError):
        asyncio.run(main())
    assert gate.store.rows()[0].outcome == Outcome(
        ok=False, failure="exception:ConnectionResetError"
    )


def test_async_cancellation_leaves_the_row_to_reconciliation(
    harness: Harness, clock: FakeClock
) -> None:
    gate = make_gate(harness, clock)
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

    asyncio.run(main())
    row = gate.store.rows()[0]
    assert row.state is RowState.IN_FLIGHT
    clock.advance(timedelta(hours=2))
    gate.reconcile()
    assert gate.store.row(row.id).state is RowState.RECONCILED


def test_the_package_exports_the_gate() -> None:
    import politefetch

    assert politefetch.Gate is Gate
    assert politefetch.HostPolicy is HostPolicy


# --- conditional requests and bytes (T4) ----------------------------------------------


LAST_MODIFIED = "Fri, 16 Oct 2026 10:00:00 GMT"


def test_validators_come_back_as_conditional_headers(
    harness: Harness, clock: FakeClock
) -> None:
    gate = make_gate(harness, clock)
    with gate.attempt(URL) as attempt:
        assert attempt.conditional_headers == {}
        attempt.response(200, headers={"etag": '"v1"', "Last-Modified": LAST_MODIFIED})
    clock.advance(timedelta(minutes=1))
    with gate.attempt(URL) as attempt:
        assert attempt.conditional_headers == {
            "If-None-Match": '"v1"',
            "If-Modified-Since": LAST_MODIFIED,
        }
        attempt.response(304)
    clock.advance(timedelta(minutes=1))
    with gate.attempt("https://example.org/other") as attempt:
        assert attempt.conditional_headers == {}
        attempt.response(200)


def test_retry_after_is_read_from_headers(harness: Harness, clock: FakeClock) -> None:
    gate = make_gate(harness, clock, backoff_base="1m")
    with gate.attempt(URL) as attempt:
        attempt.response(503, headers={"retry-after": "3600"})
    assert promise(gate, clock) == T + timedelta(hours=1)


def test_bytes_are_recorded(harness: Harness, clock: FakeClock) -> None:
    gate = make_gate(harness, clock)
    with gate.attempt(URL) as attempt:
        attempt.response(200, wire_bytes=300, decoded_bytes=1200)
    clock.advance(timedelta(minutes=1))
    with gate.attempt(URL) as attempt:
        attempt.failure("truncated", wire_bytes=50)
    state = gate.store.host_state(HOST, clock.now())
    assert state.wire_bytes_today == 350
    assert state.decoded_bytes_today == 1200


def test_byte_budget_through_the_gate(harness: Harness, clock: FakeClock) -> None:
    gate = make_gate(harness, clock, byte_budget="1kB")
    with gate.attempt(URL) as attempt:
        attempt.response(200, wire_bytes=600)
    clock.advance(timedelta(minutes=1))
    refusal = gate.may_fetch(URL).refusal  # 600 used + 600 reserved > 1000
    assert refusal is not None
    assert refusal.reason is RefusalReason.BYTE_BUDGET
    with pytest.raises(Refused), gate.attempt(URL):
        pass
    assert gate.may_fetch("https://example.org/small")  # nothing to reserve


# --- data times (T3) ------------------------------------------------------------------


def test_the_gate_notes_data_times_with_its_clock(
    harness: Harness, clock: FakeClock
) -> None:
    gate = make_gate(harness, clock)
    data_time = T - timedelta(hours=1)
    assert gate.note_data_time(URL, data_time).reads == 1
    clock.advance(timedelta(minutes=10))
    record = gate.note_data_time(URL, data_time)
    assert record.reads == 2
    assert record.updated_at == clock.now()
