"""What every adapter shares: the fetch cycle, the result, the failure shapes.

An adapter only knows how to send one GET with its client (`Transport`),
hand back the body as transferred, piece by piece, and name that client's
errors. Everything else happens here, the same way for every client:

1. the gate admits the request or raises `Refused`: nothing is sent;
2. the request goes out once, with the conditional headers the gate
   remembers, a `User-Agent` and `Accept-Encoding: gzip, deflate`;
   redirects are not followed, because each would be another request to
   the source;
3. the body is read piece by piece, within a deadline for the whole request
   and a size limit (`max_bytes`, and what is left of the host's byte
   budget), then decoded here, so wire and decoded bytes count the same in
   every client and a compressed stream cut short is caught;
4. no answer, an HTTP error status, or an answer past its deadline raises
   `FetchFailed`;
5. a 2xx body goes through the content checks of `politefetch.checks`
   (length, HTML, JSON, data time, freshness, stuck source) and any
   `ContentError` is raised; a 304 is checked against the data time last
   seen, because "not modified" does not mean "fresh";
6. the outcome is recorded with its bytes before anything is returned.
   Stale or stuck data came from a source that answered well: it is
   recorded as an answer, and `StaleData` or `StuckSource` carries the
   result.

The deadline is checked when the head arrives and after each piece of the
body: a request lasts at most the deadline plus one connect and one read
timeout. A source that drips the head itself is bounded only by the
client's read timeout on each piece of it.
"""

import asyncio
import socket
import ssl
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from importlib import metadata
from types import TracebackType
from typing import Any, Literal, Protocol, Self
from urllib.parse import urljoin

from politefetch.adapters.decoding import ACCEPT_ENCODING, decode_body
from politefetch.checks import (
    Expect,
    check_advancing,
    check_body,
    check_fresh,
    check_length,
    check_not_ahead,
    check_redirect,
    read_data_time,
)
from politefetch.errors import (
    BodyTooLarge,
    ContentError,
    FetchFailed,
    SourceNotReady,
    StaleData,
    StuckSource,
    UnexpectedContent,
)
from politefetch.gate import Attempt, Gate, host_of
from politefetch.policy import parse_duration, parse_size

