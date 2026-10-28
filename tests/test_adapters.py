"""Every adapter, against the source simulator, on the same scenarios."""

import contextlib
import gzip
import json
import socket
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from politefetch.adapters import FailureShape
from politefetch.clock import FakeClock
from politefetch.errors import (
    BodyTooLarge,
    FetchFailed,
    RefusalReason,
    Refused,
    SourceNotReady,
    StaleData,
    StuckSource,
    UnexpectedContent,
)
from politefetch.gate import Gate
from politefetch.policy import HostPolicy
from politefetch.store import MemoryStore, RequestRow
from politefetch.testing.simulator import (
    Drip,
    HalfJson,
    Html,
    NoAnswer,
    Reset,
    Respond,
    SourceSimulator,
    TooManyRequests,
    Truncated,
    Validated,
    closed_port_url,
    stuck_json,
)

from .adapter_kit import KINDS, Get, getter

T = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
HOST = "127.0.0.1"


def data_time(content: dict[str, str]) -> datetime:
    return datetime.fromisoformat(content["time"])


@pytest.fixture
def source() -> Iterator[SourceSimulator]:
    with SourceSimulator() as simulator:
        yield simulator


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T)


@pytest.fixture
def gate(clock: FakeClock) -> Gate:
    return Gate(
        MemoryStore(),
        {HOST: HostPolicy(daily_cap=50, min_interval="10m", backoff_base="1m")},
        clock=clock,
    )


@pytest.fixture(params=KINDS)
def get(request: pytest.FixtureRequest, gate: Gate) -> Get:
    return getter(request.param, gate)


def last_row(gate: Gate) -> RequestRow:
    return gate.store.rows()[-1]


# --- good answers ---------------------------------------------------------------------


def test_a_json_answer(get: Get, gate: Gate, source: SourceSimulator) -> None:
    body = b'{"a": [1, 2]}'
    source.scenario = Respond(body=body)
    result = get(source.url("/data.json"))
    assert result.status == 200
    assert result.content == {"a": [1, 2]}
    assert result.body == body
    assert not result.not_modified
    assert result.decoded_bytes == len(body)
    assert result.wire_bytes > 0
    outcome = last_row(gate).outcome
    assert outcome is not None
    assert (outcome.ok, outcome.status, outcome.decoded_bytes) == (True, 200, len(body))
    assert gate.store.host_state(HOST, T).wire_bytes_today == result.wire_bytes


def test_text_and_bytes(
    get: Get, gate: Gate, clock: FakeClock, source: SourceSimulator
) -> None:
    source.scenario = Respond(
        body="caffè".encode(), content_type="text/plain; charset=utf-8"
    )
    assert get(source.url(), expect="text").content == "caffè"
    clock.advance(timedelta(minutes=10))
    assert get(source.url(), expect="bytes").content == "caffè".encode()


def test_it_says_who_it_is(get: Get, source: SourceSimulator) -> None:
    get(source.url(), headers={"Accept": "application/json"})
    sent = source.counter.requests[0].headers
    assert sent["user-agent"].startswith("politefetch/")
    assert sent["accept"] == "application/json"


def test_conditional_requests(
    get: Get, gate: Gate, clock: FakeClock, source: SourceSimulator
) -> None:
    source.scenario = Validated(b'{"a": 1}', etag='"v1"')
    assert get(source.url()).content == {"a": 1}
    clock.advance(timedelta(minutes=10))
    result = get(source.url())
    assert result.not_modified
    assert result.status == 304
    assert result.content is None
    assert source.counter.requests[1].headers["if-none-match"] == '"v1"'


def test_a_304_does_not_make_old_data_fresh(
    get: Get, gate: Gate, clock: FakeClock, source: SourceSimulator
) -> None:
    body = json.dumps({"time": (T - timedelta(hours=1)).isoformat()}).encode()
    source.scenario = Validated(body, etag='"v1"')
    get(source.url(), data_time=data_time, max_age="3h")
    clock.advance(timedelta(hours=3))
    with pytest.raises(StaleData):
        get(source.url(), data_time=data_time, max_age="3h")
    outcome = last_row(gate).outcome
    assert outcome is not None
    # Stale data is an answer: the source did its part (D-048).
    assert (outcome.ok, outcome.status, outcome.failure) == (True, 304, None)


# --- failures without a usable answer -------------------------------------------------


