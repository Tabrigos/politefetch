"""Shared fixtures: every test runs behind the network barrier."""

from collections.abc import Iterator

import pytest

from politefetch.testing.barrier import NetworkBarrier


@pytest.fixture(autouse=True)
def network_barrier() -> Iterator[NetworkBarrier]:
    """Block every outbound connection except loopback and Unix sockets."""
    with NetworkBarrier() as barrier:
        yield barrier
