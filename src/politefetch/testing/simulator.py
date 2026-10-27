"""Source simulator: a real HTTP server on 127.0.0.1 that plays the scenarios
of SPEC.md.

    with SourceSimulator(NoAnswer()) as source:
        ...  # requests to source.url("/data.json")
        source.scenario = Respond()  # switch scenario between requests
        assert source.counter.count == 2

Because it is a real server on loopback, any HTTP client can talk to it, and
it passes the network barrier. Every request that arrives is counted before
the scenario plays (`politefetch.testing.counter`), with the time it arrived
on the simulator's `clock` (the system clock unless given one, such as the
`FakeClock` of the gate under test).

Scenarios: `Respond` (any status, body, headers), `TooManyRequests`, `Html`,
`Truncated` (body shorter than its Content-Length), `HalfJson` (a complete
answer whose JSON stops midway), `Drip` (the body one
byte at a time), `NoAnswer` (holds the connection until the simulator
stops), `Reset` (closes with a TCP reset),
`stuck_json` and `advancing_json` (data whose time stays put or moves on),
`Validated` (answers 304 to a matching If-None-Match or If-Modified-Since).
A source that refuses connections is a closed port: `closed_port_url()`.

Every answer closes the connection (`Connection: close`). Socket timeouts
use real time: keep client timeouts short in tests that do not get answers.
A header that a request sends more than once is recorded once, its values
joined by ", ".
"""

import contextlib
import json
import socket
import struct
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from types import TracebackType
from typing import Protocol, Self

from politefetch.clock import Clock, SystemClock
from politefetch.testing.counter import RequestCounter, SourceRequest

__all__ = [
    "Drip",
    "HalfJson",
    "Html",
    "NoAnswer",
    "Reset",
    "Respond",
    "Scenario",
    "SourceSimulator",
    "TooManyRequests",
    "Truncated",
    "Validated",
    "advancing_json",
    "closed_port_url",
    "stuck_json",
]

_LOOPBACK = "127.0.0.1"
_MAX_HEAD = 64 * 1024
_IO_TIMEOUT = 5.0


class Scenario(Protocol):
    """How the simulated source answers one request.

    Attributes:
        name: What the counter records for the requests that arrive while
            the scenario plays (`RequestCounter.by_scenario`).
    """

    name: str

    def serve(
        self, conn: socket.socket, request: SourceRequest, stop: threading.Event
    ) -> None:
        """Answer (or not) on `conn`. `stop` is set when the simulator stops."""
        ...


def _send(
    conn: socket.socket,
    status: int,
    headers: Mapping[str, str],
    body: bytes,
    *,
    declared_length: int | None = None,
) -> None:
    length = len(body) if declared_length is None else declared_length
    conn.sendall(_head(status, headers, length) + body)


