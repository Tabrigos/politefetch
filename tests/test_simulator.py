"""The source simulator: a real HTTP server on 127.0.0.1, scenario by scenario."""

import http.client
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from politefetch.testing.counter import RequestCounter
from politefetch.testing.simulator import (
    Html,
    NoAnswer,
    Reset,
    Respond,
    SourceSimulator,
    TooManyRequests,
    Truncated,
    advancing_json,
    closed_port_url,
    stuck_json,
)

T = datetime(2026, 10, 17, 12, tzinfo=UTC)


@pytest.fixture
def source() -> Iterator[SourceSimulator]:
    with SourceSimulator() as simulator:
        yield simulator


def get(url: str, timeout: float = 5) -> tuple[int, dict[str, str], bytes]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, dict(response.headers), response.read()


# --- the server -----------------------------------------------------------------------


def test_it_listens_on_loopback(source: SourceSimulator) -> None:
    assert source.url("/data.json") == f"http://127.0.0.1:{source.port}/data.json"


def test_default_answer_is_an_empty_json_object(source: SourceSimulator) -> None:
    status, headers, body = get(source.url())
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert body == b"{}"


def test_every_request_is_counted_with_its_scenario(source: SourceSimulator) -> None:
    get(source.url("/a"))
    source.scenario = Respond(body=b"[]", name="other")
    get(source.url("/b?x=1"))
    assert source.counter.count == 2
    first, second = source.counter.requests
    assert (first.number, first.method, first.path, first.scenario) == (
        1,
        "GET",
        "/a",
        "respond",
    )
    assert (second.number, second.path, second.scenario) == (2, "/b?x=1", "other")
    assert second.headers["host"] == f"127.0.0.1:{source.port}"
    assert source.counter.by_scenario() == {"respond": 1, "other": 1}


def test_body_can_depend_on_the_request(source: SourceSimulator) -> None:
    source.scenario = Respond(body=lambda request: str(request.number).encode())
    assert get(source.url())[2] == b"1"
    assert get(source.url())[2] == b"2"


def test_stop_releases_everything() -> None:
    before = threading.active_count()
    simulator = SourceSimulator(NoAnswer()).start()
    with pytest.raises((TimeoutError, urllib.error.URLError)):
        get(simulator.url(), timeout=0.2)
    simulator.stop()
    assert threading.active_count() == before
    simulator.stop()  # stopping twice is harmless


def test_a_stopped_simulator_refuses_connections() -> None:
    simulator = SourceSimulator().start()
    url = simulator.url()
    simulator.stop()
    with pytest.raises(urllib.error.URLError):
        get(url)


# --- the scenarios of SPEC.md ---------------------------------------------------------


def test_refuses() -> None:
    with pytest.raises(urllib.error.URLError) as refused:
        get(closed_port_url("/data.json"))
    assert isinstance(refused.value.reason, ConnectionRefusedError)


def test_never_answers(source: SourceSimulator) -> None:
    source.scenario = NoAnswer()
    with pytest.raises((TimeoutError, urllib.error.URLError)):
        get(source.url(), timeout=0.2)
    assert source.counter.count == 1  # it arrived: the source saw it


def test_resets(source: SourceSimulator) -> None:
    source.scenario = Reset()
    with pytest.raises((ConnectionResetError, urllib.error.URLError)):
        get(source.url())
    assert source.counter.count == 1


def test_too_many_requests(source: SourceSimulator) -> None:
    source.scenario = TooManyRequests(retry_after="120")
    with pytest.raises(urllib.error.HTTPError) as error:
        get(source.url())
    assert error.value.code == 429
    assert error.value.headers["Retry-After"] == "120"
    error.value.close()


def test_too_many_requests_without_retry_after(source: SourceSimulator) -> None:
    source.scenario = TooManyRequests()
    with pytest.raises(urllib.error.HTTPError) as error:
        get(source.url())
    assert "Retry-After" not in error.value.headers
    error.value.close()


def test_truncated_body(source: SourceSimulator) -> None:
    source.scenario = Truncated(body=b'{"time": "2026-', declared_length=200)
    with urllib.request.urlopen(source.url(), timeout=5) as response:
        assert response.headers["Content-Length"] == "200"
        with pytest.raises(http.client.IncompleteRead):
            response.read()


def test_html_instead_of_json(source: SourceSimulator) -> None:
    source.scenario = Html()
    status, headers, body = get(source.url())
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert body.lstrip().lower().startswith(b"<!doctype html>")


def test_stuck_data(source: SourceSimulator) -> None:
    source.scenario = stuck_json(T)
    bodies = [json.loads(get(source.url())[2]) for _ in range(3)]
    assert bodies == [{"time": "2026-10-17T12:00:00+00:00"}] * 3


def test_advancing_data(source: SourceSimulator) -> None:
    source.scenario = advancing_json(T, timedelta(minutes=10))
    times = [json.loads(get(source.url())[2])["time"] for _ in range(3)]
    assert times == [
        "2026-10-17T12:00:00+00:00",
        "2026-10-17T12:10:00+00:00",
        "2026-10-17T12:20:00+00:00",
    ]


def test_any_status_and_headers(source: SourceSimulator) -> None:
    source.scenario = Respond(
        503, b"busy", content_type="text/plain", headers={"Retry-After": "30"}
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        get(source.url())
    assert error.value.code == 503
    assert error.value.headers["Retry-After"] == "30"
    assert error.value.read() == b"busy"
    error.value.close()


# --- the counter on its own -----------------------------------------------------------


def test_counter_can_be_reset(source: SourceSimulator) -> None:
    get(source.url())
    source.counter.reset()
    assert source.counter.count == 0
    assert source.counter.requests == []
    get(source.url())
    assert source.counter.requests[0].number == 1


def test_counter_is_safe_across_threads() -> None:
    counter = RequestCounter()
    threads = [
        threading.Thread(
            target=lambda: [counter.record("GET", "/", {}, "s") for _ in range(200)]
        )
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert counter.count == 1600
    assert sorted(r.number for r in counter.requests) == list(range(1, 1601))
