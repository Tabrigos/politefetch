"""Child processes started for tests run behind the network barrier (D-012)."""

import socket

import pytest

from politefetch.testing.barrier import NetworkBlockedError
from politefetch.testing.processes import ChildFailed, spawn

from . import children

PUBLIC_IP = "1.1.1.1"


def test_the_result_comes_back() -> None:
    assert spawn(children.add, 2, 3).join(timeout=60) == 5


def test_an_exception_comes_back() -> None:
    with pytest.raises(ValueError, match="from the child"):
        spawn(children.fail, "from the child").join(timeout=60)


def test_the_barrier_is_active_in_the_child() -> None:
    with pytest.raises(NetworkBlockedError, match=PUBLIC_IP):
        spawn(children.connect, PUBLIC_IP, 443).join(timeout=60)


def test_loopback_works_in_the_child() -> None:
    with socket.create_server(("127.0.0.1", 0)) as server:
        server.settimeout(60)
        child = spawn(children.connect, "127.0.0.1", server.getsockname()[1])
        peer, _ = server.accept()
        with peer:
            assert peer.recv(64) == b"hello from the child"
        assert child.join(timeout=60) == "connected"


def test_a_child_that_dies_reports_its_exit_code() -> None:
    with pytest.raises(ChildFailed) as failed:
        spawn(children.exit_now, 3).join(timeout=60)
    assert failed.value.exitcode == 3


def test_a_child_can_be_killed() -> None:
    child = spawn(children.sleep, 60)
    child.kill()
    with pytest.raises(ChildFailed) as failed:
        child.join(timeout=60)
    assert failed.value.exitcode == -9


def test_a_child_that_overruns_is_killed() -> None:
    child = spawn(children.sleep, 60)
    with pytest.raises(TimeoutError):
        child.join(timeout=0.5)
    assert child.exitcode == -9
