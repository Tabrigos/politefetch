"""What every adapter shares: the fetch cycle, the result, the failure shapes.

An adapter only knows how to send one GET with its client (`Transport`) and
how to name that client's errors. Everything else happens here, the same way
for every client:

1. the gate admits the request or raises `Refused`: nothing is sent;
2. the request goes out once, with the conditional headers the gate
   remembers and a `User-Agent`; redirects are not followed, because each
   would be another request to the source;
3. no answer, or an HTTP error status, raises `FetchFailed`;
4. a 2xx body goes through the content checks of `politefetch.checks`
   (length, HTML, JSON, data time, freshness, stuck source) and any
   `ContentError` is raised; a 304 is checked against the data time last
   seen, because "not modified" does not mean "fresh";
5. the outcome is recorded with its bytes before anything is returned.
"""

import asyncio
import socket
import ssl
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from importlib import metadata
from types import TracebackType
from typing import Any, Literal, Protocol, Self
from urllib.parse import urljoin

from politefetch.checks import (
    Expect,
    check_advancing,
    check_body,
    check_fresh,
    check_length,
    check_redirect,
    read_data_time,
)
from politefetch.errors import (
    ContentError,
    FetchFailed,
    SourceNotReady,
    UnexpectedContent,
)
from politefetch.gate import Attempt, Gate
from politefetch.policy import parse_duration

__all__ = [
    "AsyncFetcher",
    "AsyncTransport",
    "FailureShape",
    "FetchResult",
    "Fetcher",
    "IncompleteBody",
    "RawResponse",
    "Transport",
    "TransportError",
    "classify",
]


class FailureShape(StrEnum):
    """How a request failed without a usable HTTP answer. Stored: do not
    rename."""

    CONNECT_TIMEOUT = "connect_timeout"
    READ_TIMEOUT = "read_timeout"
    REFUSED = "refused"
    RESET = "reset"
    TLS = "tls"
    DNS = "dns"
    CONNECTION = "connection"
    """Any other failure to connect or to read the answer."""
    HTTP = "http"
    """An HTTP error status (1xx, 4xx, 5xx)."""


Phase = Literal["connect", "read"]
Timeout = float | tuple[float, float]


def _chain(error: BaseException) -> Iterator[BaseException]:
    """The error and everything behind it: causes, contexts, `reason`
    attributes and exceptions passed as arguments."""
    seen: set[int] = set()
    queue: list[BaseException] = [error]
    while queue:
        current = queue.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        behind = [
            current.__cause__,
            current.__context__,
            getattr(current, "reason", None),
        ]
        behind += [arg for arg in current.args if isinstance(arg, BaseException)]
        queue += [b for b in behind if isinstance(b, BaseException)]


def classify(error: BaseException, *, phase: Phase) -> FailureShape:
    """Name a low-level failure by what is found behind it. A timeout counts
    as a connect timeout or a read timeout according to `phase`."""
    for found in _chain(error):
        if isinstance(found, ssl.SSLError | ssl.CertificateError):
            return FailureShape.TLS
        if isinstance(found, socket.gaierror):
            return FailureShape.DNS
        if isinstance(found, ConnectionRefusedError):
            return FailureShape.REFUSED
        if isinstance(
            found, ConnectionResetError | BrokenPipeError | ConnectionAbortedError
        ):
            return FailureShape.RESET
        if isinstance(found, TimeoutError):
            return (
                FailureShape.CONNECT_TIMEOUT
                if phase == "connect"
                else FailureShape.READ_TIMEOUT
            )
    return FailureShape.CONNECTION


# --- what a transport hands back ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RawResponse:
    """One HTTP answer as a transport received it. Header names are
    lowercase; `body` is decoded, `wire_bytes` counts the body as
    transferred."""

    status: int
    headers: Mapping[str, str]
    body: bytes
    wire_bytes: int


class TransportError(Exception):
    """No usable answer: raised by a transport, turned into `FetchFailed`."""

    def __init__(self, shape: FailureShape, *, wire_bytes: int | None = None) -> None:
        super().__init__(shape)
        self.shape = shape
        self.wire_bytes = wire_bytes


class IncompleteBody(Exception):
    """An answer started but its body broke off: turned into `SourceNotReady`."""

    def __init__(
        self, status: int, headers: Mapping[str, str], wire_bytes: int
    ) -> None:
        super().__init__(status, headers, wire_bytes)
        self.status = status
        self.headers = headers
        self.wire_bytes = wire_bytes


