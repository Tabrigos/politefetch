"""`wait_up_to`: a short, bounded wait instead of a refusal (#14, D-061).

A logical call made of a few GETs seconds apart may wait for the gate, but
only for `min_interval`, and only within the bound counted from the first
refusal: every other refusal comes at once. The waits go through the clock,
so these tests do not wait.
"""

import asyncio
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from politefetch.clock import FakeClock, SleepingClock, SystemClock
from politefetch.errors import RefusalReason, Refused
from politefetch.gate import Attempt, Gate
from politefetch.policy import HostPolicy
from politefetch.store import MemoryStore, RowState
from politefetch.testing.simulator import Respond, SourceSimulator

from .adapter_kit import KINDS as FETCHERS
from .adapter_kit import getter
from .stores import KINDS, Harness

T = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
URL = "https://example.org/data.json"
OTHER = "https://example.org/other.json"
HOST = "example.org"


class RecordingClock(FakeClock):
    """A fake clock that records its sleeps and can run something first."""

    def __init__(self, start: datetime) -> None:
        super().__init__(start)
        self.sleeps: list[timedelta] = []
        self.before_sleep: Callable[[timedelta], None] | None = None

    def sleep(self, delta: timedelta) -> None:
        self.sleeps.append(delta)
        if self.before_sleep is not None:
            self.before_sleep(delta)
        super().sleep(delta)

    async def sleep_async(self, delta: timedelta) -> None:
        self.sleeps.append(delta)
        await super().sleep_async(delta)


class NowOnlyClock:
    """A clock written by hand, as a caller may have: it cannot sleep."""

    def now(self) -> datetime:
        return T


@pytest.fixture(params=KINDS)
def harness(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Harness]:
    h = Harness(request.param, tmp_path)
    yield h
    h.close()


@pytest.fixture
def clock() -> RecordingClock:
    return RecordingClock(T)


def make_gate(harness: Harness, clock: object, **policy: object) -> Gate:
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
        clock=clock,  # type: ignore[arg-type]
        reconcile_after=timedelta(hours=2),
    )


def respond(gate: Gate, status: int = 200, url: str = URL, **outcome: object) -> None:
    with gate.attempt(url) as attempt:
        attempt.response(status, **outcome)  # type: ignore[arg-type]
        if attempt.outcome is not None and attempt.outcome.ok:
            attempt.accept()


def opened_at(gate: Gate) -> list[datetime]:
    return [row.opened_at for row in gate.store.rows()]


# --- sync -----------------------------------------------------------------------


def test_without_wait_up_to_a_request_too_soon_is_refused(
    harness: Harness, clock: RecordingClock
) -> None:
    gate = make_gate(harness, clock)
    respond(gate)
    clock.advance(timedelta(seconds=3))
    with pytest.raises(Refused) as refused:
        respond(gate)
    assert refused.value.reason is RefusalReason.MIN_INTERVAL
    assert clock.sleeps == []


def test_a_wait_within_the_bound_sends_when_the_interval_ends(
    harness: Harness, clock: RecordingClock
) -> None:
    gate = make_gate(harness, clock)
    respond(gate)
    clock.advance(timedelta(seconds=3))
    with gate.attempt(URL, wait_up_to="10s") as attempt:
        attempt.response(200)
    assert clock.sleeps == [timedelta(seconds=7)]
    assert opened_at(gate) == [T, T + timedelta(seconds=10)]
    assert attempt.result is not None
    assert attempt.result.state is RowState.CLOSED


def test_a_wait_as_long_as_the_bound_is_waited(
    harness: Harness, clock: RecordingClock
) -> None:
    gate = make_gate(harness, clock)
    respond(gate)
    with gate.attempt(URL, wait_up_to="10s") as attempt:
        attempt.response(200)
    assert clock.sleeps == [timedelta(seconds=10)]


def test_a_wait_longer_than_the_bound_refuses_at_once(
    harness: Harness, clock: RecordingClock
) -> None:
    gate = make_gate(harness, clock)
    respond(gate)
    with pytest.raises(Refused) as refused, gate.attempt(URL, wait_up_to="9s") as a:
        a.response(200)
    assert refused.value.reason is RefusalReason.MIN_INTERVAL
    assert clock.sleeps == []
    assert len(gate.store.rows()) == 1


def test_another_url_of_the_host_waits_for_the_host(
    harness: Harness, clock: RecordingClock
) -> None:
    gate = make_gate(harness, clock)
    respond(gate)
    with gate.attempt(OTHER, wait_up_to="1m") as attempt:
        attempt.response(200)
    assert clock.sleeps == [timedelta(seconds=10)]


