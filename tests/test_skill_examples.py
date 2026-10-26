"""What the skill says about politefetch in particular: its examples run
against the simulator, and its tables name every refusal reason, failure
shape and error.

The checks every library shares (frontmatter, reference, names, wheel, the
shared module) are in `test_skill.py`, a copy of the common template.
"""

import contextlib
import gzip
import importlib
import inspect
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import politefetch
from politefetch import errors
from politefetch.adapters import FailureShape
from politefetch.clock import FakeClock
from politefetch.errors import RefusalReason
from politefetch.gate import Gate
from politefetch.skill import read_skill
from politefetch.store import MemoryStore
from politefetch.testing.simulator import Respond, SourceSimulator

SKILL = read_skill()
BODY = SKILL.split("\n---\n", 1)[1]
FENCE = re.compile(r"^```(\w*)[^\n]*\n(.*?)^```$", re.MULTILINE | re.DOTALL)
PROSE = FENCE.sub("", BODY)
T = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def _blocks(kind: str) -> list[str]:
    return [body for k, body in FENCE.findall(BODY) if k == kind]


# --- the tables name everything a caller can meet -------------------------------------


def test_every_refusal_reason_and_failure_shape_is_explained() -> None:
    assert len(RefusalReason) >= 5
    assert len(FailureShape) >= 8
    for reason in RefusalReason:
        assert f'`"{reason.value}"`' in BODY, reason
    for shape in FailureShape:
        assert f'`"{shape.value}"`' in BODY, shape


def test_every_error_is_in_the_table() -> None:
    rows = set(re.findall(r"^\| `(\w+)` \|", BODY, re.MULTILINE))
    raised = {
        name
        for name in errors.__all__
        if isinstance(getattr(errors, name), type)
        and issubclass(getattr(errors, name), errors.PoliteFetchError)
        and name not in {"PoliteFetchError", "ContentError"}
    }
    assert len(raised) >= 9
    assert raised <= rows, sorted(raised - rows)


def _parameters(value: object) -> set[str]:
    target = value.fget if isinstance(value, property) else value
    if not callable(target):
        return set()
    with contextlib.suppress(TypeError, ValueError):
        return set(inspect.signature(target).parameters)
    return set()


def _parameters_by_name() -> dict[str, set[str]]:
    """For each public function or method name, the parameters of every
    callable with that name."""
    import build_skill

    found: dict[str, set[str]] = {}
    for module_name in build_skill.MODULES:
        module = importlib.import_module(module_name)
        for name in getattr(module, "__all__", []):
            value = getattr(module, name)
            members = [(name, value)]
            if inspect.isclass(value):
                members += [
                    (n, m)
                    for n, m in inspect.getmembers(value)
                    if not n.startswith("_")
                ]
            for member_name, member in members:
                params = _parameters(member)
                if params:
                    found.setdefault(member_name, set()).update(params)
    return found


def test_keyword_arguments_belong_to_their_call() -> None:
    known = _parameters_by_name()
    spans = re.findall(r"`([^`]+)`", PROSE)  # code spans, over line breaks too
    calls = 0
    wrong = set()
    for span in spans:
        for call, args in re.findall(r"(\w+)\(([^()]*)\)", span.replace("\n", " ")):
            if call not in known:
                continue
            calls += 1
            for keyword in re.findall(r"(\w+)=", re.sub(r'"[^"]*"', '""', args)):
                if keyword not in known[call]:
                    wrong.add(f"{call}({keyword}=)")
    assert calls >= 3
    assert not wrong, sorted(wrong)


# --- the examples run --------------------------------------------------------


@pytest.fixture
def examples() -> dict[str, Any]:
    namespace: dict[str, Any] = {"__name__": "skill"}
    blocks = _blocks("python")
    assert len(blocks) >= 3
    for number, code in enumerate(blocks):
        exec(compile(code, f"SKILL.md python block {number}", "exec"), namespace)
    return namespace


def kp_rows(newest: datetime) -> bytes:
    times = [(newest - timedelta(hours=3 * i)).replace(tzinfo=None) for i in (1, 0)]
    rows = ",".join(f'{{"time_tag": "{t.isoformat()}", "Kp": 2.33}}' for t in times)
    return f"[{rows}]".encode()


def test_the_polling_example_runs(
    examples: dict[str, Any], source_simulator: SourceSimulator
) -> None:
    source_simulator.scenario = Respond(body=kp_rows(T - timedelta(hours=1)))
    url = source_simulator.url("/kp.json")
    clock = FakeClock(T)
    policy = next(iter(examples["POLICIES"].values()))
    gate = Gate(MemoryStore(), {"127.0.0.1": policy}, clock=clock)
    rows = examples["poll"](gate, url)
    assert rows[-1]["Kp"] == 2.33
    assert examples["poll"](gate, url) is None  # too early: refused, not sent
    assert source_simulator.counter.count == 1


def test_the_attempt_example_runs(
    examples: dict[str, Any], source_simulator: SourceSimulator
) -> None:
    source_simulator.scenario = Respond(body=b'{"ok": true}', headers={"ETag": '"v1"'})
    url = source_simulator.url("/data.json")
    clock = FakeClock(T)
    gate = Gate(
        MemoryStore(), {"127.0.0.1": politefetch.HostPolicy(5, "1m")}, clock=clock
    )
    assert examples["fetch_with_my_client"](gate, url) == b'{"ok": true}'
    status = gate.status(url)
    assert status.requests_today == 1
    [row] = status.recent
    assert row.outcome is not None
    assert row.outcome.status == 200
    clock.advance(timedelta(minutes=1))
    with gate.attempt(url) as attempt:
        assert attempt.conditional_headers == {"If-None-Match": '"v1"'}
        attempt.response(304)


def test_the_attempt_example_records_the_bytes_as_transferred(
    examples: dict[str, Any], source_simulator: SourceSimulator
) -> None:
    body = b'{"ok": true, "padding": "' + b"x" * 2000 + b'"}'
    compressed = gzip.compress(body)
    source_simulator.scenario = Respond(
        body=compressed, headers={"Content-Encoding": "gzip"}
    )
    url = source_simulator.url("/data.json")
    gate = Gate(
        MemoryStore(),
        {"127.0.0.1": politefetch.HostPolicy(5, "1m")},
        clock=FakeClock(T),
    )
    assert examples["fetch_with_my_client"](gate, url) == body
    [row] = gate.status(url).recent
    assert row.outcome is not None
    assert row.outcome.wire_bytes == len(compressed)
    assert row.outcome.decoded_bytes == len(body)


def test_the_test_example_passes(
    examples: dict[str, Any], source_simulator: SourceSimulator
) -> None:
    examples["test_ingest_gives_up_politely"](source_simulator)
