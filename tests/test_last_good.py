"""The last good answer and the not-ready count (#24, #13, D-062).

When a row closes with an answer the caller accepted, the store writes its
time as the host's and the URL's last good answer, atomically with the
close. A URL also counts its not-ready answers (`reject("not_ready")`) since
its last good answer, for an alert. `status` shows the host's, and, given a
URL, the URL's.
"""

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from politefetch import FakeClock, Gate, HostPolicy, SourceNotReady
from politefetch.errors import Refused
from politefetch.status import UrlStatus
from politefetch.store import MemoryStore
from politefetch.testing.simulator import HalfJson, Respond, SourceSimulator

from .adapter_kit import KINDS as FETCHERS
from .adapter_kit import getter
from .stores import KINDS, Harness, needs_postgres

T = datetime(2026, 10, 16, 12, tzinfo=UTC)
HOST = "example.org"
URL = "https://example.org/data.json"
OTHER_URL = "https://example.org/other.json"
SECOND = timedelta(seconds=1)
MINUTE = timedelta(minutes=1)
POLICY = HostPolicy(
    daily_cap=1000, min_interval="10s", backoff_base="1m", backoff_max="1h"
)


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


# --- how a request ends --------------------------------------------------------------


def accepted(gate: Gate, url: str, status: int = 200) -> None:
    with gate.attempt(url) as attempt:
        attempt.response(status, headers={"ETag": '"v1"'})
        attempt.accept()


def no_verdict(gate: Gate, url: str) -> None:
    with gate.attempt(url) as attempt:
        attempt.response(200)


def not_ready(gate: Gate, url: str) -> None:
    with gate.attempt(url) as attempt:
        attempt.response(200)
        attempt.reject("not_ready")


def unusable(gate: Gate, url: str) -> None:
    with gate.attempt(url) as attempt:
        attempt.response(200)
        attempt.reject("unusable")


def unavailable(gate: Gate, url: str) -> None:
    with gate.attempt(url) as attempt:
        attempt.response(503)


def missing(gate: Gate, url: str) -> None:
    with gate.attempt(url) as attempt:
        attempt.response(404)


def reset(gate: Gate, url: str) -> None:
    with gate.attempt(url) as attempt:
        attempt.failure("reset")


Ending = Callable[[Gate, str], None]
NOT_GOOD: list[Ending] = [no_verdict, not_ready, unusable, unavailable, missing, reset]


def url_status(gate: Gate, url: str = URL) -> UrlStatus:
    found = gate.status(url).url
    assert found is not None
    return found


def last_good(gate: Gate) -> datetime | None:
    return gate.status(HOST).last_good


# --- the last good answer (#24) -------------------------------------------------


def test_nothing_is_good_before_the_first_answer(gate: Gate) -> None:
    assert last_good(gate) is None
    assert url_status(gate) == UrlStatus(URL, last_good=None, not_ready=0)


@pytest.mark.parametrize("status", [200, 304])
def test_an_accepted_answer_is_the_last_good_one(
    gate: Gate, clock: FakeClock, status: int
) -> None:
    with gate.attempt(URL) as attempt:
        attempt.response(status)
        clock.advance(3 * SECOND)  # the close, not the open, is the answer
        attempt.accept()
    assert last_good(gate) == T + 3 * SECOND
    assert url_status(gate).last_good == T + 3 * SECOND


@pytest.mark.parametrize("ending", NOT_GOOD, ids=lambda e: e.__name__)
def test_any_other_ending_keeps_the_last_good_answer(
    gate: Gate, clock: FakeClock, ending: Ending
) -> None:
    accepted(gate, URL)
    clock.advance(2 * MINUTE)
    ending(gate, URL)
    assert last_good(gate) == T
    assert url_status(gate).last_good == T


@pytest.mark.parametrize("ending", NOT_GOOD, ids=lambda e: e.__name__)
def test_a_url_never_accepted_has_no_good_answer(gate: Gate, ending: Ending) -> None:
    ending(gate, URL)
    assert last_good(gate) is None
    assert url_status(gate).last_good is None


