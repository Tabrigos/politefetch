"""A socket barrier for tests: no packet leaves the machine.

While a `NetworkBarrier` is active, every attempt to reach another machine
raises `NetworkBlockedError` before any system call is made: TCP and UDP
connections, unconnected UDP sends and DNS lookups. Loopback addresses
(127.0.0.0/8, ::1, "localhost") and Unix sockets pass, so a local source
simulator and a local Postgres keep working.

A barrier can also let through the private networks it is given, for
services in containers next to the tests. Only networks inside 10.0.0.0/8,
172.16.0.0/12, 192.168.0.0/16 and fc00::/7 are accepted, and the check of
every address enforces those ranges again: the barrier never becomes a way
out to the Internet. Host names still need a DNS lookup, which stays blocked.

The barrier patches the `socket` module process-wide, so only one can be
active at a time. It does not reach child processes. On Windows it also
patches the proactor of asyncio's default event loop, which connects and
sends without going through `socket.socket`.
"""

import ipaddress
import socket
import sys
from collections.abc import Callable, Iterable
from types import TracebackType
from typing import Any, Self

__all__ = ["NetworkBarrier", "NetworkBlockedError", "is_allowed_address"]

# A plain alias, not `type`: the API reference then shows the two classes.
Network = ipaddress.IPv4Network | ipaddress.IPv6Network

# The only networks a barrier may let through besides loopback: RFC 1918 and
# the IPv6 unique local addresses. Link-local (the metadata service of clouds)
# and the shared address space of providers stay out.
_PRIVATE_RANGES: tuple[Network, ...] = tuple(
    ipaddress.ip_network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)

_AF_UNIX: int | None = getattr(socket, "AF_UNIX", None)

# Windows sockets have no sendmsg: guard the methods this platform has.
_SOCKET_METHODS = tuple(
    name
    for name in ("connect", "connect_ex", "sendto", "sendmsg")
    if hasattr(socket.socket, name)
)
_MODULE_FUNCTIONS = (
    "getaddrinfo",
    "gethostbyname",
    "gethostbyname_ex",
    "gethostbyaddr",
)

_ORIGINALS: dict[str, Callable[..., Any]] = {
    **{name: getattr(socket.socket, name) for name in _SOCKET_METHODS},
    **{name: getattr(socket, name) for name in _MODULE_FUNCTIONS},
}

_PROACTOR: type[Any] | None = None
if sys.platform == "win32":
    # The proactor connects with ConnectEx/WSAConnect and sends with
    # WSASendTo, straight from _overlapped.
    from asyncio.windows_events import IocpProactor

    _PROACTOR = IocpProactor

_PROACTOR_METHODS = ("connect", "sendto")
_PROACTOR_ORIGINALS: dict[str, Callable[..., Any]] = (
    {name: getattr(_PROACTOR, name) for name in _PROACTOR_METHODS}
    if _PROACTOR is not None
    else {}
)


class NetworkBlockedError(RuntimeError):
    """A test tried to reach the network.

    Deliberately not an `OSError`: code under test that catches `OSError` to
    record a failed connection must not mistake the barrier for a source that
    is down.
    """

    def __init__(self, target: str) -> None:
        super().__init__(target)
        self.target = target

    def __str__(self) -> str:
        return f"network access blocked in tests: {self.target}"


def _is_loopback_host(host: object) -> bool:
    if isinstance(host, bytes):
        try:
            host = host.decode("ascii")
        except UnicodeDecodeError:
            return False
    if not isinstance(host, str):
        return False
    if host.lower() == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped.is_loopback
    return ip.is_loopback


def _is_ip_literal(host: object) -> bool:
    if isinstance(host, bytes):
        try:
            host = host.decode("ascii")
        except UnicodeDecodeError:
            return False
    if not isinstance(host, str):
        return False
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return True


def _never(_: object) -> bool:
    return False


def _is_private(network: Network) -> bool:
    return any(
        network.version == whole.version and network.subnet_of(whole)  # type: ignore[arg-type]
        for whole in _PRIVATE_RANGES
    )


def _private_networks(values: Iterable[str | Network]) -> tuple[Network, ...]:
    """Parse the networks a barrier lets through; refuse any that is not private."""
    networks = []
    for value in values:
        try:
            network = (
                value
                if isinstance(value, ipaddress.IPv4Network | ipaddress.IPv6Network)
                else ipaddress.ip_network(value.strip())
            )
        except ValueError as error:
            raise ValueError(f"not a private network: {value!r} ({error})") from None
        if not _is_private(network):
            ranges = ", ".join(str(whole) for whole in _PRIVATE_RANGES)
            raise ValueError(
                f"not a private network: {value!r}; the barrier lets through "
                f"only networks inside {ranges}"
            )
        networks.append(network)
    return tuple(networks)


def _in_private_network(host: object, networks: Iterable[Network]) -> bool:
    if isinstance(host, bytes):
        try:
            host = host.decode("ascii")
        except UnicodeDecodeError:
            return False
    if not isinstance(host, str):
        return False
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # The private ranges again: a network handed here without going through
    # the barrier's check opens nothing more than them.
    if not any(ip in whole for whole in _PRIVATE_RANGES):
        return False
    return any(ip in network for network in networks)


def is_allowed_address(
    family: int, address: object, private_networks: Iterable[Network] = ()
) -> bool:
    """Tell whether a socket of `family` may reach `address`.

    Unix sockets pass; IPv4 and IPv6 pass only towards loopback and towards
    the `private_networks` given, and only where those lie inside the private
    ranges; every other family is blocked.
    """
    if _AF_UNIX is not None and family == _AF_UNIX:
        return True
    if family in (socket.AF_INET, socket.AF_INET6):
        if isinstance(address, tuple) and address:
            return _is_loopback_host(address[0]) or _in_private_network(
                address[0], private_networks
            )
        return False
    return False


