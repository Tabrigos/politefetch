"""Failures per URL for 404 and 410 (#28, part 2, D-060).

A 404 or a 410 says the source answered and that one URL is missing: the
host keeps its spacing (`min_interval`, Retry-After) and its failures, and
the URL gets a pause of its own, growing with its consecutive 404/410 as the
host's grows with its failures. A good answer on the URL clears it.
Transport errors, 5xx, 429 and the other 4xx stay failures of the host.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, rule

from politefetch import FakeClock, FetchFailed, Gate, HostPolicy
from politefetch.errors import RefusalReason, Refused
from politefetch.store import HostState, MemoryStore, Outcome
from politefetch.store.base import url_failure
from politefetch.testing.simulator import Respond, SourceSimulator, Validated

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


def answer(
    gate: Gate, url: str, status: int, headers: dict[str, str] | None = None
) -> None:
    with gate.attempt(url) as attempt:
        attempt.response(status, headers=headers)
        if status == 200:
            attempt.accept()


def refusal(gate: Gate, url: str) -> Refused:
    with pytest.raises(Refused) as refused, gate.attempt(url) as attempt:
        attempt.response(200)
    return refused.value


def host(gate: Gate, clock: FakeClock) -> HostState:
    return gate.store.host_state(HOST, clock.now())


# --- one URL, not the host ----------------------------------------------------------


@pytest.mark.parametrize("status", [404, 410])
def test_a_missing_url_pauses_the_url_and_not_the_host(
    gate: Gate, clock: FakeClock, status: int
) -> None:
    answer(gate, URL, status)
    state = host(gate, clock)
    assert (state.failures, state.not_before) == (0, T + 10 * SECOND)
    assert state.not_before_reason is RefusalReason.MIN_INTERVAL
    clock.advance(10 * SECOND)
    answer(gate, OTHER_URL, 200)  # the host answers as before
    refused = refusal(gate, URL)
    assert (refused.reason, refused.retry_after) == (
        RefusalReason.URL_BACKOFF,
        T + MINUTE,
    )
    record = gate.store.url_record(URL)
    assert record is not None
    assert (record.failures, record.not_before) == (1, T + MINUTE)


def test_the_pause_of_a_url_grows_with_its_failures(
    gate: Gate, clock: FakeClock
) -> None:
    answer(gate, URL, 404)
    clock.advance(MINUTE)
    answer(gate, URL, 404)
    assert refusal(gate, URL).retry_after == T + MINUTE + 2 * MINUTE
    clock.advance(2 * MINUTE)
    answer(gate, URL, 410)
    assert refusal(gate, URL).retry_after == T + 3 * MINUTE + 4 * MINUTE


def test_a_good_answer_on_the_url_clears_its_failures(
    gate: Gate, clock: FakeClock
) -> None:
    answer(gate, URL, 404)
    clock.advance(MINUTE)
    answer(gate, URL, 200)
    record = gate.store.url_record(URL)
    assert record is not None
    assert (record.failures, record.not_before) == (0, None)
    clock.advance(MINUTE)
    answer(gate, URL, 404)
    assert refusal(gate, URL).retry_after == T + 2 * MINUTE + MINUTE  # from 1 again


@pytest.mark.parametrize("status", [304, 204])
def test_an_answer_that_keeps_the_validators_clears_the_failures_too(
    gate: Gate, clock: FakeClock, status: int
) -> None:
    with gate.attempt(URL) as attempt:
        attempt.response(200, headers={"ETag": '"v1"'})
        attempt.accept()
    clock.advance(MINUTE)
    answer(gate, URL, 404)
    clock.advance(MINUTE)
    with gate.attempt(URL) as attempt:
        attempt.response(status, headers={"ETag": '"v1"'})
        attempt.accept()
    record = gate.store.url_record(URL)
    assert record is not None
    assert record.validators.etag == '"v1"'
    assert (record.failures, record.not_before) == (0, None)


def test_a_good_answer_without_a_verdict_clears_them_too(
    gate: Gate, clock: FakeClock
) -> None:
    answer(gate, URL, 404)
    clock.advance(MINUTE)
    with gate.attempt(URL) as attempt:
        attempt.response(200)  # no accept(): no validators, but an answer
    record = gate.store.url_record(URL)
    assert record is not None
    assert record.failures == 0


def test_a_rejected_answer_leaves_the_failures_of_the_url(
    gate: Gate, clock: FakeClock
) -> None:
    answer(gate, URL, 404)
    clock.advance(MINUTE)
    with gate.attempt(URL) as attempt:
        attempt.response(200)
        attempt.reject("not_ready")
    record = gate.store.url_record(URL)
    assert record is not None
    assert record.failures == 1


def test_a_failure_of_the_host_leaves_the_failures_of_the_url(
    gate: Gate, clock: FakeClock
) -> None:
    answer(gate, URL, 404)
    clock.advance(MINUTE)
    answer(gate, URL, 503)
    assert host(gate, clock).failures == 1
    record = gate.store.url_record(URL)
    assert record is not None
    assert record.failures == 1


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503])
def test_other_errors_stay_with_the_host(
    gate: Gate, clock: FakeClock, status: int
) -> None:
    answer(gate, URL, status)
    assert host(gate, clock).failures == 1
    assert gate.store.url_record(URL) is None


def test_a_transport_failure_stays_with_the_host(gate: Gate, clock: FakeClock) -> None:
    with gate.attempt(URL) as attempt:
        attempt.failure("connect_timeout")
    assert host(gate, clock).failures == 1
    assert gate.store.url_record(URL) is None


def test_a_missing_url_keeps_the_retry_after_of_the_host(
    gate: Gate, clock: FakeClock
) -> None:
    answer(gate, URL, 404, {"Retry-After": "300"})
    assert host(gate, clock).not_before == T + 5 * MINUTE


def test_the_pause_of_a_url_survives_a_restart(
    harness: Harness, gate: Gate, clock: FakeClock
) -> None:
    answer(gate, URL, 404)
    clock.advance(30 * SECOND)
    restarted = Gate(harness.restart(), {HOST: POLICY}, clock=clock)
    assert refusal(restarted, URL).reason is RefusalReason.URL_BACKOFF


def test_a_url_known_only_from_its_404_sends_no_conditions(
    gate: Gate, clock: FakeClock
) -> None:
    answer(gate, URL, 404)
    record = gate.store.url_record(URL)
    assert record is not None
    assert (record.validators.etag, record.validators.last_modified) == (None, None)
    assert record.size is None
    clock.advance(MINUTE)
    with gate.attempt(URL) as attempt:
        assert attempt.conditional_headers == {}
        attempt.reject("not_sent")


def test_a_404_that_comes_after_the_reconciliation_changes_no_url(
    gate: Gate, clock: FakeClock
) -> None:
    with gate.attempt(URL) as attempt:
        clock.advance(2 * gate.reconcile_after)
        gate.reconcile()
        attempt.response(404)
    assert gate.store.url_record(URL) is None


def test_status_of_the_url_tells_its_pause(gate: Gate, clock: FakeClock) -> None:
    answer(gate, URL, 404)
    clock.advance(10 * SECOND)
    status = gate.status(URL)
    assert (status.may_fetch, status.reason) == (False, RefusalReason.URL_BACKOFF)
    assert status.next_allowed == T + MINUTE
    assert gate.status(HOST).may_fetch
    assert gate.may_fetch(OTHER_URL)
    assert not gate.may_fetch(URL)


def test_only_a_missing_url_is_a_url_failure() -> None:
    assert url_failure(Outcome(ok=False, status=404, failure="http"))
    assert url_failure(Outcome(ok=False, status=410, failure="http"))
    assert not url_failure(Outcome(ok=False, status=400, failure="http"))
    assert not url_failure(Outcome(ok=False, failure="reset"))
    assert not url_failure(Outcome(ok=True, status=200))


@pytest.mark.parametrize("kind", FETCHERS)
def test_a_fetcher_pauses_a_missing_url(kind: str, clock: FakeClock) -> None:
    gate = Gate(MemoryStore(), {"127.0.0.1": POLICY}, clock=clock)
    missing = Respond(404, b"not here", content_type="text/plain")
    with SourceSimulator(missing) as gone, SourceSimulator(Validated(b"{}")) as here:
        get = getter(kind, gate)
        with pytest.raises(FetchFailed):
            get(gone.url("/gone"))
        clock.advance(10 * SECOND)
        get(here.url("/here"))  # same host, another URL: allowed
        clock.advance(10 * SECOND)
        with pytest.raises(Refused) as refused:
            get(gone.url("/gone"))
    assert refused.value.reason is RefusalReason.URL_BACKOFF
    assert gate.store.host_state("127.0.0.1", clock.now()).failures == 0


# --- the invariant --------------------------------------------------------------

URLS = [URL, OTHER_URL]


class UrlFailureMachine(RuleBasedStateMachine):
    """One process, two URLs of one host, answers 200, 404 and 503, and
    restarts: a request is admitted exactly when the host's promise and its
    URL's pause allow it, so no URL is asked again before its pause ends."""

    kind = ""

    def __init__(self) -> None:
        super().__init__()
        self.harness = Harness(self.kind)
        self.clock = FakeClock(T)
        self.promise: datetime | None = None
        self.failures = 0
        self.url_failures = dict.fromkeys(URLS, 0)
        self.url_pause: dict[str, datetime | None] = dict.fromkeys(URLS)

    def teardown(self) -> None:
        self.harness.close()

    def gate(self) -> Gate:
        return Gate(self.harness.store, {HOST: POLICY}, clock=self.clock)

    @rule(
        url=st.sampled_from(URLS),
        status=st.sampled_from([200, 404, 503]),
        step=st.timedeltas(min_value=SECOND, max_value=10 * MINUTE),
    )
    def request(self, url: str, status: int, step: timedelta) -> None:
        self.clock.advance(step)
        now = self.clock.now()
        host_open = self.promise is None or now >= self.promise
        pause = self.url_pause[url]
        url_open = pause is None or now >= pause
        reason = None
        try:
            answer(self.gate(), url, status)
        except Refused as refused:
            reason = refused.reason
        if reason is not None:
            assert not (host_open and url_open)
            assert not host_open or reason is RefusalReason.URL_BACKOFF
            return
        assert host_open
        assert url_open
        if status == 503:
            self.failures += 1
            self.promise = now + max(10 * SECOND, POLICY.backoff(self.failures))
            return
        self.promise = now + 10 * SECOND
        if status == 200:
            self.failures = 0
            self.url_failures[url] = 0
            self.url_pause[url] = None
        else:
            self.url_failures[url] += 1
            self.url_pause[url] = now + max(
                10 * SECOND, POLICY.backoff(self.url_failures[url])
            )

    @rule()
    def restart(self) -> None:
        self.harness.restart()


class MemoryUrlFailures(UrlFailureMachine):
    kind = "memory"


class SqliteUrlFailures(UrlFailureMachine):
    kind = "sqlite"


class PostgresUrlFailures(UrlFailureMachine):
    kind = "postgres"


TestMemoryUrlFailures = MemoryUrlFailures.TestCase
TestMemoryUrlFailures.settings = settings(deadline=None)
TestSqliteUrlFailures = SqliteUrlFailures.TestCase
TestSqliteUrlFailures.settings = settings(max_examples=40, deadline=None)
TestPostgresUrlFailures = needs_postgres(PostgresUrlFailures.TestCase)
TestPostgresUrlFailures.settings = settings(max_examples=20, deadline=None)
