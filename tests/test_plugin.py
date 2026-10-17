"""The pytest plugin, tried on small test suites run by pytester.

The inner sessions run in this process. The outer barrier is lifted while
they run, so that they can manage their own; their tests never try to reach
the network for real, they only look at whether a barrier is active or try
a blocked address.
"""

from collections.abc import Iterator

import pytest

from politefetch.testing.barrier import NetworkBarrier

BLOCKED_CONNECT = """
import socket
import pytest
from politefetch.testing.barrier import NetworkBlockedError

def connect():
    with socket.socket() as sock:
        sock.connect(("1.1.1.1", 443))
"""


@pytest.fixture
def inner(network_barrier: NetworkBarrier) -> Iterator[None]:
    network_barrier.disable()
    try:
        yield
    finally:
        network_barrier.enable()


def run(pytester: pytest.Pytester, ini: str, tests: str) -> pytest.RunResult:
    pytester.makeini(f"[pytest]\n{ini}\n")
    pytester.makepyfile(tests)
    return pytester.runpytest_inprocess("-p", "no:cacheprovider")


@pytest.mark.usefixtures("inner")
def test_off_by_default(pytester: pytest.Pytester) -> None:
    result = run(
        pytester,
        "",
        """
from politefetch.testing.barrier import NetworkBarrier

def test_no_barrier():
    assert NetworkBarrier.current() is None
""",
    )
    result.assert_outcomes(passed=1)


@pytest.mark.usefixtures("inner")
def test_the_ini_switch_puts_every_test_behind_the_barrier(
    pytester: pytest.Pytester,
) -> None:
    result = run(
        pytester,
        "politefetch_barrier = true",
        """
from politefetch.testing.barrier import NetworkBarrier

def test_one():
    assert NetworkBarrier.current() is not None

def test_two():
    assert NetworkBarrier.current() is not None
""",
    )
    result.assert_outcomes(passed=2)


@pytest.mark.usefixtures("inner")
def test_the_fixture_works_without_the_switch(pytester: pytest.Pytester) -> None:
    result = run(
        pytester,
        "",
        """
from politefetch.testing.barrier import NetworkBarrier

def test_asked(network_barrier):
    assert NetworkBarrier.current() is network_barrier

def test_not_asked():
    assert NetworkBarrier.current() is None
""",
    )
    result.assert_outcomes(passed=2)


@pytest.mark.usefixtures("inner")
def test_a_swallowed_blocked_call_still_fails_the_test(
    pytester: pytest.Pytester,
) -> None:
    result = run(
        pytester,
        "politefetch_barrier = true",
        BLOCKED_CONNECT
        + """
def test_swallows():
    try:
        connect()
    except Exception:
        pass  # code under test that hides the error
""",
    )
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*tried to reach the network*1.1.1.1:443*"])


@pytest.mark.usefixtures("inner")
def test_a_test_may_declare_it_expects_blocks(pytester: pytest.Pytester) -> None:
    result = run(
        pytester,
        "politefetch_barrier = true",
        BLOCKED_CONNECT
        + """
@pytest.mark.politefetch_expects_blocked
def test_proves_the_barrier(network_barrier):
    with pytest.raises(NetworkBlockedError):
        connect()
    assert network_barrier.blocked == ["1.1.1.1:443"]
""",
    )
    result.assert_outcomes(passed=1)


@pytest.mark.usefixtures("inner")
def test_the_simulator_fixture(pytester: pytest.Pytester) -> None:
    result = run(
        pytester,
        "politefetch_barrier = true",
        """
import urllib.request

def test_simulated_call(source_simulator):
    with urllib.request.urlopen(source_simulator.url(), timeout=5) as response:
        assert response.status == 200
    assert source_simulator.counter.count == 1
""",
    )
    result.assert_outcomes(passed=1)


def test_this_suite_runs_behind_the_barrier() -> None:
    assert NetworkBarrier.current() is not None
