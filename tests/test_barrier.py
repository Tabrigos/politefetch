"""The socket barrier: real calls fail, loopback and Unix sockets pass.

This is the proof required by rule 5 of SPEC.md. It uses a real public IP on
purpose: TEST-NET addresses (192.0.2.0/24 and friends) are not global, so a
barrier that only stopped global traffic would let them through and the proof
would prove nothing.
"""

import asyncio
import importlib.util
import ipaddress
import socket
import ssl
import sys
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from hypothesis import given
from hypothesis import strategies as st

from politefetch.testing import barrier
from politefetch.testing.barrier import (
    NetworkBarrier,
    NetworkBlockedError,
    is_allowed_address,
)

# These tests hit the barrier on purpose.
pytestmark = pytest.mark.politefetch_expects_blocked

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


@pytest.mark.skipif(sys.platform == "win32", reason="no sendmsg on Windows")
def test_udp_sendmsg_is_blocked() -> None:
    if sys.platform != "win32":  # the check mypy understands
        with (
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock,
            pytest.raises(NetworkBlockedError),
        ):
            sock.sendmsg([b"query"], [], 0, (PUBLIC_IP, 53))


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


def test_asyncio_udp_connect_is_blocked() -> None:
    async def connect() -> None:
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, remote_addr=(PUBLIC_IP, 53)
        )

    with pytest.raises(NetworkBlockedError):
        asyncio.run(connect())


def test_asyncio_udp_sendto_is_blocked(network_barrier: NetworkBarrier) -> None:
    # The transport reports a failed send to the loop's exception handler
    # instead of raising it: look at what the barrier recorded.
    async def send() -> None:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, family=socket.AF_INET
        )
        loop.set_exception_handler(lambda loop, context: None)
        try:
            transport.sendto(b"query", (PUBLIC_IP, 53))
            await asyncio.sleep(0.1)
        finally:
            transport.close()

    asyncio.run(send())
    assert network_barrier.blocked == [f"{PUBLIC_IP}:53"]


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


@pytest.mark.skipif(sys.platform == "win32", reason="no AF_UNIX on Windows")
def test_unix_sockets_pass(tmp_path: Path) -> None:
    if sys.platform != "win32":  # the check mypy understands
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


@pytest.mark.skipif(sys.platform != "win32", reason="the proactor is on Windows only")
def test_disable_restores_the_proactor(network_barrier: NetworkBarrier) -> None:
    proactor = importlib.import_module("asyncio.windows_events").IocpProactor
    guarded = (proactor.connect, proactor.sendto)
    network_barrier.disable()
    try:
        restored = (proactor.connect, proactor.sendto)
    finally:
        network_barrier.enable()
    assert all(a is not b for a, b in zip(restored, guarded, strict=True))
    assert {f.__module__ for f in restored} == {"asyncio.windows_events"}


class SocketWithoutSendmsg:
    """`socket.socket` as Windows has it: no `sendmsg`."""

    family = socket.AF_INET

    def __init__(self) -> None:
        self.reached: list[object] = []

    def connect(self, address: object) -> None:
        self.reached.append(address)

    connect_ex = connect

    def sendto(self, data: bytes, address: object) -> None:
        self.reached.append(address)

    def close(self) -> None:
        pass


def load_fresh_barrier() -> ModuleType:
    """Import a second copy of the barrier module, as a new process would."""
    spec = importlib.util.spec_from_file_location("fresh_barrier", barrier.__file__)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_barrier_works_where_sockets_have_no_sendmsg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The plugin is a pytest11 entry point: if the barrier does not import,
    # pytest breaks in every project that installs politefetch on Windows.
    monkeypatch.setattr(socket, "socket", SocketWithoutSendmsg)
    fresh = load_fresh_barrier()
    sock = SocketWithoutSendmsg()
    with fresh.NetworkBarrier():
        with pytest.raises(fresh.NetworkBlockedError):
            sock.connect((PUBLIC_IP, 443))
        with pytest.raises(fresh.NetworkBlockedError):
            sock.sendto(b"query", (PUBLIC_IP, 53))
        sock.connect(("127.0.0.1", 8080))
    assert sock.reached == [("127.0.0.1", 8080)]
    assert not hasattr(SocketWithoutSendmsg, "sendmsg")


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


# --- private networks, when listed -----------------------------------------

PRIVATE_RANGES = [
    ipaddress.ip_network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
]
EVERYTHING = [ipaddress.ip_network("0.0.0.0/0"), ipaddress.ip_network("::/0")]


@st.composite
def private_networks(
    draw: st.DrawFn,
) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    """A network inside one of the private ranges, of any size."""
    whole = draw(st.sampled_from(PRIVATE_RANGES))
    prefix = draw(st.integers(whole.prefixlen, whole.max_prefixlen))
    offset = draw(st.integers(0, whole.num_addresses - 1))
    return ipaddress.ip_network((whole.network_address + offset, prefix), strict=False)