__all__ = [
    "DEFAULT_DEADLINE",
    "DEFAULT_MAX_AHEAD",
    "AsyncFetcher",
    "AsyncRawResponse",
    "AsyncTransport",
    "FailureShape",
    "FetchResult",
    "Fetcher",
    "IncompleteBody",
    "RawResponse",
    "Timer",
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
    DEADLINE = "deadline"
    """A good answer still arriving when the fetch's deadline was up."""


Phase = Literal["connect", "read"]
Timeout = float | tuple[float, float]
Timer = Callable[[], float]
"""Seconds on a clock that never jumps back, such as `time.monotonic`."""

DEFAULT_DEADLINE = timedelta(minutes=5)
"""How long a whole request may last by default, body included."""

DEFAULT_MAX_AHEAD = timedelta(minutes=15)
"""How far ahead of now a data time may be by default: room for a clock a
little ahead, not for a wrong field."""


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
    """One HTTP answer as a transport receives it.

    Attributes:
        status: The HTTP status.
        headers: The response headers, names in lowercase.
        chunks: The body as transferred, piece by piece as it arrives:
            without the transfer framing, but not decoded (a
            Content-Encoding stays, the core undoes it).
    """

    status: int
    headers: Mapping[str, str]
    chunks: Iterator[bytes]


@dataclass(frozen=True, slots=True)
class AsyncRawResponse:
    """`RawResponse` for an asynchronous transport.

    Attributes:
        status: The HTTP status.
        headers: The response headers, names in lowercase.
        chunks: The body as transferred, as `RawResponse.chunks`, read with
            `async for`.
    """

    status: int
    headers: Mapping[str, str]
    chunks: AsyncIterator[bytes]


class TransportError(Exception):
    """No usable answer: raised by a transport, while connecting or while
    reading the body, and turned into `FetchFailed`."""

    def __init__(self, shape: FailureShape) -> None:
        super().__init__(shape)
        self.shape = shape


class IncompleteBody(Exception):
    """The body broke off before its end: raised by a transport while the
    body is read, and turned into `SourceNotReady`."""


class Transport(Protocol):
    """Sends one GET, without following redirects, retrying or decoding.

    `send` returns a context manager: the connection stays open while the
    core reads `chunks`, and closes when the block ends, also when the core
    stops reading early. Failures raise `TransportError` or `IncompleteBody`.
    """

    def send(
        self, url: str, headers: Mapping[str, str], timeout: tuple[float, float]
    ) -> AbstractContextManager[RawResponse]: ...

    def close(self) -> None: ...


class AsyncTransport(Protocol):
    def send(
        self, url: str, headers: Mapping[str, str], timeout: tuple[float, float]
    ) -> AbstractAsyncContextManager[AsyncRawResponse]: ...

    async def aclose(self) -> None: ...


# --- what a fetch hands back ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FetchResult:
    """A fetch that passed every check.

    Attributes:
        url: The URL fetched.
        status: The HTTP status: a 2xx, or 304.
        headers: The response headers, names in lowercase.
        body: The body, decoded; empty after a 304.
        content: The parsed JSON, the decoded text or the bytes, according
            to `expect`; `None` when the source answered 304.
        not_modified: True when the source answered 304.
        data_time: The data time read from the content; after a 304, the
            latest data time seen for the URL. `None` without a `data_time`
            function, or when none was seen yet.
        wire_bytes: The body as transferred (compressed, if it was).
        decoded_bytes: The body after decoding.
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


def _deadline(
    deadline: timedelta | str, timeout: tuple[float, float], gate: Gate
) -> timedelta:
    value = parse_duration(deadline)
    if value <= timedelta(0):
        raise ValueError(f"deadline must be positive, got {value}")
    longest = value + timedelta(seconds=timeout[0] + timeout[1])
    gate.bound_requests(longest)
    if longest >= gate.reconcile_after:
        raise ValueError(
            f"deadline {value} plus one connect and one read timeout ({longest}) "
            f"must end before the gate's reconcile_after ({gate.reconcile_after}): "
            "a request still running then is closed as abandoned, and the host "
            "can be asked again while it runs"
        )
    return value


@dataclass(frozen=True, slots=True)
class _Options:
    expect: Expect
    data_time: Callable[[Any], datetime] | None
    max_age: timedelta | None
    max_ahead: timedelta
    stuck_after: int | timedelta | None
    allow_empty: bool
    max_bytes: int | None
    parse: Callable[[Any], Any] | None = None
    conditional: bool = True

    @classmethod
    def build(
        cls,
        expect: Expect,
        data_time: Callable[[Any], datetime] | None,
        max_age: timedelta | str | None,
        max_ahead: timedelta | str,
        stuck_after: int | timedelta | str | None,
        allow_empty: bool,
        max_bytes: int | str | None,
        parse: Callable[[Any], Any] | None = None,
        conditional: bool = True,
    ) -> "_Options":
        if (max_age is not None or stuck_after is not None) and data_time is None:
            raise ValueError("max_age and stuck_after need a data_time extractor")
        stuck: int | timedelta | None
        if isinstance(stuck_after, int):
            if stuck_after < 2:
                raise ValueError(f"stuck_after must be at least 2, got {stuck_after}")
            stuck = stuck_after
        elif stuck_after is not None:
            stuck = parse_duration(stuck_after)
            if stuck <= timedelta(0):
                raise ValueError(f"stuck_after must be positive, got {stuck}")
        else:
            stuck = None
        age = None if max_age is None else parse_duration(max_age)
        ahead = parse_duration(max_ahead)
        size = None if max_bytes is None else parse_size(max_bytes)
        return cls(
            expect, data_time, age, ahead, stuck, allow_empty, size, parse, conditional
        )


def _request_headers(
    attempt: Attempt,
    user_agent: str,
    headers: Mapping[str, str] | None,
    conditional: bool = True,
) -> dict[str, str]:
    """Ours, then the caller's, then the conditional ones (unless
    `conditional` is false): a name given again, in any case, replaces the
    earlier one."""
    ours = {"User-Agent": user_agent, "Accept-Encoding": ACCEPT_ENCODING}
    merged: dict[str, tuple[str, str]] = {}
    remembered = attempt.conditional_headers if conditional else {}
    for given in (ours, headers or {}, remembered):
        for name, value in given.items():
            merged[name.lower()] = (name, value)
    return dict(merged.values())


def _content_length(headers: Mapping[str, str]) -> int | None:
    value = headers.get("content-length", "").strip()
    return int(value) if value.isascii() and value.isdigit() else None


# --- reading the body -----------------------------------------------------------------


class _Reading:
    """One answer read piece by piece, until its end, the deadline or the
    size limit, whichever comes first."""

    def __init__(
        self, timer: Timer, deadline: timedelta, limit: tuple[int, str] | None
    ) -> None:
        self._timer = timer
        self._ends = timer() + deadline.total_seconds()
        self.limit = limit
        """The most wire bytes it may read, and why."""
        self.status: int | None = None
        self.headers: Mapping[str, str] = {}
        self.pieces: list[bytes] = []
        self.wire = 0
        self.cut: FailureShape | Literal["too_large"] | None = None

    def start(self, status: int, headers: Mapping[str, str]) -> bool:
        """Note the head; `False` if the body must not be read."""
        self.status, self.headers = status, headers
        return self._in_time()

    def take(self, chunk: bytes) -> bool:
        """Keep a piece of the body; `False` if reading must stop."""
        self.pieces.append(chunk)
        self.wire += len(chunk)
        if self.limit is not None and self.wire > self.limit[0]:
            self.cut = "too_large"
            return False
        return self._in_time()

    def _in_time(self) -> bool:
        if self._timer() > self._ends:
            self.cut = FailureShape.DEADLINE
            return False
        return True


def _limit(attempt: Attempt, max_bytes: int | None) -> tuple[int, str] | None:
    limits = []
    if max_bytes is not None:
        limits.append((max_bytes, f"max_bytes {max_bytes}"))
    if attempt.bytes_left is not None:
        left = attempt.bytes_left
        limits.append((left, f"the {left} bytes left in the host's byte budget"))
    return min(limits) if limits else None


def _read(
    transport: Transport,
    url: str,
    headers: Mapping[str, str],
    timeout: tuple[float, float],
    reading: _Reading,
) -> None:
    with transport.send(url, headers, timeout) as response:
        if reading.start(response.status, response.headers):
            for chunk in response.chunks:
                if not reading.take(chunk):
                    break


async def _aread(
    transport: AsyncTransport,
    url: str,
    headers: Mapping[str, str],
    timeout: tuple[float, float],
    reading: _Reading,
) -> None:
    async with transport.send(url, headers, timeout) as response:
        if reading.start(response.status, response.headers):
            async for chunk in response.chunks:
                if not reading.take(chunk):
                    break


def _lost(
    attempt: Attempt, url: str, reading: _Reading, error: TransportError
) -> FetchFailed:
    """No answer, or one that broke off: what had arrived still counts."""
    started = reading.status is not None
    attempt.failure(
        error.shape,
        status=reading.status,
        headers=reading.headers,
        wire_bytes=reading.wire if started else None,
    )
    return FetchFailed(url, error.shape, reading.status)


def _incomplete(attempt: Attempt, url: str, reading: _Reading) -> SourceNotReady:
    error = SourceNotReady("body broke off before its end", url=url)
    attempt.failure(
        error.shape,
        status=reading.status,
        headers=reading.headers,
        wire_bytes=reading.wire,
    )
    return error


# --- judging the answer ---------------------------------------------------------------


def _judge_time(
    gate: Gate, url: str, data_time: datetime, options: _Options
) -> StaleData | StuckSource | None:
    """Note the data time and tell whether the data is stale or stuck. Not
    raised here: the answer is recorded as good first."""
    record = gate.note_data_time(url, data_time)
    now = gate.clock.now()
    try:
        if options.max_age is not None:
            check_fresh(data_time, now=now, max_age=options.max_age, url=url)
        if options.stuck_after is not None:
            check_advancing(record, stuck_after=options.stuck_after, now=now, url=url)
    except (StaleData, StuckSource) as late:
        return late
    return None


def _decoded_size(
    raw: bytes, headers: Mapping[str, str], options: _Options
) -> int | None:
    """The decoded size of a body nobody will use, if it decodes."""
    try:
        encoding = headers.get("content-encoding")
        return len(decode_body(raw, encoding, max_bytes=options.max_bytes))
    except ContentError:
        return None


def _finish(
    gate: Gate, attempt: Attempt, url: str, reading: _Reading, options: _Options
) -> FetchResult:
    """Check one answer, record its outcome, and return it or raise."""
    if reading.status is None:
        raise RuntimeError("the transport ended without an answer")
    status, headers, wire = reading.status, reading.headers, reading.wire
    raw = b"".join(reading.pieces)

    def reject(error: ContentError, decoded: int | None) -> ContentError:
        attempt.failure(
            error.shape,
            status=status,
            headers=headers,
            wire_bytes=wire,
            decoded_bytes=decoded,
        )
        return error

    def answered(decoded: int | None) -> None:
        attempt.response(
            status, headers=headers, wire_bytes=wire, decoded_bytes=decoded
        )

    if status == 304:
        size = _decoded_size(raw, headers, options) or 0
        latest = None
        judged = None
        if options.data_time is not None:
            record = gate.store.data_time_record(url)
            if record is not None:
                latest = record.latest
                judged = _judge_time(gate, url, latest, options)
        attempt.response(304, headers=headers, wire_bytes=wire, decoded_bytes=size)
        attempt.accept()
        result = FetchResult(url, 304, headers, b"", None, True, latest, wire, size)
        return _deliver(result, judged)
    if not 200 <= status < 300:
        # The status is the failure: a body cut short changes nothing.
        decoded_size = _decoded_size(raw, headers, options)
        if 300 <= status < 400:
            location = headers.get("location")
            try:
                if location:
                    check_redirect(url, urljoin(url, location))
                raise UnexpectedContent(
                    f"redirect to {location!r} not followed: ask for that URL", url=url
                )
            except UnexpectedContent as error:
                raise reject(error, decoded_size) from None
        attempt.response(
            status, headers=headers, wire_bytes=wire, decoded_bytes=decoded_size
        )
        raise FetchFailed(url, FailureShape.HTTP, status)
    if reading.cut is FailureShape.DEADLINE:
        attempt.failure(
            FailureShape.DEADLINE, status=status, headers=headers, wire_bytes=wire
        )
        raise FetchFailed(url, FailureShape.DEADLINE, status)
    if reading.cut == "too_large" and reading.limit is not None:
        too_large = BodyTooLarge(f"body larger than {reading.limit[1]}", url=url)
        raise reject(too_large, None)
    decoded: int | None = None
    try:
        # A transfer cut short (shorter than announced, gzip cut) is a
        # failure: a firewall cuts connections the same way (D-057).
        if status != 204:
            check_length(
                content_length=_content_length(headers), received=wire, url=url
            )
        body = decode_body(
            raw, headers.get("content-encoding"), max_bytes=options.max_bytes, url=url
        )
        decoded = len(body)
    except ContentError as error:
        raise reject(error, decoded) from None
    try:
        content = check_body(
            body,
            expect=options.expect,
            content_type=headers.get("content-type"),
            allow_empty=options.allow_empty or status == 204,
            url=url,
        )
    except SourceNotReady:
        # Complete, but not ready (empty, half a JSON): an answer.
        answered(decoded)
        attempt.reject("not_ready")
        raise
    except ContentError as error:
        raise reject(error, decoded) from None
    value = content
    if options.parse is not None:
        try:
            value = options.parse(content)
        except Exception:
            answered(decoded)
            attempt.reject("unusable")
            raise
    data_time = None
    judged = None
    try:
        if options.data_time is not None:
            data_time = read_data_time(content, options.data_time, url=url)
            now = gate.clock.now()
            check_not_ahead(data_time, now=now, max_ahead=options.max_ahead, url=url)
            judged = _judge_time(gate, url, data_time, options)
    except ContentError as error:
        raise reject(error, decoded) from None
    answered(decoded)
    attempt.accept()
    result = FetchResult(
        url, status, headers, body, value, False, data_time, wire, len(body)
    )
    return _deliver(result, judged)


def _deliver(
    result: FetchResult, judged: StaleData | StuckSource | None
) -> FetchResult:
    """Return the result, or raise the stale or stuck error that carries it."""
    if judged is None:
        return result
    judged.result = result
    raise judged


def _note_freeze(gate: Gate, url: str, failed: FetchFailed) -> None:
    """If the answer left the host frozen, say so on the error, and how to
    lift it."""
    if failed.status is None:
        return
    host = host_of(url)
    now = gate.clock.now()
    freeze = gate.store.host_state(host, now).freeze
    if freeze is None or not freeze.active(now):
        return
    lift = f"gate.unfreeze({host!r}, by=...)"
    if freeze.until is None:
        note = f"the host is now frozen until a person lifts it: {lift}"
    else:
        until = freeze.until.isoformat()
        note = f"the host is now frozen until {until}; {lift} lifts it sooner"
    failed.note = note
    failed.args = (failed.url, failed.shape, failed.status, note)


# --- the fetchers ---------------------------------------------------------------------


class Fetcher:
    """Fetch through the gate with a synchronous transport."""

    def __init__(
        self,
        gate: Gate,
        transport: Transport,
        *,
        timeout: Timeout = (10.0, 30.0),
        deadline: timedelta | str = DEFAULT_DEADLINE,
        user_agent: str = USER_AGENT,
        timer: Timer = time.monotonic,
    ) -> None:
        """`timeout` is one number or (connect, read), in seconds; both must
        be finite. `deadline` bounds the whole request, body included; with
        the two timeouts it must end before `gate.reconcile_after`. `timer`
        measures it."""
        self.gate = gate
        self.transport = transport
        self.timeout = _timeouts(timeout)
        self.deadline = _deadline(deadline, self.timeout, gate)
        self.user_agent = user_agent
        self.timer = timer

    def get(
        self,
        url: str,
        *,
        expect: Expect = "json",
        data_time: Callable[[Any], datetime] | None = None,
        max_age: timedelta | str | None = None,
        max_ahead: timedelta | str = DEFAULT_MAX_AHEAD,
        stuck_after: int | timedelta | str | None = None,
        headers: Mapping[str, str] | None = None,
        allow_empty: bool = False,
        max_bytes: int | str | None = None,
        parse: Callable[[Any], Any] | None = None,
        conditional: bool = True,
        key: str | None = None,
        wait_up_to: timedelta | str | None = None,
        tag: str | None = None,
    ) -> FetchResult:
        """Fetch `url` once, if the gate allows it, and check what comes back.

        `data_time` reads the data time from the parsed content. A data time
        more than `max_ahead` after now is `UnexpectedContent`, and is not
        kept. `max_age` flags data older than that (`StaleData`);
        `stuck_after` flags a data time that has not advanced for that many
        reads, or for that long (`StuckSource`). Both are recorded as
        answers and carry the result. `max_bytes` bounds the body, as
        transferred and as decoded; reading also stops at what is left of the
        host's byte budget. `headers` are sent too, and replace ours of the
        same name.

        `parse` turns the checked content into what you keep, inside the
        attempt: if it raises, the answer is rejected as `"unusable"` (no
        validators are saved, the next request asks for the whole body) and
        its exception goes on; if it returns, `content` is what it returned
        and the answer is accepted. `conditional=False` leaves out the
        conditional headers, for a copy you no longer have. `key` names what
        the request reads for the policy's `key_limits` (the URL by
        default). `wait_up_to` waits out a refusal for `min_interval` that
        ends within it (`Gate.attempt`). `tag` names the logical call, kept
        in the log row (`Gate.attempt`). A body that is
        complete but not ready (empty, half a JSON) is `SourceNotReady`,
        recorded as an answer; a transfer cut short is recorded as a failure.

        Raises `Refused`, `FetchFailed` (whose message says if the answer
        froze the host, and how to lift it) or a `ContentError`.
        """
        options = _Options.build(
            expect,
            data_time,
            max_age,
            max_ahead,
            stuck_after,
            allow_empty,
            max_bytes,
            parse,
            conditional,
        )
        try:
            with self.gate.attempt(
                url, key=key, wait_up_to=wait_up_to, tag=tag
            ) as attempt:
                sent = _request_headers(
                    attempt, self.user_agent, headers, options.conditional
                )
                limit = _limit(attempt, options.max_bytes)
                reading = _Reading(self.timer, self.deadline, limit)
                try:
                    _read(self.transport, url, sent, self.timeout, reading)
                except TransportError as error:
                    raise _lost(attempt, url, reading, error) from error
                except IncompleteBody as incomplete:
                    raise _incomplete(attempt, url, reading) from incomplete
                return _finish(self.gate, attempt, url, reading, options)
        except FetchFailed as failed:
            _note_freeze(self.gate, url, failed)
            raise

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
        deadline: timedelta | str = DEFAULT_DEADLINE,
        user_agent: str = USER_AGENT,
        timer: Timer = time.monotonic,
    ) -> None:
        self.gate = gate
        self.transport = transport
        self.timeout = _timeouts(timeout)
        self.deadline = _deadline(deadline, self.timeout, gate)
        self.user_agent = user_agent
        self.timer = timer

    async def get(
        self,
        url: str,
        *,
        expect: Expect = "json",
        data_time: Callable[[Any], datetime] | None = None,
        max_age: timedelta | str | None = None,
        max_ahead: timedelta | str = DEFAULT_MAX_AHEAD,
        stuck_after: int | timedelta | str | None = None,
        headers: Mapping[str, str] | None = None,
        allow_empty: bool = False,
        max_bytes: int | str | None = None,
        parse: Callable[[Any], Any] | None = None,
        conditional: bool = True,
        key: str | None = None,
        wait_up_to: timedelta | str | None = None,
        tag: str | None = None,
    ) -> FetchResult:
        options = _Options.build(
            expect,
            data_time,
            max_age,
            max_ahead,
            stuck_after,
            allow_empty,
            max_bytes,
            parse,
            conditional,
        )
        try:
            async with self.gate.attempt(
                url, key=key, wait_up_to=wait_up_to, tag=tag
            ) as attempt:
                sent = _request_headers(
                    attempt, self.user_agent, headers, options.conditional
                )
                limit = _limit(attempt, options.max_bytes)
                reading = _Reading(self.timer, self.deadline, limit)
                try:
                    await _aread(self.transport, url, sent, self.timeout, reading)
                except TransportError as error:
                    raise _lost(attempt, url, reading, error) from error
                except IncompleteBody as incomplete:
                    raise _incomplete(attempt, url, reading) from incomplete
                return await asyncio.to_thread(
                    _finish, self.gate, attempt, url, reading, options
                )
        except FetchFailed as failed:
            await asyncio.to_thread(_note_freeze, self.gate, url, failed)
            raise

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
