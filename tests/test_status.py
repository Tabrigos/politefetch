"""The readable state per host: what `gate.status` says, and its JSON."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings

from politefetch.clock import FakeClock
from politefetch.errors import RefusalReason, Refused
from politefetch.gate import Gate
from politefetch.policy import HostPolicy
from politefetch.status import HostStatus, to_json
from politefetch.store import Freeze, MemoryStore, RowState

from .stores import KINDS, Harness
from .test_gate_properties import POLICY, actions

T = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
HOST = "example.org"
URL = "https://example.org/data.json"


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
    return Gate(
        harness.store,
        {
            HOST: HostPolicy(daily_cap=3, min_interval="10m", byte_budget="1MB"),
            "other.example": HostPolicy(daily_cap=40, min_interval="2h"),
        },
        clock=clock,
        reconcile_after="1h",
    )


def respond(gate: Gate, status: int, *, wire: int | None = None) -> None:
    with gate.attempt(URL) as attempt:
        attempt.response(status, wire_bytes=wire, decoded_bytes=wire)


def test_a_fresh_host_may_fetch_now(gate: Gate) -> None:
    status = gate.status(HOST)
    assert status == HostStatus(
        host=HOST,
        at=T,
        may_fetch=True,
        reason=None,
        next_allowed=T,
        frozen=None,
        failures=0,
        throttled=0,
        in_flight=0,
        day=T.date(),
        requests_today=0,
        daily_cap=3,
        wire_bytes_today=0,
        decoded_bytes_today=0,
        byte_budget=1_000_000,
        recent=(),
    )


def test_after_a_good_answer(gate: Gate, clock: FakeClock) -> None:
    respond(gate, 200, wire=500)
    status = gate.status(HOST)
    assert not status.may_fetch
    assert status.reason is RefusalReason.MIN_INTERVAL
    assert status.next_allowed == T + timedelta(minutes=10)
    assert status.requests_today == 1
    assert status.wire_bytes_today == 500
    assert status.decoded_bytes_today == 500
    [row] = status.recent
    assert row.state is RowState.CLOSED
    assert row.outcome is not None
    assert row.outcome.status == 200
    clock.advance(timedelta(minutes=10))
    assert gate.status(HOST).may_fetch


def test_failures_and_backoff(gate: Gate) -> None:
    with gate.attempt(URL) as attempt:
        attempt.failure("connect_timeout")
    status = gate.status(HOST)
    assert status.reason is RefusalReason.BACKOFF
    assert status.failures == 1
    assert status.next_allowed == T + timedelta(minutes=10)  # never below min_interval


def test_a_frozen_host_has_no_next_time(gate: Gate) -> None:
    respond(gate, 403)
    status = gate.status(HOST)
    assert not status.may_fetch
    assert status.reason is RefusalReason.FROZEN
    assert status.next_allowed is None
    assert status.frozen is not None
    assert status.frozen.status == 403
    gate.unfreeze(HOST, by="ops")
    assert gate.status(HOST).frozen is None


def test_an_expired_freeze_is_not_shown(
    harness: Harness, gate: Gate, clock: FakeClock
) -> None:
    gate = Gate(
        harness.store,
        {HOST: HostPolicy(daily_cap=3, min_interval="10m", freeze_for="1h")},
        clock=clock,
    )
    respond(gate, 451)
    assert gate.status(HOST).frozen is not None
    clock.advance(timedelta(hours=1))
    status = gate.status(HOST)
    assert status.frozen is None
    assert status.may_fetch


def test_the_daily_cap(gate: Gate, clock: FakeClock) -> None:
    for _ in range(3):
        respond(gate, 200)
        clock.advance(timedelta(minutes=10))
    status = gate.status(HOST)
    assert status.reason is RefusalReason.DAILY_CAP
    assert status.requests_today == 3
    assert status.next_allowed == datetime(2026, 10, 17, tzinfo=UTC)


def test_a_request_in_flight(gate: Gate) -> None:
    with gate.attempt(URL) as attempt:
        status = gate.status(HOST)
        assert status.in_flight == 1
        assert status.reason is RefusalReason.IN_FLIGHT
        assert [r.state for r in status.recent] == [RowState.IN_FLIGHT]
        attempt.response(200)


def test_an_abandoned_request_shows_as_abandoned(
    gate: Gate, harness: Harness, clock: FakeClock
) -> None:
    harness.store.open_request(
        HOST, URL, now=T, daily_cap=3, cautious=lambda _s: T + timedelta(minutes=5)
    )
    clock.advance(timedelta(hours=2))
    status = gate.status(HOST)
    assert (status.in_flight, status.abandoned) == (0, 1)
    # Shown as it is stored: the next request to the host closes it.
    assert [r.state for r in status.recent] == [RowState.IN_FLIGHT]


def test_recent_rows_are_the_latest_newest_first(gate: Gate, clock: FakeClock) -> None:
    for code in (200, 304, 200):
        respond(gate, code)
        clock.advance(timedelta(minutes=10))
    statuses = [r.outcome.status for r in gate.status(HOST).recent if r.outcome]
    assert statuses == [200, 304, 200]
    ids = [r.id for r in gate.status(HOST, recent=2).recent]
    assert ids == sorted(ids, reverse=True)
    assert len(ids) == 2
    assert gate.status(HOST, recent=0).recent == ()
    with pytest.raises(ValueError, match="recent"):
        gate.status(HOST, recent=-1)


def test_recent_rows_belong_to_the_host(gate: Gate) -> None:
    respond(gate, 200)
    with gate.attempt("https://other.example/x") as attempt:
        attempt.response(200)
    assert [r.host for r in gate.status("other.example").recent] == ["other.example"]


def test_a_url_counts_its_reservation(gate: Gate, clock: FakeClock) -> None:
    respond(gate, 200, wire=600_000)
    clock.advance(timedelta(minutes=10))
    # The host has room left; this URL's last answer would not fit in it.
    assert gate.status(HOST).may_fetch
    status = gate.status(URL)
    assert status.host == HOST
    assert not status.may_fetch
    assert status.reason is RefusalReason.BYTE_BUDGET


def test_status_opens_no_request(gate: Gate) -> None:
    gate.status(HOST)
    gate.statuses()
    assert gate.store.rows() == []


def test_unknown_hosts(harness: Harness, gate: Gate, clock: FakeClock) -> None:
    with pytest.raises(ValueError, match="no policy"):
        gate.status("unknown.example")
    lenient = Gate(
        harness.store,
        {},
        clock=clock,
        default=HostPolicy(daily_cap=7, min_interval="1m"),
    )
    assert lenient.status("unknown.example").daily_cap == 7


def test_statuses_cover_the_configured_hosts(gate: Gate) -> None:
    assert [s.host for s in gate.statuses()] == ["example.org", "other.example"]
    assert [s.host for s in gate.statuses(["https://other.example/x", HOST])] == [
        "other.example",
        HOST,
    ]


def test_statuses_of_given_hosts_keep_their_order(
    harness: Harness, clock: FakeClock
) -> None:
    gate = Gate(
        harness.store,
        {},
        clock=clock,
        default=HostPolicy(daily_cap=1, min_interval="1m"),
    )
    hosts = ["b.example", "a.example"]
    assert [s.host for s in gate.statuses(hosts)] == hosts


# --- JSON -----------------------------------------------------------------------------


def test_json_shape(gate: Gate, clock: FakeClock) -> None:
    clock.advance(timedelta(seconds=30))
    with gate.attempt(URL) as attempt:
        clock.advance(timedelta(seconds=2))
        attempt.response(200, wire_bytes=120, decoded_bytes=480)
    respond_at = clock.now()
    assert json.loads(gate.status(HOST).to_json()) == {
        "host": "example.org",
        "at": respond_at.isoformat(),
        "may_fetch": False,
        "reason": "min_interval",
        "next_allowed": "2026-10-16T12:10:32+00:00",
        "frozen": None,
        "failures": 0,
        "throttled": 0,
        "in_flight": 0,
        "abandoned": 0,
        "last_good": None,  # answered, but not accepted
        "url": None,
        "today": {
            "day": "2026-10-16",
            "requests": 1,
            "daily_cap": 3,
            "wire_bytes": 120,
            "decoded_bytes": 480,
            "byte_budget": 1_000_000,
        },
        "limits": [],
        "recent": [
            {
                "id": gate.store.rows()[0].id,
                "url": URL,
                "opened_at": "2026-10-16T12:00:30+00:00",
                "state": "closed",
                "closed_at": "2026-10-16T12:00:32+00:00",
                "ok": True,
                "status": 200,
                "failure": None,
                "wire_bytes": 120,
                "decoded_bytes": 480,
                "reserved_bytes": 0,
                "key": URL,
                "tag": None,
            }
        ],
    }


def test_json_of_a_frozen_host_and_an_open_row(gate: Gate) -> None:
    respond(gate, 429)
    data = gate.status(HOST).to_dict()
    assert data["reason"] == "frozen"
    assert data["next_allowed"] is None
    assert data["frozen"] == {
        "since": T.isoformat(),
        "until": None,
        "status": 429,
        "note": "HTTP 429",
    }
    with gate.attempt("https://other.example/x") as attempt:
        rows = gate.status("other.example").to_dict()["recent"]
        assert isinstance(rows, list)
        [row] = rows
        assert row["state"] == "in_flight"
        assert row["closed_at"] is None
        assert row["ok"] is None
        attempt.response(200)


def test_json_shows_what_an_open_row_reserves(gate: Gate, clock: FakeClock) -> None:
    respond(gate, 200, wire=700)
    clock.advance(timedelta(minutes=10))
    with gate.attempt(URL) as attempt:
        rows = gate.status(HOST).to_dict()["recent"]
        assert isinstance(rows, list)
        newest, oldest = rows
        assert (newest["state"], newest["reserved_bytes"]) == ("in_flight", 700)
        assert (oldest["state"], oldest["reserved_bytes"]) == ("closed", 0)
        attempt.response(200, wire_bytes=650)


def test_to_json_takes_one_status_or_many(gate: Gate) -> None:
    one = gate.status(HOST)
    assert json.loads(to_json(one)) == one.to_dict()
    many = gate.statuses()
    assert json.loads(to_json(many)) == [s.to_dict() for s in many]
    assert to_json(one, indent=2).startswith("{\n  ")


def test_freeze_note_and_expiry_in_json() -> None:
    freeze = Freeze(since=T, until=T + timedelta(hours=1), status=451, note="legal")
    status = HostStatus(
        host=HOST,
        at=T,
        may_fetch=False,
        reason=RefusalReason.FROZEN,
        next_allowed=T + timedelta(hours=1),
        frozen=freeze,
        failures=0,
        throttled=0,
        in_flight=0,
        day=T.date(),
        requests_today=1,
        daily_cap=3,
        wire_bytes_today=0,
        decoded_bytes_today=0,
        byte_budget=None,
        recent=(),
    )
    data = status.to_dict()
    assert data["frozen"] == {
        "since": "2026-10-16T12:00:00+00:00",
        "until": "2026-10-16T13:00:00+00:00",
        "status": 451,
        "note": "legal",
    }
    assert data["today"]["byte_budget"] is None  # type: ignore[index]


# --- the status says what the gate would do ------------------------------------------


@settings(max_examples=100, deadline=None)
@given(actions)
def test_the_status_agrees_with_the_gate(script: list[tuple[str, object]]) -> None:
    clock = FakeClock(T)
    store = MemoryStore()
    gate = Gate(store, {HOST: POLICY}, clock=clock)
    for kind, arg in script:
        if kind == "wait":
            assert isinstance(arg, int)
            clock.advance(timedelta(seconds=arg))
            continue
        if kind == "unfreeze":
            gate.unfreeze(HOST, by="test")
            continue
        status = gate.status(URL, recent=3)
        rows = store.rows(host=HOST)
        assert status.recent == tuple(reversed(rows[-3:]))
        assert status.requests_today == store.requests_on(HOST, clock.now().date())
        context = gate.attempt(URL)
        refusal: Refused | None = None
        try:
            attempt = context.__enter__()
        except Refused as refused:
            refusal = refused
        if refusal is not None:
            # Never a "yes" the gate would turn into a "no", and the same why
            # and until when.
            assert not status.may_fetch
            assert status.reason is refusal.reason
            assert status.next_allowed == refusal.retry_after
            continue
        assert status.may_fetch
        assert status.next_allowed == clock.now()
        clock.advance(timedelta(seconds=1))
        assert isinstance(arg, tuple)
        answer, retry_after = arg
        if isinstance(answer, int):
            attempt.response(answer, retry_after=retry_after)
        elif answer == "timeout":
            attempt.failure("connect_timeout")
        context.__exit__(None, None, None)