def test_a_short_retry_after_is_waited_and_a_long_one_is_not(
    harness: Harness, clock: RecordingClock
) -> None:
    gate = make_gate(harness, clock)
    respond(gate, headers={"Retry-After": "30"})
    with pytest.raises(Refused) as refused, gate.attempt(URL, wait_up_to="20s") as a:
        a.response(200)
    assert refused.value.reason is RefusalReason.MIN_INTERVAL
    assert clock.sleeps == []
    with gate.attempt(URL, wait_up_to="30s") as attempt:
        attempt.response(200)
    assert clock.sleeps == [timedelta(seconds=30)]


def test_the_bound_counts_from_the_first_refusal(
    harness: Harness, clock: RecordingClock
) -> None:
    # Another process takes the host the moment the interval ends, so the
    # second ask is refused for another interval: 7 s waited, 10 s more.
    gate = make_gate(harness, clock)
    other = make_gate(harness, clock)
    respond(gate)
    clock.advance(timedelta(seconds=3))

    def another_process_goes_first(delta: timedelta) -> None:
        if len(clock.sleeps) == 1:
            clock.advance(delta)
            respond(other, url=OTHER)
            clock.set(clock.now() - delta)

    clock.before_sleep = another_process_goes_first
    with pytest.raises(Refused) as refused, gate.attempt(URL, wait_up_to="15s") as a:
        a.response(200)
    assert refused.value.reason is RefusalReason.MIN_INTERVAL
    assert clock.sleeps == [timedelta(seconds=7)]
    assert len(gate.store.rows()) == 2

    # With room for both waits, the same happens once more and it goes.
    clock.sleeps.clear()
    clock.advance(timedelta(seconds=3))
    with gate.attempt(URL, wait_up_to="20s") as attempt:
        attempt.response(200)
    assert clock.sleeps == [timedelta(seconds=7), timedelta(seconds=10)]
    assert opened_at(gate)[-1] == T + timedelta(seconds=30)


def freeze(gate: Gate) -> None:
    respond(gate, 429)


def back_off(gate: Gate) -> None:
    with gate.attempt(URL) as attempt:
        attempt.failure("connect")


def go_missing(gate: Gate) -> None:
    respond(gate, 404)


def spend_the_day(gate: Gate) -> None:
    respond(gate)


def spend_the_bytes(gate: Gate) -> None:
    respond(gate, wire_bytes=500)


def stay_in_flight(gate: Gate) -> None:
    gate.attempt(URL).__enter__()  # never ends


@pytest.mark.parametrize(
    ("setup", "policy", "reason"),
    [
        (freeze, {}, RefusalReason.FROZEN),
        (back_off, {}, RefusalReason.BACKOFF),
        (go_missing, {}, RefusalReason.URL_BACKOFF),
        (spend_the_day, {"daily_cap": 1}, RefusalReason.DAILY_CAP),
        (spend_the_bytes, {"byte_budget": 100}, RefusalReason.BYTE_BUDGET),
        (spend_the_day, {"limits": ["1/1m"]}, RefusalReason.RATE_LIMIT),
        (stay_in_flight, {}, RefusalReason.IN_FLIGHT),
    ],
)
def test_other_refusals_never_wait(
    harness: Harness,
    clock: RecordingClock,
    setup: Callable[[Gate], None],
    policy: dict[str, object],
    reason: RefusalReason,
) -> None:
    gate = make_gate(harness, clock, **policy)
    setup(gate)
    with pytest.raises(Refused) as refused, gate.attempt(URL, wait_up_to="1d") as a:
        a.response(200)
    assert refused.value.reason is reason
    assert clock.sleeps == []
    assert clock.now() == T


class StillClock(FakeClock):
    """A clock whose sleep returns at once without moving it, as a fake clock
    written by hand may, or a wall clock set back during the wait."""

    def __init__(self, start: datetime) -> None:
        super().__init__(start)
        self.sleeps = 0

    def sleep(self, delta: timedelta) -> None:
        self.sleeps += 1
        if self.sleeps > 5:
            raise AssertionError("the wait kept asking with a clock that never moved")

    async def sleep_async(self, delta: timedelta) -> None:
        self.sleep(delta)


def test_a_clock_that_does_not_move_while_it_sleeps_ends_the_wait(
    harness: Harness,
) -> None:
    clock = StillClock(T)
    gate = make_gate(harness, clock)
    respond(gate)
    with pytest.raises(Refused) as refused, gate.attempt(URL, wait_up_to="1m") as a:
        a.response(200)
    assert refused.value.reason is RefusalReason.MIN_INTERVAL
    assert clock.sleeps == 1
    assert len(gate.store.rows()) == 1


