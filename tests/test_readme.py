"""The README: its complete example runs, and its snippets name real things.

The example is run as it is written, with only its URL and host pointed at
the local simulator: twice as `__main__`, the way cron would start it, and
its `poll` against the bad days of a source.
"""

import ast
import contextlib
import importlib
import io
import json
import re
import textwrap
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from politefetch import Gate
from politefetch.clock import FakeClock, SystemClock
from politefetch.gate import host_of
from politefetch.store import MemoryStore
from politefetch.testing.simulator import Html, Respond, SourceSimulator

README = (Path(__file__).resolve().parent.parent / "README.md").read_text("utf-8")
BLOCK = re.compile(r"^( *)```(\w+)\n(.*?)^\1```", re.DOTALL | re.MULTILINE)
REAL_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
T = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def blocks(language: str) -> list[str]:
    return [
        textwrap.dedent(body)
        for _, lang, body in BLOCK.findall(README)
        if lang == language
    ]


EXAMPLE = blocks("python")[0]


def kp_rows(newest: datetime) -> bytes:
    """A Kp product in the SWPC format: rows every 3 hours, UTC without `Z`."""
    rows = [
        {
            "time_tag": (newest - timedelta(hours=3 * i))
            .replace(tzinfo=None)
            .isoformat(),
            "Kp": 2.33,
            "a_running": 9,
            "station_count": 8,
        }
        for i in reversed(range(4))
    ]
    return json.dumps(rows).encode()


def run(source: str, name: str) -> tuple[dict[str, Any], str]:
    namespace: dict[str, Any] = {"__name__": name}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(compile(source, "README.md", "exec"), namespace)
    return namespace, out.getvalue()


def test_the_example_is_the_one_described() -> None:
    assert REAL_URL in EXAMPLE
    assert 'if __name__ == "__main__":' in EXAMPLE
    namespace, _ = run(EXAMPLE, "readme")
    assert list(namespace["POLICIES"]) == [host_of(REAL_URL)]


def test_the_example_runs_as_a_cron_job(
    source_simulator: SourceSimulator,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    newest = SystemClock().now() - timedelta(hours=1)
    source_simulator.scenario = Respond(body=kp_rows(newest))
    url = source_simulator.url("/products/noaa-planetary-k-index.json")
    source = EXAMPLE.replace(REAL_URL, url).replace(
        '"services.swpc.noaa.gov"', f'"{host_of(url)}"'
    )
    monkeypatch.chdir(tmp_path)

    _, first = run(source, "__main__")
    line, _, rest = first.partition("\n")
    assert line == f"Kp 2.33 at {newest.replace(tzinfo=None).isoformat()}"
    status = json.loads(rest)
    assert status["host"] == "127.0.0.1"
    assert status["reason"] == "min_interval"
    assert status["today"]["requests"] == 1
    assert status["recent"][0]["status"] == 200

    # The next run is a new process, started too early: it sends nothing.
    _, second = run(source, "__main__")
    assert second.startswith("not now: 127.0.0.1: refused (min_interval)")
    assert json.loads(second.partition("\n")[2])["today"]["requests"] == 1
    assert source_simulator.counter.count == 1
    assert (tmp_path / "ingest.db").exists()


@pytest.fixture
def poll() -> Callable[..., Any]:
    namespace, _ = run(EXAMPLE, "readme")
    return namespace["poll"]  # type: ignore[no-any-return]


def make_gate(url: str, clock: FakeClock, poll: Callable[..., Any]) -> Gate:
    policy = next(iter(poll.__globals__["POLICIES"].values()))
    return Gate(MemoryStore(), {host_of(url): policy}, clock=clock)


@pytest.mark.parametrize(
    ("scenario", "printed"),
    [
        (Respond(body=kp_rows(T - timedelta(hours=7))), "rejected: "),
        (Html(), "rejected: "),
        (Respond(status=503, body=b""), "no answer: "),
        (Respond(body=b'[{"time_tag": "2026-10-16T09:00:00", "Kp": 2'), "rejected: "),
    ],
    ids=["stale", "html", "503", "truncated"],
)
def test_poll_rejects_what_cannot_be_used(
    source_simulator: SourceSimulator,
    poll: Callable[..., Any],
    capsys: pytest.CaptureFixture[str],
    scenario: Respond,
    printed: str,
) -> None:
    source_simulator.scenario = scenario
    url = source_simulator.url("/kp.json")
    clock = FakeClock(T)
    gate = make_gate(url, clock, poll)
    assert poll(gate, url) is None
    assert capsys.readouterr().out.startswith(printed)
    # The next call, too early, is refused without reaching the source.
    assert poll(gate, url) is None
    assert capsys.readouterr().out.startswith("not now: ")
    assert source_simulator.counter.count == 1


def test_poll_returns_fresh_rows(
    source_simulator: SourceSimulator, poll: Callable[..., Any]
) -> None:
    source_simulator.scenario = Respond(body=kp_rows(T - timedelta(hours=1)))
    url = source_simulator.url("/kp.json")
    rows = poll(make_gate(url, FakeClock(T), poll), url)
    assert rows[-1]["time_tag"] == "2026-10-16T11:00:00"


# --- the snippets ------------------------------------------------------------------


@pytest.mark.parametrize("source", blocks("python"))
def test_snippets_parse_and_import_real_names(source: str) -> None:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "politefetch"
        ):
            assert node.module is not None
            module = importlib.import_module(node.module)
            for alias in node.names:
                assert hasattr(module, alias.name), f"{node.module}.{alias.name}"


def test_the_status_json_has_the_real_shape() -> None:
    [shown] = blocks("json")
    data = json.loads(shown)
    clock = FakeClock(T)
    gate = make_gate_for_shape(clock)
    with gate.attempt("https://services.swpc.noaa.gov/x") as attempt:
        attempt.response(200)
    real = gate.status("services.swpc.noaa.gov").to_dict()
    assert data.keys() == real.keys()
    assert data["today"].keys() == real["today"].keys()  # type: ignore[attr-defined]
    recent = real["recent"]
    assert isinstance(recent, list)
    assert data["recent"][0].keys() == recent[0].keys()


def make_gate_for_shape(clock: FakeClock) -> Gate:
    namespace, _ = run(EXAMPLE, "readme")
    return Gate(MemoryStore(), namespace["POLICIES"], clock=clock)