def _describe(address: object) -> str:
    if isinstance(address, tuple) and len(address) >= 2:
        return f"{address[0]}:{address[1]}"
    return repr(address)


class NetworkBarrier:
    """Block network access for as long as it is active.

    Use it as a context manager, or call `enable` and `disable`. Every blocked
    attempt is appended to `blocked` as "host:port" or the lookup target.
    `private_networks` ("10.0.0.0/8", "fd00::/8", or network objects) are let
    through as well; one that is not inside the private ranges raises
    `ValueError`.
    """

    _active: "NetworkBarrier | None" = None

    def __init__(self, private_networks: Iterable[str | Network] = ()) -> None:
        self.blocked: list[str] = []
        self._private_networks = _private_networks(private_networks)

    @property
    def private_networks(self) -> tuple[Network, ...]:
        """The private networks this barrier lets through, besides loopback."""
        return self._private_networks

    @property
    def active(self) -> bool:
        return NetworkBarrier._active is self

    @classmethod
    def current(cls) -> "NetworkBarrier | None":
        """The barrier active right now, if any."""
        return cls._active

    @staticmethod
    def original(name: str) -> Callable[..., Any]:
        """Return the unpatched `socket` function or method called `name`."""
        return _ORIGINALS[name]

    def enable(self) -> None:
        if NetworkBarrier._active is not None:
            raise RuntimeError("a NetworkBarrier is already active")
        for name in _SOCKET_METHODS:
            setattr(socket.socket, name, self._guard_method(name))
        for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex"):
            setattr(socket, name, self._guard_lookup(name, _is_ip_literal))
        # A reverse lookup of a literal still asks a DNS server.
        socket.gethostbyaddr = self._guard_lookup("gethostbyaddr", _never)
        if _PROACTOR is not None:
            for name in _PROACTOR_METHODS:
                setattr(_PROACTOR, name, self._guard_proactor(name))
        NetworkBarrier._active = self

    def disable(self) -> None:
        if NetworkBarrier._active is not self:
            return
        for name in _SOCKET_METHODS:
            setattr(socket.socket, name, _ORIGINALS[name])
        for name in _MODULE_FUNCTIONS:
            setattr(socket, name, _ORIGINALS[name])
        if _PROACTOR is not None:
            for name in _PROACTOR_METHODS:
                setattr(_PROACTOR, name, _PROACTOR_ORIGINALS[name])
        NetworkBarrier._active = None

    def __enter__(self) -> Self:
        self.enable()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.disable()

    def _block(self, target: str) -> NetworkBlockedError:
        self.blocked.append(target)
        return NetworkBlockedError(target)

    def _guard_method(self, name: str) -> Callable[..., Any]:
        original = _ORIGINALS[name]

        def guarded(sock: socket.socket, *args: Any) -> Any:
            address = _destination(name, args)
            if address is not None and not is_allowed_address(
                sock.family, address, self._private_networks
            ):
                if name in ("connect", "connect_ex"):
                    # Callers such as socket.create_connection only close the
                    # socket on OSError; close it here so it does not leak.
                    sock.close()
                raise self._block(_describe(address))
            return original(sock, *args)

        guarded.__name__ = name
        guarded.__doc__ = original.__doc__
        return guarded

    def _guard_proactor(self, name: str) -> Callable[..., Any]:
        original = _PROACTOR_ORIGINALS[name]

        def guarded(
            proactor: object, conn: socket.socket, *args: Any, **kwargs: Any
        ) -> Any:
            address = _proactor_destination(name, args, kwargs)
            if address is not None and not is_allowed_address(
                conn.family, address, self._private_networks
            ):
                if name == "connect":
                    conn.close()
                raise self._block(_describe(address))
            return original(proactor, conn, *args, **kwargs)

        guarded.__name__ = name
        guarded.__doc__ = original.__doc__
        return guarded

    def _guard_lookup(
        self, name: str, literal_ok: Callable[[object], bool]
    ) -> Callable[..., Any]:
        original = _ORIGINALS[name]

        def guarded(host: object, *args: Any, **kwargs: Any) -> Any:
            if (
                host is not None
                and not _is_loopback_host(host)
                and not literal_ok(host)
            ):
                raise self._block(str(host))
            return original(host, *args, **kwargs)

        guarded.__name__ = name
        guarded.__doc__ = original.__doc__
        return guarded


def _destination(method: str, args: tuple[Any, ...]) -> object | None:
    """Extract the destination address from a socket method's arguments.

    `None` means the call carries no address (a send on a connected socket),
    so the destination was already checked at connect time.
    """
    if method in ("connect", "connect_ex"):
        return args[0] if args else None
    if method == "sendto":
        # sendto(data, address) or sendto(data, flags, address)
        return args[-1] if len(args) >= 2 else None
    if method == "sendmsg":
        # sendmsg(buffers[, ancdata[, flags[, address]]])
        return args[3] if len(args) >= 4 else None
    return None


def _proactor_destination(
    method: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> object | None:
    """Extract the destination address from an `IocpProactor` method's
    arguments, after the socket: connect(address), sendto(buf, flags, addr)."""
    if method == "connect":
        return args[0] if args else kwargs.get("address")
    if method == "sendto":
        return args[2] if len(args) >= 3 else kwargs.get("addr")
    return None