def test_an_http_error(get: Get, gate: Gate, source: SourceSimulator) -> None:
    source.scenario = Respond(503, b"busy", content_type="text/plain")
    with pytest.raises(FetchFailed) as failed:
        get(source.url())
    assert (failed.value.shape, failed.value.status) == (FailureShape.HTTP, 503)
    outcome = last_row(gate).outcome
    assert outcome is not None
    assert (outcome.ok, outcome.status, outcome.failure) == (False, 503, "http")
    assert outcome.decoded_bytes == 4


def test_429_freezes_the_host(get: Get, gate: Gate, source: SourceSimulator) -> None:
    source.scenario = TooManyRequests(retry_after="60")
    with pytest.raises(FetchFailed):
        get(source.url())
    with pytest.raises(Refused) as refused:
        get(source.url())
    assert refused.value.reason is RefusalReason.FROZEN
    assert source.counter.count == 1


@pytest.mark.parametrize(
    ("scenario", "shape"),
    [(NoAnswer(), FailureShape.READ_TIMEOUT), (Reset(), FailureShape.RESET)],
    ids=["no answer", "reset"],
)
def test_no_answer_and_reset(
    get: Get, gate: Gate, source: SourceSimulator, scenario: object, shape: FailureShape
) -> None:
    source.scenario = scenario  # type: ignore[assignment]
    with pytest.raises(FetchFailed) as failed:
        get(source.url())
    assert failed.value.shape == shape
    outcome = last_row(gate).outcome
    assert outcome is not None
    assert (outcome.ok, outcome.failure) == (False, shape.value)


@pytest.mark.parametrize("kind", KINDS)
def test_a_refused_connection(kind: str, gate: Gate) -> None:
    # Windows retries the SYN for about two seconds before it reports a
    # refusal, even on loopback: a shorter connect timeout would see a timeout
    # (urllib connects with the read timeout).
    get = getter(kind, gate, timeout=(10.0, 10.0))
    with pytest.raises(FetchFailed) as failed:
        get(closed_port_url())
    assert failed.value.shape == FailureShape.REFUSED


@pytest.fixture
def plain_port() -> Iterator[int]:
    """A loopback port that answers in plain HTTP to the first bytes it gets,
    as a web server does to a TLS handshake it does not understand."""
    server = socket.create_server(("127.0.0.1", 0))
    server.settimeout(5)

    def serve() -> None:
        with contextlib.suppress(OSError):
            conn, _ = server.accept()
            with conn:
                # Read the handshake first: closing with unread data sends a
                # reset, and Windows then drops the answer before the client
                # reads it.
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield server.getsockname()[1]
    server.close()
    thread.join(timeout=5)


def test_tls_against_a_plain_server(get: Get, gate: Gate, plain_port: int) -> None:
    with pytest.raises(FetchFailed) as failed:
        get(f"https://127.0.0.1:{plain_port}/")
    assert failed.value.shape == FailureShape.TLS


# --- answers that are not data --------------------------------------------------------


def test_a_truncated_body(get: Get, gate: Gate, source: SourceSimulator) -> None:
    source.scenario = Truncated(b'{"time": "2026-', declared_length=200)
    with pytest.raises(SourceNotReady):
        get(source.url())
    outcome = last_row(gate).outcome
    assert outcome is not None
    assert (outcome.ok, outcome.status, outcome.failure) == (False, 200, "not_ready")


def test_a_complete_answer_with_half_a_json(
    get: Get, gate: Gate, source: SourceSimulator
) -> None:
    source.scenario = HalfJson()
    with pytest.raises(SourceNotReady, match="JSON ends mid-way"):
        get(source.url())
    outcome = last_row(gate).outcome
    assert outcome is not None
    # The source answered: a rejected answer, not a failure (#28).
    assert (outcome.ok, outcome.status, outcome.failure) == (
        False,
        200,
        "rejected:not_ready",
    )


def test_html_instead_of_json(get: Get, gate: Gate, source: SourceSimulator) -> None:
    source.scenario = Html()
    with pytest.raises(UnexpectedContent, match="HTML"):
        get(source.url())
    outcome = last_row(gate).outcome
    assert outcome is not None
    assert (outcome.ok, outcome.failure) == (False, "unexpected_content")


def test_json_served_as_html_is_data(get: Get, source: SourceSimulator) -> None:
    source.scenario = Respond(body=b'{"a": 1}', content_type="text/html")
    assert get(source.url()).content == {"a": 1}


def test_stale_data(get: Get, gate: Gate, source: SourceSimulator) -> None:
    source.scenario = stuck_json(T - timedelta(hours=5))
    with pytest.raises(StaleData):
        get(source.url(), data_time=data_time, max_age="3h")