def _head(status: int, headers: Mapping[str, str], length: int) -> bytes:
    try:
        reason = HTTPStatus(status).phrase
    except ValueError:
        reason = ""
    lines = [
        f"HTTP/1.1 {status} {reason}",
        f"Content-Length: {length}",
        "Connection: close",
    ]
    lines += [f"{name}: {value}" for name, value in headers.items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


class Respond:
    """Answer with `status`, a body (bytes, or a function of the request) and
    extra headers."""

    def __init__(
        self,
        status: int = 200,
        body: bytes | Callable[[SourceRequest], bytes] = b"{}",
        *,
        content_type: str = "application/json",
        headers: Mapping[str, str] | None = None,
        name: str = "respond",
    ) -> None:
        self.status = status
        self.body = body
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.name = name

    def serve(
        self, conn: socket.socket, request: SourceRequest, stop: threading.Event
    ) -> None:
        body = self.body(request) if callable(self.body) else self.body
        _send(conn, self.status, self.headers, body)


class TooManyRequests(Respond):
    """429, with a Retry-After header if given."""

    def __init__(self, retry_after: str | None = None, *, name: str = "429") -> None:
        headers = {} if retry_after is None else {"Retry-After": retry_after}
        super().__init__(
            429,
            b"Too Many Requests",
            content_type="text/plain",
            headers=headers,
            name=name,
        )


class Html(Respond):
    """A maintenance page where data was expected."""

    PAGE = (
        b"<!DOCTYPE html>\n<html><head><title>Maintenance</title></head>"
        b"<body><p>We will be back soon.</p></body></html>\n"
    )

    def __init__(self, status: int = 200, *, name: str = "html") -> None:
        super().__init__(
            status, self.PAGE, content_type="text/html; charset=utf-8", name=name
        )


class Validated:
    """A resource with an ETag and/or Last-Modified: 304 when the request's
    conditional headers match, 200 with the body otherwise."""

    def __init__(
        self,
        body: bytes = b"{}",
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        content_type: str = "application/json",
        name: str = "validated",
    ) -> None:
        self.body = body
        self.etag = etag
        self.last_modified = last_modified
        self.content_type = content_type
        self.name = name

    def _not_modified(self, request: SourceRequest) -> bool:
        if_none_match = request.headers.get("if-none-match")
        if if_none_match is not None:
            return self.etag is not None and self.etag in {
                tag.strip() for tag in if_none_match.split(",")
            }
        since = request.headers.get("if-modified-since")
        if since is None or self.last_modified is None:
            return False
        try:
            return parsedate_to_datetime(self.last_modified) <= parsedate_to_datetime(
                since
            )
        except (TypeError, ValueError):
            return False

    def serve(
        self, conn: socket.socket, request: SourceRequest, stop: threading.Event
    ) -> None:
        headers = {}
        if self.etag is not None:
            headers["ETag"] = self.etag
        if self.last_modified is not None:
            headers["Last-Modified"] = self.last_modified
        if self._not_modified(request):
            _send(conn, 304, headers, b"")
        else:
            _send(conn, 200, {"Content-Type": self.content_type, **headers}, self.body)


class Truncated:
    """A body shorter than the Content-Length it announces, then the
    connection closes."""

    def __init__(
        self,
        body: bytes = b'{"time": "20',
        *,
        declared_length: int | None = None,
        name: str = "truncated",
    ) -> None:
        self.body = body
        self.declared_length = (
            declared_length if declared_length is not None else 2 * len(body) + 10
        )
        if self.declared_length <= len(body):
            raise ValueError("declared_length must exceed the body length")
        self.name = name

    def serve(
        self, conn: socket.socket, request: SourceRequest, stop: threading.Event
    ) -> None:
        _send(
            conn,
            200,
            {"Content-Type": "application/json"},
            self.body,
            declared_length=self.declared_length,
        )


class HalfJson(Respond):
    """A complete answer whose JSON stops midway: the Content-Length matches
    the body, as when a source serves a file it is still rewriting. No HTTP
    client complains; only the content check does (`Truncated` is the other
    truncation, a connection that closes before the announced length)."""

    def __init__(
        self, body: bytes = b'{"time": "20', *, name: str = "half json"
    ) -> None:
        super().__init__(200, body, name=name)


class Drip:
    """Send the head at once, then the body one byte every `every` seconds:
    a source that keeps a request alive for as long as it likes."""

    def __init__(
        self,
        body: bytes = b'{"data": "' + b"x" * 200 + b'"}',
        *,
        every: float = 0.05,
        content_type: str = "application/json",
        name: str = "drip",
    ) -> None:
        self.body = body
        self.every = every
        self.content_type = content_type
        self.name = name

    def serve(
        self, conn: socket.socket, request: SourceRequest, stop: threading.Event
    ) -> None:
        conn.sendall(_head(200, {"Content-Type": self.content_type}, len(self.body)))
        for byte in self.body:
            if stop.wait(self.every):
                return
            conn.sendall(bytes([byte]))


class NoAnswer:
    """Read the request, then say nothing until the simulator stops."""

    def __init__(self, *, name: str = "no answer") -> None:
        self.name = name

    def serve(
        self, conn: socket.socket, request: SourceRequest, stop: threading.Event
    ) -> None:
        stop.wait()


class Reset:
    """Read the request, then close the connection with a TCP reset."""

    def __init__(self, *, name: str = "reset") -> None:
        self.name = name

    def serve(
        self, conn: socket.socket, request: SourceRequest, stop: threading.Event
    ) -> None:
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        conn.close()


def stuck_json(data_time: datetime, *, name: str = "stuck data") -> Respond:
    """JSON whose time never moves: `{"time": "<data_time>"}` every time."""
    body = json.dumps({"time": data_time.isoformat()}).encode()
    return Respond(body=body, name=name)


def advancing_json(
    start: datetime, step: timedelta, *, name: str = "advancing data"
) -> Respond:
    """JSON whose time moves on by `step` with every request the simulator
    has received."""

    def body(request: SourceRequest) -> bytes:
        moment = start + step * (request.number - 1)
        return json.dumps({"time": moment.isoformat()}).encode()

    return Respond(body=body, name=name)


def closed_port_url(path: str = "/") -> str:
    """A loopback URL on a port nobody listens on: connections are refused."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((_LOOPBACK, 0))
        port = probe.getsockname()[1]
    return f"http://{_LOOPBACK}:{port}{path}"


def _read_head(conn: socket.socket) -> tuple[str, str, dict[str, str]] | None:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk or len(data) > _MAX_HEAD:
            return None
        data += chunk
    lines = data.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3:
        return None
    method, path, _version = parts
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        key, value = name.strip().lower(), value.strip()
        headers[key] = f"{headers[key]}, {value}" if key in headers else value
    return method, path, headers


class SourceSimulator:
    """A simulated source on 127.0.0.1, playing one scenario at a time.

    `clock` gives the time each request arrived (`SourceRequest.arrived`);
    like `scenario`, it can be replaced between requests.
    """

    def __init__(
        self, scenario: Scenario | None = None, *, clock: Clock | None = None
    ) -> None:
        self.scenario: Scenario = scenario if scenario is not None else Respond()
        self.clock: Clock = clock if clock is not None else SystemClock()
        self.counter = RequestCounter()
        self._server: socket.socket | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._connections: set[socket.socket] = set()
        self._port = 0

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("the simulator is not running")
        return self._port

    def url(self, path: str = "/") -> str:
        return f"http://{_LOOPBACK}:{self.port}{path}"

    def start(self) -> Self:
        if self._server is not None:
            raise RuntimeError("the simulator is already running")
        self._stop.clear()
        server = socket.create_server((_LOOPBACK, 0))
        server.settimeout(0.05)
        self._server = server
        self._port = server.getsockname()[1]
        self._spawn(self._accept_loop, server)
        return self

    def stop(self) -> None:
        """Stop serving, release every held connection, wait for the threads."""
        server, self._server = self._server, None
        if server is None:
            return
        self._stop.set()
        server.close()
        with self._lock:
            connections = list(self._connections)
            threads = list(self._threads)
        for conn in connections:
            with contextlib.suppress(OSError):  # already closed by the client
                conn.shutdown(socket.SHUT_RDWR)
        for thread in threads:
            thread.join(timeout=_IO_TIMEOUT)
        with self._lock:
            self._threads.clear()

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    # --- serving ------------------------------------------------------------

    def _spawn(
        self, target: Callable[[socket.socket], None], arg: socket.socket
    ) -> None:
        thread = threading.Thread(target=target, args=(arg,), daemon=True)
        with self._lock:
            self._threads.append(thread)
        thread.start()

    def _accept_loop(self, server: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                return  # the listening socket was closed
            self._spawn(self._handle, conn)

    def _handle(self, conn: socket.socket) -> None:
        with self._lock:
            self._connections.add(conn)
        try:
            with conn:
                conn.settimeout(_IO_TIMEOUT)
                head = _read_head(conn)
                if head is None:
                    return
                method, path, headers = head
                scenario = self.scenario
                request = self.counter.record(
                    method, path, headers, scenario.name, arrived=self.clock.now()
                )
                scenario.serve(conn, request, self._stop)
        except OSError:
            pass  # the client went away: nothing to answer
        finally:
            with self._lock:
                self._connections.discard(conn)
