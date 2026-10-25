"""What a fetch does with the time read from the data.

Data that is old or stuck came from a source that answered well: it counts
as an answer (the pause is the minimum interval, the failures do not grow,
the validators are kept) and the error carries the result. A data time
too far ahead is not data: it never enters the history.
"""

import json
import pickle
from datetime import UTC, datetime, timedelta

import pytest

from politefetch.adapters.base import Fetcher
from politefetch.clock import FakeClock
from politefetch.errors import (
    RefusalReason,
    Refused,
    StaleData,
    StuckSource,
    UnexpectedContent,
)
from politefetch.gate import Gate
from politefetch.policy import HostPolicy
from politefetch.store import MemoryStore, Outcome

from .scripted import Scripted

T = datetime(2026, 10, 25, 12, tzinfo=UTC)
URL = "https://example.org/data.json"
HOST = "example.org"


def at(moment: datetime) -> bytes:
    return json.dumps({"time": moment.isoformat()}).encode()


def data_time(content: dict[str, str]) -> datetime:
    return datetime.fromisoformat(content["time"])


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T)


@pytest.fixture
def gate(clock: FakeClock) -> Gate:
    policy = HostPolicy(daily_cap=50, min_interval="10m", backoff_base="1h")
    return Gate(MemoryStore(), {HOST: policy}, clock=clock)


def last_outcome(gate: Gate) -> Outcome:
    outcome = gate.store.rows()[-1].outcome
    assert outcome is not None
    return outcome


def assert_answered(gate: Gate) -> None:
    """The host was left as after a good answer: no failure counted, and the
    next request waits for the minimum interval only."""
    outcome = last_outcome(gate)
    assert (outcome.ok, outcome.failure) == (True, None)
    assert gate.store.host_state(HOST, gate.clock.now()).failures == 0
    with pytest.raises(Refused) as refused:
        Fetcher(gate, Scripted()).get(URL)
    assert refused.value.reason is RefusalReason.MIN_INTERVAL
    assert refused.value.retry_after == gate.clock.now() + timedelta(minutes=10)


# --- stale data -----------------------------------------------------------------------


def test_stale_data_is_an_answer(gate: Gate) -> None:
    old = T - timedelta(hours=5)
    transport = Scripted([at(old)], headers={"etag": '"v1"'})
    with pytest.raises(StaleData) as stale:
        Fetcher(gate, transport).get(URL, data_time=data_time, max_age="3h")
    result = stale.value.result
    assert result is not None
    assert (result.status, result.content, result.data_time) == (
        200,
        {"time": old.isoformat()},
        old,
    )
    assert_answered(gate)
    record = gate.store.url_record(URL)
    assert record is not None
    assert record.validators.etag == '"v1"'


def test_a_304_on_stale_data_is_an_answer(gate: Gate, clock: FakeClock) -> None:
    fetcher = Fetcher(gate, Scripted([at(T)], headers={"etag": '"v1"'}))
    fetcher.get(URL, data_time=data_time, max_age="3h")
    clock.advance(timedelta(hours=4))
    with pytest.raises(StaleData) as stale:
        Fetcher(gate, Scripted([], status=304)).get(
            URL, data_time=data_time, max_age="3h"
        )
    result = stale.value.result
    assert result is not None
    assert (result.not_modified, result.data_time) == (True, T)
    assert last_outcome(gate).status == 304
    assert_answered(gate)


def test_the_result_survives_pickling(gate: Gate) -> None:
    with pytest.raises(StaleData) as stale:
        Fetcher(gate, Scripted([at(T - timedelta(days=1))])).get(
            URL, data_time=data_time, max_age="3h"
        )
    copy = pickle.loads(pickle.dumps(stale.value))
    assert copy.result == stale.value.result
    assert copy.age == timedelta(days=1)


# --- stuck sources --------------------------------------------------------------------


def test_a_stuck_source_is_an_answer(gate: Gate, clock: FakeClock) -> None:
    for _ in range(2):
        Fetcher(gate, Scripted([at(T)])).get(URL, data_time=data_time, stuck_after=3)
        clock.advance(timedelta(minutes=10))
    with pytest.raises(StuckSource) as stuck:
        Fetcher(gate, Scripted([at(T)])).get(URL, data_time=data_time, stuck_after=3)
    assert stuck.value.reads == 3
    assert stuck.value.result is not None
    assert stuck.value.result.content == {"time": T.isoformat()}
    assert_answered(gate)


def test_stuck_after_a_time(gate: Gate, clock: FakeClock) -> None:
    for _ in range(6):
        Fetcher(gate, Scripted([at(T)])).get(URL, data_time=data_time, stuck_after="1h")
        clock.advance(timedelta(minutes=10))
    # The first read was at T; now it is T + 1h.
    with pytest.raises(StuckSource) as stuck:
        Fetcher(gate, Scripted([at(T)])).get(URL, data_time=data_time, stuck_after="1h")
    assert (stuck.value.since, stuck.value.reads) == (T, 7)


def test_stuck_after_a_time_is_not_fooled_by_few_reads(
    gate: Gate, clock: FakeClock
) -> None:
    Fetcher(gate, Scripted([at(T)])).get(URL, data_time=data_time, stuck_after="1h")
    clock.advance(timedelta(hours=2))
    with pytest.raises(StuckSource):
        Fetcher(gate, Scripted([at(T)])).get(URL, data_time=data_time, stuck_after="1h")


@pytest.mark.parametrize("stuck_after", [1, 0, "0s"])
def test_stuck_after_must_mean_something(gate: Gate, stuck_after: object) -> None:
    with pytest.raises(ValueError, match="stuck_after"):
        Fetcher(gate, Scripted()).get(
            URL,
            data_time=data_time,
            stuck_after=stuck_after,  # type: ignore[arg-type]
        )


# --- data times ahead of now ----------------------------------------------------------


def test_a_data_time_far_ahead_is_unexpected_and_not_kept(
    gate: Gate, clock: FakeClock
) -> None:
    future = T + timedelta(days=365 * 73)
    with pytest.raises(UnexpectedContent, match="ahead"):
        Fetcher(gate, Scripted([at(future)])).get(
            URL, data_time=data_time, stuck_after=3
        )
    assert last_outcome(gate).failure == "unexpected_content"
    assert gate.store.data_time_record(URL) is None
    # The history was not poisoned: real data later is one fresh read.
    clock.advance(timedelta(days=1))
    result = Fetcher(gate, Scripted([at(clock.now())])).get(
        URL, data_time=data_time, stuck_after=3
    )
    assert result.data_time == clock.now()
    record = gate.store.data_time_record(URL)
    assert record is not None
    assert record.reads == 1


def test_a_data_time_a_little_ahead_is_fine(gate: Gate) -> None:
    ahead = T + timedelta(minutes=15)
    result = Fetcher(gate, Scripted([at(ahead)])).get(URL, data_time=data_time)
    assert result.data_time == ahead


def test_max_ahead_allows_forecasts(gate: Gate) -> None:
    ahead = T + timedelta(days=2)
    fetcher = Fetcher(gate, Scripted([at(ahead)]))
    assert fetcher.get(URL, data_time=data_time, max_ahead="3d").data_time == ahead
