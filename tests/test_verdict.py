"""The caller's verdict, apart from the source's answer (#28, part 1).

`response()` records what the source did; `accept()` that the caller took
the body, the only case that saves the validators; `reject(reason)` why it
did not. A rejected answer is still an answer: `min_interval` holds and the
failures stay as they were. A 2xx with no verdict is not vouched for: no
validators, otherwise as before.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from politefetch.clock import FakeClock
from politefetch.errors import SourceNotReady
from politefetch.gate import Gate
from politefetch.policy import HostPolicy
from politefetch.store import MemoryStore, RowState
from politefetch.testing.simulator import (
    HalfJson,
    SourceSimulator,
    Truncated,
    Validated,
)

from .adapter_kit import KINDS as FETCHERS
from .adapter_kit import getter
from .stores import KINDS, Harness

T = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
URL = "https://example.org/data.json"
HOST = "example.org"
ETAG = {"ETag": '"v1"'}


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
    policy = HostPolicy(daily_cap=100, min_interval="10s", backoff_base="1m")
    return Gate(harness.store, {HOST: policy}, clock=clock)


def fail_once(gate: Gate, clock: FakeClock) -> None:
    with gate.attempt(URL) as attempt:
        attempt.failure("connect_timeout")
    clock.advance(timedelta(hours=1))


def failures(gate: Gate, clock: FakeClock) -> int:
    return gate.store.host_state(HOST, clock.now()).failures


def promise(gate: Gate, clock: FakeClock) -> datetime | None:
    return gate.store.host_state(HOST, clock.now()).not_before


def conditional(gate: Gate) -> dict[str, str]:
    with gate.attempt(URL) as attempt:
        headers = attempt.conditional_headers
        attempt.reject("not_sent")
    return headers


# --- accept, reject, neither ------------------------------------------------------


def test_an_accepted_answer_saves_its_validators(gate: Gate, clock: FakeClock) -> None:
    fail_once(gate, clock)
    with gate.attempt(URL) as attempt:
        attempt.response(200, headers=ETAG)
        attempt.accept()
    assert failures(gate, clock) == 0
    clock.advance(timedelta(minutes=1))
    assert conditional(gate) == {"If-None-Match": '"v1"'}


def test_an_answer_without_a_verdict_saves_no_validators(
    gate: Gate, clock: FakeClock
) -> None:
    fail_once(gate, clock)
    with gate.attempt(URL) as attempt:
        attempt.response(200, headers=ETAG)
    assert failures(gate, clock) == 0  # the source answered: as before
    clock.advance(timedelta(minutes=1))
    assert conditional(gate) == {}


@pytest.mark.parametrize("reason", ["not_ready", "unusable"])
def test_a_rejected_answer_is_an_answer(
    gate: Gate, clock: FakeClock, reason: str
) -> None:
    with gate.attempt(URL) as attempt:  # an earlier body the caller kept
        attempt.response(200, headers={"ETag": '"v0"'})
        attempt.accept()
    clock.advance(timedelta(minutes=1))
    fail_once(gate, clock)
    with gate.attempt(URL) as attempt:
        attempt.response(200, headers=ETAG)
        attempt.reject(reason)
    row = attempt.result
    assert row is not None
    assert row.outcome is not None
    assert (row.outcome.ok, row.outcome.status, row.outcome.failure) == (
        False,
        200,
        f"rejected:{reason}",
    )
    assert failures(gate, clock) == 1  # neither grown nor reset
    assert promise(gate, clock) == clock.now() + timedelta(seconds=10)
    clock.advance(timedelta(minutes=1))
    assert conditional(gate) == {"If-None-Match": '"v0"'}  # not the rejected one


def test_a_request_not_sent_leaves_the_failures(gate: Gate, clock: FakeClock) -> None:
    fail_once(gate, clock)
    with gate.attempt(URL) as attempt:
        attempt.reject("not_sent")
    row = attempt.result
    assert row is not None
    assert row.outcome is not None
    assert (row.state, row.outcome.failure, row.outcome.status) == (
        RowState.CLOSED,
        "rejected:not_sent",
        None,
    )
    assert failures(gate, clock) == 1
    assert promise(gate, clock) == clock.now() + timedelta(seconds=10)


def test_a_cancellation_is_still_a_failure(gate: Gate, clock: FakeClock) -> None:
    with gate.attempt(URL):
        pass  # no outcome, no verdict
    assert failures(gate, clock) == 1


def test_a_rejected_answer_keeps_its_retry_after(gate: Gate, clock: FakeClock) -> None:
    with gate.attempt(URL) as attempt:
        attempt.response(200, headers={"Retry-After": "600"})
        attempt.reject("not_ready")
    assert promise(gate, clock) == clock.now() + timedelta(minutes=10)


# --- what is refused --------------------------------------------------------------


def test_accept_needs_a_good_answer(gate: Gate) -> None:
    with gate.attempt(URL) as attempt:
        with pytest.raises(RuntimeError, match="accept"):
            attempt.accept()
        attempt.response(500)
        with pytest.raises(RuntimeError, match="accept"):
            attempt.accept()


def test_a_rejection_needs_a_good_answer_or_none(gate: Gate) -> None:
    with gate.attempt(URL) as attempt:
        attempt.response(429)
        with pytest.raises(RuntimeError, match="reject"):
            attempt.reject("not_ready")
        with pytest.raises(RuntimeError, match="not_sent"):
            attempt.reject("not_sent")


def test_a_reason_must_be_known(gate: Gate) -> None:
    with gate.attempt(URL) as attempt:
        attempt.response(200)
        with pytest.raises(ValueError, match="reason"):
            attempt.reject("bored")
        attempt.accept()


def test_a_verdict_is_given_once(gate: Gate) -> None:
    with gate.attempt(URL) as attempt:
        attempt.response(200)
        attempt.accept()
        with pytest.raises(RuntimeError, match="once"):
            attempt.reject("unusable")


# --- the fetchers -----------------------------------------------------------------


def fetch_gate(clock: FakeClock) -> Gate:
    policy = HostPolicy(daily_cap=50, min_interval="1m", backoff_base="1m")
    return Gate(MemoryStore(), {"127.0.0.1": policy}, clock=clock)


@pytest.mark.parametrize("kind", FETCHERS)
def test_half_a_json_in_a_complete_answer_is_not_ready(kind: str) -> None:
    clock = FakeClock(T)
    gate = fetch_gate(clock)
    with SourceSimulator(HalfJson()) as source, pytest.raises(SourceNotReady):
        getter(kind, gate)(source.url())
    row = gate.store.rows()[-1]
    assert row.outcome is not None
    assert (row.outcome.failure, row.outcome.status) == ("rejected:not_ready", 200)
    assert gate.store.host_state("127.0.0.1", clock.now()).failures == 0


@pytest.mark.parametrize("kind", FETCHERS)
def test_a_transfer_cut_short_is_still_a_failure(kind: str) -> None:
    clock = FakeClock(T)
    gate = fetch_gate(clock)
    with SourceSimulator(Truncated()) as source, pytest.raises(SourceNotReady):
        getter(kind, gate)(source.url())
    assert gate.store.host_state("127.0.0.1", clock.now()).failures == 1


class Unparsable(Exception):
    pass


def refuse(content: object) -> object:
    raise Unparsable(content)


@pytest.mark.parametrize("kind", FETCHERS)
def test_a_body_the_parser_refuses_saves_no_validators(kind: str) -> None:
    clock = FakeClock(T)
    gate = fetch_gate(clock)
    with SourceSimulator(Validated(b'{"a": 1}', etag='"v1"')) as source:
        get = getter(kind, gate)
        with pytest.raises(Unparsable):
            get(source.url(), parse=refuse)
        row = gate.store.rows()[-1]
        assert row.outcome is not None
        assert row.outcome.failure == "rejected:unusable"
        clock.advance(timedelta(minutes=1))
        result = get(source.url(), parse=lambda content: content["a"])
        assert (result.status, result.content) == (200, 1)  # not a 304
        assert "if-none-match" not in source.counter.requests[-1].headers
        clock.advance(timedelta(minutes=1))
        assert get(source.url()).not_modified  # accepted: now conditional


@pytest.mark.parametrize("kind", FETCHERS)
def test_conditional_false_asks_for_the_whole_body(kind: str) -> None:
    clock = FakeClock(T)
    gate = fetch_gate(clock)
    with SourceSimulator(Validated(b'{"a": 1}', etag='"v1"')) as source:
        get = getter(kind, gate)
        get(source.url())
        clock.advance(timedelta(minutes=1))
        result = get(source.url(), conditional=False)
        assert (result.status, result.content) == (200, {"a": 1})
        assert "if-none-match" not in source.counter.requests[-1].headers