def test_a_stuck_source(
    get: Get, gate: Gate, clock: FakeClock, source: SourceSimulator
) -> None:
    source.scenario = stuck_json(T)
    for _ in range(2):
        get(source.url(), data_time=data_time, stuck_after=3)
        clock.advance(timedelta(minutes=10))
    with pytest.raises(StuckSource) as stuck:
        get(source.url(), data_time=data_time, stuck_after=3)
    assert stuck.value.reads == 3


def test_failed_content_does_not_teach_validators(
    get: Get, gate: Gate, source: SourceSimulator
) -> None:
    source.scenario = Validated(
        b"<!doctype html>", etag='"bad"', content_type="text/html"
    )
    with pytest.raises(UnexpectedContent):
        get(source.url())
    assert gate.store.url_record(source.url()) is None


# --- redirects are not followed -------------------------------------------------------


def test_a_redirect_to_another_host(get: Get, source: SourceSimulator) -> None:
    source.scenario = Respond(
        302, b"", headers={"Location": "https://login.example.com/sso"}
    )
    with pytest.raises(UnexpectedContent, match="another host"):
        get(source.url())
    assert source.counter.count == 1


def test_a_redirect_on_the_same_host(get: Get, source: SourceSimulator) -> None:
    source.scenario = Respond(301, b"", headers={"Location": "/moved.json"})
    with pytest.raises(UnexpectedContent, match="not followed"):
        get(source.url("/data.json"))
    assert source.counter.count == 1


# --- the gate comes first -------------------------------------------------------------


def test_a_refused_request_never_leaves(get: Get, source: SourceSimulator) -> None:
    get(source.url())
    with pytest.raises(Refused):
        get(source.url())
    assert source.counter.count == 1


# --- one way to read, for every client ------------------------------------------------

SERIES = json.dumps({"series": list(range(2000))}).encode()


def test_it_asks_for_what_the_core_decodes(get: Get, source: SourceSimulator) -> None:
    get(source.url())
    assert source.counter.requests[0].headers["accept-encoding"] == "gzip, deflate"


def test_a_gzip_answer_counts_the_same_in_every_client(
    get: Get, gate: Gate, source: SourceSimulator
) -> None:
    packed = gzip.compress(SERIES)
    source.scenario = Respond(body=packed, headers={"Content-Encoding": "gzip"})
    result = get(source.url())
    assert result.content == {"series": list(range(2000))}
    assert (result.wire_bytes, result.decoded_bytes) == (len(packed), len(SERIES))


@pytest.mark.parametrize("expect", ["json", "text", "bytes"])
def test_a_truncated_gzip_is_not_ready(
    get: Get, gate: Gate, source: SourceSimulator, expect: str
) -> None:
    packed = gzip.compress(SERIES)
    cut = packed[: len(packed) // 2]  # framed right: Content-Length matches
    source.scenario = Respond(
        body=cut, content_type="text/plain", headers={"Content-Encoding": "gzip"}
    )
    with pytest.raises(SourceNotReady):
        get(source.url(), expect=expect)
    outcome = last_row(gate).outcome
    assert outcome is not None
    assert (outcome.failure, outcome.wire_bytes) == ("not_ready", len(cut))


def test_one_user_agent_whatever_its_case(get: Get, source: SourceSimulator) -> None:
    get(source.url(), headers={"user-agent": "mine/1.0"})
    assert source.counter.requests[0].headers["user-agent"] == "mine/1.0"


@pytest.mark.parametrize("kind", KINDS)
def test_a_dripping_source_is_cut_at_the_deadline(
    kind: str, gate: Gate, source: SourceSimulator
) -> None:
    source.scenario = Drip(every=0.05)  # about ten seconds for the whole body
    get = getter(kind, gate, deadline=timedelta(milliseconds=500))
    started = time.monotonic()
    with pytest.raises(FetchFailed) as failed:
        get(source.url())
    assert failed.value.shape == FailureShape.DEADLINE
    assert time.monotonic() - started < 5
    outcome = last_row(gate).outcome
    assert outcome is not None
    assert (outcome.failure, outcome.status) == ("deadline", 200)
    assert 0 < (outcome.wire_bytes or 0) < 100


def test_a_large_body_stops_at_max_bytes(
    get: Get, gate: Gate, source: SourceSimulator
) -> None:
    big = b'{"data": "' + b"x" * 3_000_000 + b'"}'
    source.scenario = Respond(body=big)
    with pytest.raises(BodyTooLarge):
        get(source.url(), max_bytes="10kB")
    outcome = last_row(gate).outcome
    assert outcome is not None
    assert outcome.failure == "too_large"
    assert 10_000 < (outcome.wire_bytes or 0) < len(big)