def test_the_host_has_the_latest_of_its_urls(gate: Gate, clock: FakeClock) -> None:
    accepted(gate, URL)
    clock.advance(MINUTE)
    accepted(gate, OTHER_URL)
    clock.advance(MINUTE)
    unavailable(gate, URL)
    assert last_good(gate) == T + MINUTE
    assert url_status(gate, URL).last_good == T
    assert url_status(gate, OTHER_URL).last_good == T + MINUTE


def test_an_answer_after_the_reconciliation_is_not_the_last_good_one(
    gate: Gate, clock: FakeClock
) -> None:
    with gate.attempt(URL) as attempt:
        clock.advance(2 * gate.reconcile_after)
        gate.reconcile()
        attempt.response(200)
        attempt.accept()
    assert last_good(gate) is None
    assert url_status(gate).last_good is None


def test_the_last_good_answer_survives_a_restart(
    harness: Harness, gate: Gate, clock: FakeClock
) -> None:
    accepted(gate, URL)
    clock.advance(MINUTE)
    not_ready(gate, URL)
    restarted = Gate(harness.restart(), {HOST: POLICY}, clock=clock)
    assert last_good(restarted) == T
    assert url_status(restarted) == UrlStatus(URL, last_good=T, not_ready=1)


def test_a_clock_gone_back_writes_the_time_of_the_answer(
    gate: Gate, clock: FakeClock
) -> None:
    accepted(gate, URL)
    clock.advance(MINUTE)
    with gate.attempt(URL) as attempt:
        clock.set(T - 10 * MINUTE)  # set back while the request runs
        attempt.response(200)
        attempt.accept()
    assert last_good(gate) == T - 10 * MINUTE
    assert url_status(gate).last_good == T - 10 * MINUTE


# --- the not-ready count (#13) --------------------------------------------------


def test_not_ready_answers_are_counted_per_url(gate: Gate, clock: FakeClock) -> None:
    for _ in range(3):
        not_ready(gate, URL)
        clock.advance(10 * SECOND)
    not_ready(gate, OTHER_URL)
    assert url_status(gate, URL).not_ready == 3
    assert url_status(gate, OTHER_URL).not_ready == 1


@pytest.mark.parametrize(
    "ending",
    [no_verdict, unusable, unavailable, missing, reset],
    ids=lambda e: e.__name__,
)
def test_only_a_good_answer_starts_the_count_again(
    gate: Gate, clock: FakeClock, ending: Ending
) -> None:
    not_ready(gate, URL)
    clock.advance(10 * SECOND)
    ending(gate, URL)
    clock.advance(2 * MINUTE)
    not_ready(gate, URL)
    assert url_status(gate).not_ready == 2
    clock.advance(10 * SECOND)
    accepted(gate, URL)
    assert url_status(gate).not_ready == 0


def test_a_not_ready_answer_keeps_the_validators(gate: Gate, clock: FakeClock) -> None:
    accepted(gate, URL)
    clock.advance(10 * SECOND)
    not_ready(gate, URL)
    record = gate.store.url_record(URL)
    assert record is not None
    assert record.validators.etag == '"v1"'
    assert (record.not_ready, record.last_good) == (1, T)


def test_a_not_ready_answer_after_the_reconciliation_is_not_counted(
    gate: Gate, clock: FakeClock
) -> None:
    with gate.attempt(URL) as attempt:
        clock.advance(2 * gate.reconcile_after)
        gate.reconcile()
        attempt.response(200)
        attempt.reject("not_ready")
    assert url_status(gate).not_ready == 0


# --- status ---------------------------------------------------------------------


def test_status_of_a_host_has_no_url(gate: Gate) -> None:
    accepted(gate, URL)
    assert gate.status(HOST).url is None
    assert gate.statuses([HOST])[0].url is None


def test_status_of_a_url_tells_its_pause_and_failures(
    gate: Gate, clock: FakeClock
) -> None:
    missing(gate, URL)
    clock.advance(10 * SECOND)
    assert url_status(gate) == UrlStatus(
        URL, last_good=None, not_ready=0, failures=1, paused_until=T + MINUTE
    )
    clock.advance(MINUTE)  # the pause is over: the failures stay until a good answer
    assert url_status(gate) == UrlStatus(URL, last_good=None, not_ready=0, failures=1)


