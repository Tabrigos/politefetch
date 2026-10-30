"""A free tag on each log row (#17, D-063).

The caller names the logical call a request belongs to (`tag=` on
`gate.attempt` and on the fetchers); the row keeps it, for a dashboard.
"""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from politefetch import FakeClock, Gate, HostPolicy
from politefetch.status import to_json
from politefetch.store import MemoryStore
from politefetch.testing.simulator import Respond, SourceSimulator

from .adapter_kit import KINDS as FETCHERS
from .adapter_kit import getter
from .stores import KINDS, Harness

T = datetime(2026, 10, 16, 12, tzinfo=UTC)
HOST = "example.org"
URL = "https://example.org/data.json"
POLICY = HostPolicy(daily_cap=1000, min_interval="10s")


@pytest.fixture(params=KINDS)
def harness(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Harness]:
    h = Harness(request.param, tmp_path)
    yield h
    h.close()


@pytest.fixture
def gate(harness: Harness) -> Gate:
    return Gate(harness.store, {HOST: POLICY}, clock=FakeClock(T))


def test_the_row_keeps_the_tag(gate: Gate) -> None:
    with gate.attempt(URL, tag="daily-kp") as attempt:
        assert attempt.row.tag == "daily-kp"
        attempt.response(200)
        attempt.accept()
    assert attempt.result is not None
    assert attempt.result.tag == "daily-kp"
    assert [r.tag for r in gate.store.rows(host=HOST)] == ["daily-kp"]
    assert gate.store.row(attempt.row.id).tag == "daily-kp"


def test_a_row_without_a_tag_has_none(gate: Gate) -> None:
    with gate.attempt(URL) as attempt:
        attempt.response(200)
        attempt.accept()
    assert gate.store.row(attempt.row.id).tag is None


def test_the_tag_survives_a_restart(harness: Harness, gate: Gate) -> None:
    with gate.attempt(URL, tag="daily-kp") as attempt:
        attempt.response(503)
    assert harness.restart().row(attempt.row.id).tag == "daily-kp"


def test_the_status_json_shows_the_tag(gate: Gate) -> None:
    with gate.attempt(URL, tag="daily-kp") as attempt:
        attempt.response(200)
        attempt.accept()
    (row,) = json.loads(to_json(gate.status(HOST)))["recent"]
    assert row["tag"] == "daily-kp"


@pytest.mark.parametrize("tag", ["", "x" * 201])
def test_an_empty_or_too_long_tag_is_refused_before_opening(
    gate: Gate, tag: str
) -> None:
    with pytest.raises(ValueError, match="tag"):
        gate.attempt(URL, tag=tag)
    assert gate.store.rows() == []


def test_a_tag_of_the_longest_length_is_kept(gate: Gate) -> None:
    with gate.attempt(URL, tag="x" * 200) as attempt:
        attempt.response(200)
        attempt.accept()
    assert gate.store.row(attempt.row.id).tag == "x" * 200


@pytest.mark.parametrize("kind", FETCHERS)
def test_a_fetcher_writes_the_tag(kind: str) -> None:
    gate = Gate(MemoryStore(), {"127.0.0.1": POLICY}, clock=FakeClock(T))
    with SourceSimulator(Respond(200, b'{"rows": []}')) as source:
        get = getter(kind, gate)
        get(source.url("/data.json"), tag="daily-kp")
    (row,) = gate.store.rows()
    assert row.tag == "daily-kp"
