"""The socket barrier: real calls fail, loopback and Unix sockets pass.

This is the proof required by rule 5 of SPEC.md. It uses a real public IP on
purpose: TEST-NET addresses (192.0.2.0/24 and friends) are not global, so a
barrier that only stopped global traffic would let them through and the proof
would prove nothing.
"""

import asyncio
import ipaddress
import socket
import ssl
import urllib.request
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from politefetch.testing.barrier import (
    NetworkBarrier,
    NetworkBlockedError,
    is_allowed_address,
)

PUBLIC_IP = "1.1.1.1"  # a real, routable address
TEST_NET_IP = "192.0.2.1"

LOOPBACK_V4 = ipaddress.ip_network("127.0.0.0/8")
LOOPBACK_V6 = ipaddress.ip_address("::1")


# --- the proof -------------------------------------------------------------


def test_the_proof_uses_a_really_public_address() -> None:
    assert ipaddress.ip_address(PUBLIC_IP).is_global
    assert not ipaddress.ip_address(TEST_NET_IP).is_global


def test_a_real_call_fails(network_barrier: NetworkBarrier) -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(NetworkBlockedError, match=PUBLIC_IP),
    ):
        sock.connect((PUBLIC_IP, 443))
    assert network_barrier.blocked == [f"{PUBLIC_IP}:443"]


def test_a_simulated_call_passes() -> None:
    with (
        socket.create_server(("127.0.0.1", 0)) as server,
        socket.create_connection(server.getsockname(), timeout=5) as client,
    ):
        peer, _ = server.accept()
        with peer:
            client.sendall(b"ping")
            assert peer.recv(4) == b"ping"


def test_the_barrier_is_active_without_asking_for_it() -> None:
    # This test does not request the fixture: the barrier is autouse.
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(NetworkBlockedError),
    ):
        sock.connect((PUBLIC_IP, 80))


def test_blocked_error_is_not_an_os_error() -> None:
    # Code under test catches OSError to record "connection failed"; a blocked
    # call must not be mistaken for a source that is down.
    assert not issubclass(NetworkBlockedError, OSError)


# --- what is blocked -------------------------------------------------------


@pytest.mark.parametrize("address", [TEST_NET_IP, "10.0.0.1", "192.168.1.1"])
def test_private_and_test_networks_are_blocked_too(address: str) -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(NetworkBlockedError),
    ):
        sock.connect((address, 80))


def test_connect_ex_is_blocked() -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(NetworkBlockedError),
    ):
        sock.connect_ex((PUBLIC_IP, 80))


def test_hostname_in_connect_is_blocked() -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(NetworkBlockedError),
    ):
        sock.connect(("example.org", 80))


@pytest.mark.parametrize(
    "resolve",
    [
        lambda: socket.getaddrinfo("example.org", 443),
        lambda: socket.gethostbyname("example.org"),
        lambda: socket.gethostbyname_ex("example.org"),
        lambda: socket.gethostbyaddr(PUBLIC_IP),
    ],
    ids=["getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"],
)
def test_dns_lookups_are_blocked(resolve: object) -> None:
    assert callable(resolve)
    with pytest.raises(NetworkBlockedError):
        resolve()


def test_udp_sendto_is_blocked() -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock,
        pytest.raises(NetworkBlockedError),
    ):
        sock.sendto(b"query", (PUBLIC_IP, 53))


def test_udp_sendto_with_flags_is_blocked() -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock,
        pytest.raises(NetworkBlockedError),
    ):
        sock.sendto(b"query", 0, (PUBLIC_IP, 53))


def test_create_connection_is_blocked() -> None:
    with pytest.raises(NetworkBlockedError):
        socket.create_connection((PUBLIC_IP, 443), timeout=5)


def test_tls_connection_is_blocked() -> None:
    context = ssl.create_default_context()
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as raw,
        context.wrap_socket(raw, server_hostname="example.org") as sock,
        pytest.raises(NetworkBlockedError),
    ):
        sock.connect((PUBLIC_IP, 443))