class Transport(Protocol):
    """Sends one GET, without following redirects or retrying."""

    def send(
        self, url: str, headers: Mapping[str, str], timeout: tuple[float, float]
    ) -> RawResponse: ...

    def close(self) -> None: ...


class AsyncTransport(Protocol):
    async def send(
        self, url: str, headers: Mapping[str, str], timeout: tuple[float, float]
    ) -> RawResponse: ...

    async def aclose(self) -> None: ...


# --- what a fetch hands back ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FetchResult:
    """A fetch that passed every check.

    `content` is the parsed JSON, the decoded text or the bytes, according to
    `expect`; `None` when the source answered 304 (`not_modified`). Then
    `data_time` is the latest data time seen for the URL, if any.
    """

    url: str
    status: int
    headers: Mapping[str, str]
    body: bytes
    content: Any
    not_modified: bool
    data_time: datetime | None
    wire_bytes: int
    decoded_bytes: int


def _user_agent() -> str:
    try:
        return f"politefetch/{metadata.version('politefetch')}"
    except metadata.PackageNotFoundError:  # pragma: no cover - running from a checkout
        return "politefetch"


USER_AGENT = _user_agent()


def _timeouts(timeout: Timeout) -> tuple[float, float]:
    connect, read = (timeout, timeout) if isinstance(timeout, int | float) else timeout
    for value in (connect, read):
        if not 0 < value < float("inf"):
            raise ValueError(
                f"timeouts must be positive and finite, got {timeout!r}: a request "
                "that never ends keeps the host blocked until reconciliation"
            )
    return float(connect), float(read)


@dataclass(frozen=True, slots=True)
class _Options:
    expect: Expect
    data_time: Callable[[Any], datetime] | None
    max_age: timedelta | None
    stuck_after: int | None
    allow_empty: bool

    @classmethod
    def build(
        cls,
        expect: Expect,
        data_time: Callable[[Any], datetime] | None,
        max_age: timedelta | str | None,
        stuck_after: int | None,
        allow_empty: bool,
    ) -> "_Options":
        if (max_age is not None or stuck_after is not None) and data_time is None:
            raise ValueError("max_age and stuck_after need a data_time extractor")
        if stuck_after is not None and stuck_after < 2:
            raise ValueError(f"stuck_after must be at least 2, got {stuck_after}")
        age = None if max_age is None else parse_duration(max_age)
        return cls(expect, data_time, age, stuck_after, allow_empty)


def _request_headers(
    attempt: Attempt, user_agent: str, headers: Mapping[str, str] | None
) -> dict[str, str]:
    return {"User-Agent": user_agent, **(headers or {}), **attempt.conditional_headers}


def _content_length(headers: Mapping[str, str]) -> int | None:
    value = headers.get("content-length", "").strip()
    return int(value) if value.isdigit() else None


def _judge_time(gate: Gate, url: str, data_time: datetime, options: _Options) -> None:
    record = gate.note_data_time(url, data_time)
    if options.max_age is not None:
        check_fresh(data_time, now=gate.clock.now(), max_age=options.max_age, url=url)
    if options.stuck_after is not None:
        check_advancing(record, stuck_after=options.stuck_after, url=url)


def _finish(
    gate: Gate, attempt: Attempt, url: str, raw: RawResponse, options: _Options
) -> FetchResult:
    """Check one answer, record its outcome, and return it or raise."""
    status, headers = raw.status, raw.headers
    wire, decoded = raw.wire_bytes, len(raw.body)

    def reject(error: ContentError) -> ContentError:
        attempt.failure(
            error.shape,
            status=status,
            headers=headers,
            wire_bytes=wire,
            decoded_bytes=decoded,
        )
        return error

    if status == 304:
        latest = None
        if options.data_time is not None:
            record = gate.store.data_time_record(url)
            if record is not None:
                latest = record.latest
                try:
                    _judge_time(gate, url, latest, options)
                except ContentError as error:
                    raise reject(error) from None
        attempt.response(304, headers=headers, wire_bytes=wire, decoded_bytes=decoded)
        return FetchResult(url, 304, headers, b"", None, True, latest, wire, decoded)
    if 300 <= status < 400:
        location = headers.get("location")
        try:
            if location:
                check_redirect(url, urljoin(url, location))
            raise UnexpectedContent(
                f"redirect to {location!r} not followed: ask for that URL", url=url
            )
        except UnexpectedContent as error:
            raise reject(error) from None
    if not 200 <= status < 300:
        attempt.response(
            status, headers=headers, wire_bytes=wire, decoded_bytes=decoded
        )
        raise FetchFailed(url, FailureShape.HTTP, status)
    try:
        if status != 204:
            check_length(
                content_length=_content_length(headers),
                received=raw.wire_bytes,
                url=url,
            )
        content = check_body(
            raw.body,
            expect=options.expect,
            content_type=headers.get("content-type"),
            allow_empty=options.allow_empty or status == 204,
            url=url,
        )
        data_time = None
        if options.data_time is not None:
            data_time = read_data_time(content, options.data_time, url=url)
            _judge_time(gate, url, data_time, options)
    except ContentError as error:
        raise reject(error) from None
    attempt.response(status, headers=headers, wire_bytes=wire, decoded_bytes=decoded)
    return FetchResult(
        url, status, headers, raw.body, content, False, data_time, wire, decoded
    )