def family_of(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> int:
    return socket.AF_INET if ip.version == 4 else socket.AF_INET6


def reached(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """The address a connection to `ip` reaches: IPv4-mapped is IPv4."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def test_a_listed_private_network_passes_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A fake socket: a listed address must reach connect without a packet
    # leaving the machine.
    monkeypatch.setattr(socket, "socket", SocketWithoutSendmsg)
    fresh = load_fresh_barrier()
    sock = SocketWithoutSendmsg()
    with fresh.NetworkBarrier(private_networks=["10.0.0.0/8"]) as guard:
        sock.connect(("10.1.2.3", 5432))
        for address in (PUBLIC_IP, "192.168.1.1", "172.17.0.2", TEST_NET_IP):
            with pytest.raises(fresh.NetworkBlockedError):
                sock.connect((address, 5432))
        with pytest.raises(fresh.NetworkBlockedError):
            sock.sendto(b"query", (PUBLIC_IP, 53))
        sock.sendto(b"query", ("10.9.9.9", 53))
    assert sock.reached == [("10.1.2.3", 5432), ("10.9.9.9", 53)]
    assert guard.blocked == [
        f"{PUBLIC_IP}:5432",
        "192.168.1.1:5432",
        "172.17.0.2:5432",
        f"{TEST_NET_IP}:5432",
        f"{PUBLIC_IP}:53",
    ]


@pytest.fixture
def listed(network_barrier: NetworkBarrier) -> Iterator[NetworkBarrier]:
    """The suite's barrier, replaced by one that lets 10.0.0.0/8 through."""
    network_barrier.disable()
    try:
        with NetworkBarrier(private_networks=["10.0.0.0/8"]) as barrier:
            yield barrier
    finally:
        network_barrier.enable()


def test_a_public_address_is_still_blocked_with_networks_listed(
    listed: NetworkBarrier,
) -> None:
    assert NetworkBarrier.current() is listed
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(NetworkBlockedError, match=PUBLIC_IP),
    ):
        sock.connect((PUBLIC_IP, 443))
    with pytest.raises(NetworkBlockedError):
        urllib.request.urlopen(f"http://{PUBLIC_IP}/", timeout=5)
    assert listed.blocked == [f"{PUBLIC_IP}:443", f"{PUBLIC_IP}:80"]


@pytest.mark.usefixtures("listed")
def test_names_are_still_looked_up_only_for_loopback() -> None:
    # A lookup sends a packet to a DNS server: listing networks does not
    # open it.
    with pytest.raises(NetworkBlockedError):
        socket.getaddrinfo("db.internal", 5432)
    assert socket.getaddrinfo("10.1.2.3", 5432)


def test_the_listed_networks_are_parsed() -> None:
    barrier = NetworkBarrier(
        private_networks=["10.0.0.0/8", "172.17.0.0/16", "192.168.1.5", "fd12::/16"]
    )
    assert barrier.private_networks == (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.17.0.0/16"),
        ipaddress.ip_network("192.168.1.5/32"),
        ipaddress.ip_network("fd12::/16"),
    )
    assert NetworkBarrier().private_networks == ()


@pytest.mark.parametrize(
    "network",
    [
        "1.1.1.0/24",  # public
        "0.0.0.0/0",  # everything
        "::/0",
        "8.0.0.0/6",  # holds 10.0.0.0/8 and public addresses
        "10.0.0.0/7",  # 10.0.0.0/8 and the public 11.0.0.0/8
        "10.0.0.1/8",  # host bits set
        "169.254.0.0/16",  # link-local: the metadata service of clouds
        "100.64.0.0/10",  # shared address space of providers
        "192.0.2.0/24",  # TEST-NET
        "2001:db8::/32",
        "::ffff:10.0.0.0/104",  # IPv4-mapped
        "not a network",
    ],
)
def test_a_network_that_is_not_private_is_refused(network: str) -> None:
    with pytest.raises(ValueError, match="private network"):
        NetworkBarrier(private_networks=[network])


@given(st.lists(private_networks(), max_size=3), st.ip_addresses())
def test_an_address_passes_only_if_loopback_or_listed(
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> None:
    allowed = is_allowed_address(family_of(ip), (str(ip), 443), networks)
    target = reached(ip)
    assert allowed == (target.is_loopback or any(target in n for n in networks))


@given(private_networks(), st.data())
def test_every_address_of_a_listed_network_passes(
    network: ipaddress.IPv4Network | ipaddress.IPv6Network, data: st.DataObject
) -> None:
    offset = data.draw(st.integers(0, network.num_addresses - 1))
    ip = network.network_address + offset
    assert is_allowed_address(family_of(ip), (str(ip), 5432), [network])


@given(st.ip_addresses())
def test_no_global_address_passes_whatever_is_listed(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> None:
    # Even networks handed to the check without going through the barrier,
    # which refuses them, open only the private ranges.
    for networks in (PRIVATE_RANGES, EVERYTHING):
        allowed = is_allowed_address(family_of(ip), (str(ip), 443), networks)
        target = reached(ip)
        assert allowed == (
            target.is_loopback or any(target in n for n in PRIVATE_RANGES)
        )
        if ip.is_global:
            assert not allowed


@given(st.ip_addresses(), st.integers(0, 128))
def test_the_barrier_accepts_only_networks_inside_the_private_ranges(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address, prefix: int
) -> None:
    network = ipaddress.ip_network((ip, min(prefix, ip.max_prefixlen)), strict=False)
    inside = any(
        network.version == whole.version and network.subnet_of(whole)  # type: ignore[arg-type]
        for whole in PRIVATE_RANGES
    )
    try:
        NetworkBarrier(private_networks=[network])
    except ValueError:
        assert not inside
    else:
        assert inside
        assert not network.network_address.is_global
        assert not network.broadcast_address.is_global
