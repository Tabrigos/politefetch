"""The politefetch pytest plugin.

Installed with politefetch and loaded by pytest automatically; it does
nothing until asked:

- `politefetch_barrier = true` in the pytest configuration puts every test
  behind the network barrier;
- the `network_barrier` fixture puts one test behind it (or returns the
  active barrier);
- the `source_simulator` fixture starts a `SourceSimulator` for one test.

A test that tried to reach the network fails at teardown even if the code
under test swallowed the `NetworkBlockedError`, unless it is marked
`politefetch_expects_blocked` (tests that prove the barrier itself).

Disable it with `-p no:politefetch`.
"""

from collections.abc import Iterator

import pytest

from politefetch.testing.barrier import NetworkBarrier
from politefetch.testing.simulator import SourceSimulator

INI_SWITCH = "politefetch_barrier"
EXPECTS_BLOCKED = "politefetch_expects_blocked"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini(
        INI_SWITCH,
        type="bool",
        default=False,
        help="politefetch: run every test behind the network barrier",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{EXPECTS_BLOCKED}: the test hits the politefetch network barrier on purpose",
    )


@pytest.fixture
def network_barrier(request: pytest.FixtureRequest) -> Iterator[NetworkBarrier]:
    """Block every outbound connection except loopback and Unix sockets."""
    with NetworkBarrier() as barrier:
        yield barrier
        blocked = list(barrier.blocked)
    if blocked and request.node.get_closest_marker(EXPECTS_BLOCKED) is None:
        pytest.fail(
            f"the test tried to reach the network: {', '.join(blocked)}",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _politefetch_barrier(request: pytest.FixtureRequest) -> None:
    if request.config.getini(INI_SWITCH):
        request.getfixturevalue("network_barrier")


@pytest.fixture
def source_simulator() -> Iterator[SourceSimulator]:
    """A simulated source on 127.0.0.1, answering 200 with `{}` until told
    otherwise."""
    with SourceSimulator() as simulator:
        yield simulator
