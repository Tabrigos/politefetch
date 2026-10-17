"""A socket barrier for tests: no packet leaves the machine.

While a `NetworkBarrier` is active, every attempt to reach another machine
raises `NetworkBlockedError` before any system call is made: TCP and UDP
connections, unconnected UDP sends and DNS lookups. Loopback addresses
(127.0.0.0/8, ::1, "localhost") and Unix sockets pass, so a local source
simulator and a local Postgres keep working.

The barrier patches the `socket` module process-wide, so only one can be
active at a time. It does not reach child processes.
"""

import ipaddress
import socket
from collections.abc import Callable
from types import TracebackType
from typing import Any, Self

__all__ = ["NetworkBarrier", "NetworkBlockedError", "is_allowed_address"]

_AF_UNIX: int | None = getattr(socket, "AF_UNIX", None)

_SOCKET_METHODS = ("connect", "connect_ex", "sendto", "sendmsg")
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


def is_allowed_address(family: int, address: object) -> bool:
    """Tell whether a socket of `family` may reach `address`.

    Unix sockets pass; IPv4 and IPv6 pass only towards loopback; every other
    family is blocked.
    """
    if _AF_UNIX is not None and family == _AF_UNIX:
        return True
    if family in (socket.AF_INET, socket.AF_INET6):
        if isinstance(address, tuple) and address:
            return _is_loopback_host(address[0])
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
    """

    _active: "NetworkBarrier | None" = None

    def __init__(self) -> None:
        self.blocked: list[str] = []

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
        NetworkBarrier._active = self

    def disable(self) -> None:
        if NetworkBarrier._active is not self:
            return
        for name in _SOCKET_METHODS:
            setattr(socket.socket, name, _ORIGINALS[name])
        for name in _MODULE_FUNCTIONS:
            setattr(socket, name, _ORIGINALS[name])
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
            if address is not None and not is_allowed_address(sock.family, address):
                if name in ("connect", "connect_ex"):
                    # Callers such as socket.create_connection only close the
                    # socket on OSError; close it here so it does not leak.
                    sock.close()
                raise self._block(_describe(address))
            return original(sock, *args)

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