def test_wait_up_to_must_not_be_negative(
    harness: Harness, clock: RecordingClock
) -> None:
    gate = make_gate(harness, clock)
    with pytest.raises(ValueError, match="negative"):
        gate.attempt(URL, wait_up_to=timedelta(seconds=-1))


def test_wait_up_to_needs_a_clock_that_can_sleep(harness: Harness) -> None:
    gate = make_gate(harness, NowOnlyClock())
    with pytest.raises(TypeError, match="sleep"):
        gate.attempt(URL, wait_up_to="10s")
    # Without a wait, such a clock is enough, as before.
    respond(gate)
    assert len(gate.store.rows()) == 1


# --- async ----------------------------------------------------------------------


def test_the_async_attempt_waits_too(harness: Harness, clock: RecordingClock) -> None:
    gate = make_gate(harness, clock)
    respond(gate)

    async def main() -> Attempt:
        async with gate.attempt(URL, wait_up_to="10s") as attempt:
            attempt.response(200)
        return attempt

    attempt = asyncio.run(main())
    assert clock.sleeps == [timedelta(seconds=10)]
    assert opened_at(gate) == [T, T + timedelta(seconds=10)]
    assert attempt.result is not None


def test_the_async_attempt_refuses_past_the_bound(
    harness: Harness, clock: RecordingClock
) -> None:
    gate = make_gate(harness, clock)
    respond(gate)

    async def main() -> None:
        async with gate.attempt(URL, wait_up_to="9s") as attempt:
            attempt.response(200)

    with pytest.raises(Refused):
        asyncio.run(main())
    assert clock.sleeps == []
    assert len(gate.store.rows()) == 1


class StuckClock(FakeClock):
    """A clock whose async sleep never ends, until cancelled."""

    def __init__(self, start: datetime) -> None:
        super().__init__(start)
        self.sleeping = asyncio.Event()

    async def sleep_async(self, delta: timedelta) -> None:
        self.sleeping.set()
        await asyncio.Event().wait()


def test_cancelling_the_wait_opens_nothing(harness: Harness) -> None:
    clock = StuckClock(T)
    gate = make_gate(harness, clock)
    respond(gate)

    async def main() -> None:
        async def waiting() -> None:
            async with gate.attempt(URL, wait_up_to="10s") as attempt:
                attempt.response(200)

        task = asyncio.ensure_future(waiting())
        sleeping = asyncio.ensure_future(clock.sleeping.wait())
        await asyncio.wait([task, sleeping], timeout=5, return_when="FIRST_COMPLETED")
        assert clock.sleeping.is_set(), task  # the attempt is waiting, not done
        sleeping.cancel()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert len(gate.store.rows()) == 1
    assert gate.store.host_state(HOST, clock.now()).in_flight == 0


# --- the clocks -----------------------------------------------------------------


def test_the_clocks_of_the_library_can_sleep() -> None:
    assert isinstance(FakeClock(T), SleepingClock)
    assert isinstance(SystemClock(), SleepingClock)
    assert not isinstance(NowOnlyClock(), SleepingClock)


def test_the_fake_clock_moves_when_it_sleeps() -> None:
    clock = FakeClock(T)
    clock.sleep(timedelta(seconds=5))
    asyncio.run(clock.sleep_async(timedelta(seconds=5)))
    assert clock.now() == T + timedelta(seconds=10)


def test_the_system_clock_sleeps() -> None:
    # Windows reads `time.monotonic` in steps of about 15 ms: the bound
    # leaves room for one step.
    start = time.monotonic()
    SystemClock().sleep(timedelta(milliseconds=50))
    asyncio.run(SystemClock().sleep_async(timedelta(milliseconds=50)))
    assert time.monotonic() - start >= 0.08


# --- the fetchers ---------------------------------------------------------------


@pytest.mark.parametrize("kind", FETCHERS)
def test_the_fetchers_wait_too(kind: str) -> None:
    clock = RecordingClock(T)
    with SourceSimulator() as source:
        source.scenario = Respond(body=b'{"a": 1}')
        gate = Gate(
            MemoryStore(),
            {"127.0.0.1": HostPolicy(daily_cap=10, min_interval="2s")},
            clock=clock,
        )
        get = getter(kind, gate)
        get(source.url())
        result = get(source.url(), wait_up_to="5s")
        assert result.content == {"a": 1}
        assert source.counter.count == 2
    assert clock.sleeps == [timedelta(seconds=2)]