def test_the_json_has_the_last_good_answer_and_the_url(
    gate: Gate, clock: FakeClock
) -> None:
    accepted(gate, URL)
    clock.advance(10 * SECOND)
    not_ready(gate, URL)
    data = json.loads(gate.status(URL).to_json())
    assert data["last_good"] == "2026-10-16T12:00:00+00:00"
    assert data["url"] == {
        "url": URL,
        "last_good": "2026-10-16T12:00:00+00:00",
        "not_ready": 1,
        "failures": 0,
        "paused_until": None,
    }
    assert json.loads(gate.status(HOST).to_json())["url"] is None


@pytest.mark.parametrize("kind", FETCHERS)
def test_a_fetcher_counts_what_is_not_ready(kind: str, clock: FakeClock) -> None:
    gate = Gate(MemoryStore(), {"127.0.0.1": POLICY}, clock=clock)
    with SourceSimulator(HalfJson(b'{"rows": [1, 2')) as source:
        get = getter(kind, gate)
        url = source.url("/data.json")
        with pytest.raises(SourceNotReady):
            get(url)
        clock.advance(10 * SECOND)
        source.scenario = Respond(body=b'{"rows": []}')
        assert url_status(gate, url).not_ready == 1
        get(url)
    assert url_status(gate, url) == UrlStatus(
        url, last_good=T + 10 * SECOND, not_ready=0
    )
    assert gate.status("127.0.0.1").last_good == T + 10 * SECOND


# --- the invariant --------------------------------------------------------------

URLS = [URL, OTHER_URL]
ENDINGS: list[Ending] = [accepted, *NOT_GOOD]


class LastGoodMachine(RuleBasedStateMachine):
    """One host, two URLs, every way a request can end, clock jumps back and
    restarts: the host's last good answer is always the close of the last
    accepted one, each URL's the close of its own, and each URL's count the
    not-ready answers since then."""

    kind = ""

    def __init__(self) -> None:
        super().__init__()
        self.harness = Harness(self.kind)
        self.clock = FakeClock(T)
        self.host_good: datetime | None = None
        self.url_good: dict[str, datetime | None] = dict.fromkeys(URLS)
        self.not_ready = dict.fromkeys(URLS, 0)

    def teardown(self) -> None:
        self.harness.close()

    def gate(self) -> Gate:
        return Gate(self.harness.store, {HOST: POLICY}, clock=self.clock)

    @rule(
        url=st.sampled_from(URLS),
        ending=st.sampled_from(ENDINGS),
        step=st.timedeltas(min_value=SECOND, max_value=2 * MINUTE),
    )
    def request(self, url: str, ending: Ending, step: timedelta) -> None:
        self.clock.advance(step)
        try:
            ending(self.gate(), url)
        except Refused:
            return  # nothing opened, nothing closed
        now = self.clock.now()
        if ending is accepted:
            self.host_good = self.url_good[url] = now
            self.not_ready[url] = 0
        elif ending is not_ready:
            self.not_ready[url] += 1

    @rule(back=st.timedeltas(min_value=SECOND, max_value=30 * MINUTE))
    def clock_goes_back(self, back: timedelta) -> None:
        self.clock.set(self.clock.now() - back)

    @rule()
    def restart(self) -> None:
        self.harness.restart()

    @invariant()
    def recorded(self) -> None:
        gate = self.gate()
        assert gate.status(HOST).last_good == self.host_good
        for url in URLS:
            assert url_status(gate, url).last_good == self.url_good[url]
            assert url_status(gate, url).not_ready == self.not_ready[url]


class MemoryLastGood(LastGoodMachine):
    kind = "memory"


class SqliteLastGood(LastGoodMachine):
    kind = "sqlite"


class PostgresLastGood(LastGoodMachine):
    kind = "postgres"


TestMemoryLastGood = MemoryLastGood.TestCase
TestMemoryLastGood.settings = settings(deadline=None)
TestSqliteLastGood = SqliteLastGood.TestCase
TestSqliteLastGood.settings = settings(max_examples=40, deadline=None)
TestPostgresLastGood = needs_postgres(PostgresLastGood.TestCase)
TestPostgresLastGood.settings = settings(max_examples=20, deadline=None)