def test_urllib_is_blocked() -> None:
    with pytest.raises(NetworkBlockedError):
        urllib.request.urlopen(f"http://{PUBLIC_IP}/", timeout=5)


def test_asyncio_is_blocked() -> None:
    async def connect() -> None:
        await asyncio.open_connection(PUBLIC_IP, 443)

    with pytest.raises(NetworkBlockedError):
        asyncio.run(connect())


def test_blocked_attempts_are_recorded(network_barrier: NetworkBarrier) -> None:
    for port in (80, 443):
        with socket.socket() as sock, pytest.raises(NetworkBlockedError):
            sock.connect((PUBLIC_IP, port))
    assert network_barrier.blocked == [f"{PUBLIC_IP}:80", f"{PUBLIC_IP}:443"]


# --- what passes -----------------------------------------------------------


def test_localhost_resolves_and_connects() -> None:
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        assert socket.getaddrinfo("localhost", port)
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            server.accept()[0].close()


def test_ipv6_loopback_passes() -> None:
    if not socket.has_ipv6:
        pytest.skip("no IPv6 in this environment")
    try:
        server = socket.create_server(("::1", 0), family=socket.AF_INET6)
    except OSError:
        pytest.skip("cannot bind ::1 in this environment")
    with server, socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(server.getsockname()[:2])
        server.accept()[0].close()


def test_unix_sockets_pass(tmp_path: Path) -> None:
    path = str(tmp_path / "s.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(path)
        server.listen()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(path)
            server.accept()[0].close()


def test_loopback_udp_passes() -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver,
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender,
    ):
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(5)
        sender.sendto(b"hi", receiver.getsockname())
        assert receiver.recv(2) == b"hi"


# --- lifecycle -------------------------------------------------------------


def test_two_barriers_cannot_be_active_at_once() -> None:
    with pytest.raises(RuntimeError, match="already"):
        NetworkBarrier().enable()


def test_disable_restores_the_socket_module(network_barrier: NetworkBarrier) -> None:
    original = NetworkBarrier.original
    network_barrier.disable()
    try:
        restored: tuple[object, ...] = (socket.socket.connect, socket.getaddrinfo)
    finally:
        network_barrier.enable()
    guarded: object = socket.socket.connect
    assert restored == (original("connect"), original("getaddrinfo"))
    assert guarded is not original("connect")
    assert network_barrier.active


def test_a_blocked_connect_closes_the_socket() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(NetworkBlockedError):
        sock.connect((PUBLIC_IP, 443))
    assert sock.fileno() == -1


# --- the rule, for every address -------------------------------------------


@given(st.ip_addresses(v=4), st.integers(min_value=0, max_value=65535))
def test_ipv4_passes_only_if_loopback(ip: ipaddress.IPv4Address, port: int) -> None:
    allowed = is_allowed_address(socket.AF_INET, (str(ip), port))
    assert allowed == (ip in LOOPBACK_V4)


@given(st.ip_addresses(v=6), st.integers(min_value=0, max_value=65535))
def test_ipv6_passes_only_if_loopback(ip: ipaddress.IPv6Address, port: int) -> None:
    allowed = is_allowed_address(socket.AF_INET6, (str(ip), port, 0, 0))
    mapped = ip.ipv4_mapped
    expected = ip == LOOPBACK_V6 or (mapped is not None and mapped in LOOPBACK_V4)
    assert allowed == expected


@given(st.ip_addresses())
def test_no_global_address_ever_passes(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> None:
    family = socket.AF_INET if ip.version == 4 else socket.AF_INET6
    if ip.is_global:
        assert not is_allowed_address(family, (str(ip), 443))


@given(st.text())
def test_unknown_hostnames_never_pass(host: str) -> None:
    if host.lower() == "localhost":
        return
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        assert not is_allowed_address(socket.AF_INET, (host, 80))
    else:
        if not ip.is_loopback:
            assert not is_allowed_address(socket.AF_INET, (host, 80))


def test_unknown_families_are_blocked() -> None:
    assert not is_allowed_address(-1, ("127.0.0.1", 80))