def _incomplete(
    attempt: Attempt, url: str, incomplete: IncompleteBody
) -> SourceNotReady:
    error = SourceNotReady("body broke off before its end", url=url)
    attempt.failure(
        error.shape,
        status=incomplete.status,
        headers=incomplete.headers,
        wire_bytes=incomplete.wire_bytes,
    )
    return error


class Fetcher:
    """Fetch through the gate with a synchronous transport."""

    def __init__(
        self,
        gate: Gate,
        transport: Transport,
        *,
        timeout: Timeout = (10.0, 30.0),
        user_agent: str = USER_AGENT,
    ) -> None:
        """`timeout` is one number or (connect, read), in seconds; both must
        be finite."""
        self.gate = gate
        self.transport = transport
        self.timeout = _timeouts(timeout)
        self.user_agent = user_agent

    def get(
        self,
        url: str,
        *,
        expect: Expect = "json",
        data_time: Callable[[Any], datetime] | None = None,
        max_age: timedelta | str | None = None,
        stuck_after: int | None = None,
        headers: Mapping[str, str] | None = None,
        allow_empty: bool = False,
    ) -> FetchResult:
        """Fetch `url` once, if the gate allows it, and check what comes back.

        `data_time` reads the data time from the parsed content; `max_age`
        rejects data older than that; `stuck_after` rejects a data time that
        has not advanced for that many reads. Raises `Refused`, `FetchFailed`
        or a `ContentError`.
        """
        options = _Options.build(expect, data_time, max_age, stuck_after, allow_empty)
        with self.gate.attempt(url) as attempt:
            sent = _request_headers(attempt, self.user_agent, headers)
            try:
                raw = self.transport.send(url, sent, self.timeout)
            except TransportError as error:
                attempt.failure(error.shape, wire_bytes=error.wire_bytes)
                raise FetchFailed(url, error.shape) from error
            except IncompleteBody as incomplete:
                raise _incomplete(attempt, url, incomplete) from incomplete
            return _finish(self.gate, attempt, url, raw, options)

    def close(self) -> None:
        self.transport.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class AsyncFetcher:
    """Fetch through the gate with an asynchronous transport. Same meaning
    as `Fetcher`; store work runs in a worker thread."""

    def __init__(
        self,
        gate: Gate,
        transport: AsyncTransport,
        *,
        timeout: Timeout = (10.0, 30.0),
        user_agent: str = USER_AGENT,
    ) -> None:
        self.gate = gate
        self.transport = transport
        self.timeout = _timeouts(timeout)
        self.user_agent = user_agent

    async def get(
        self,
        url: str,
        *,
        expect: Expect = "json",
        data_time: Callable[[Any], datetime] | None = None,
        max_age: timedelta | str | None = None,
        stuck_after: int | None = None,
        headers: Mapping[str, str] | None = None,
        allow_empty: bool = False,
    ) -> FetchResult:
        options = _Options.build(expect, data_time, max_age, stuck_after, allow_empty)
        async with self.gate.attempt(url) as attempt:
            sent = _request_headers(attempt, self.user_agent, headers)
            try:
                raw = await self.transport.send(url, sent, self.timeout)
            except TransportError as error:
                attempt.failure(error.shape, wire_bytes=error.wire_bytes)
                raise FetchFailed(url, error.shape) from error
            except IncompleteBody as incomplete:
                raise _incomplete(attempt, url, incomplete) from incomplete
            return await asyncio.to_thread(
                _finish, self.gate, attempt, url, raw, options
            )

    async def aclose(self) -> None:
        await self.transport.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
