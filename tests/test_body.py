"""How the core reads a body: piece by piece, within a deadline and a size
limit, decoding it in one place for every client.

The transports here are scripted: they hand back the pieces a test chose,
so the deadline can be driven by a timer that moves on each reading.
"""

import asyncio
import contextlib
import gzip
import random
import zlib
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Generator,
    Iterable,
    Iterator,
    Mapping,
)
from datetime import UTC, datetime, timedelta

import pytest

from politefetch.adapters import FailureShape
from politefetch.adapters.base import (
    AsyncFetcher,
    AsyncRawResponse,
    Fetcher,
    IncompleteBody,
    RawResponse,
    TransportError,
)
from politefetch.clock import FakeClock
from politefetch.errors import (
    BodyTooLarge,
    FetchFailed,
    RefusalReason,
    Refused,
    SourceNotReady,
    UnexpectedContent,
)
from politefetch.gate import Gate
from politefetch.policy import HostPolicy
from politefetch.store import MemoryStore, Outcome

T = datetime(2026, 10, 25, 12, tzinfo=UTC)
URL = "https://example.org/data.json"
JSON = b'{"series": [' + b", ".join(b"%d" % n for n in range(400)) + b"]}"

Piece = bytes | BaseException


class Scripted:
    """A transport that answers with `status`, `headers` and the pieces of
    the body, in order; a piece that is an exception is raised instead."""

    def __init__(
        self,
        pieces: Iterable[Piece] = (b"{}",),
        *,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.pieces = list(pieces)
        self.status = status
        self.headers = {"content-type": "application/json", **(headers or {})}
        self.sent: list[Mapping[str, str]] = []
        self.taken = 0

    def _chunks(self) -> Iterator[bytes]:
        for piece in self.pieces:
            self.taken += 1
            if isinstance(piece, BaseException):
                raise piece
            yield piece

    @contextlib.contextmanager
    def send(
        self, url: str, headers: Mapping[str, str], timeout: tuple[float, float]
    ) -> Generator[RawResponse, None, None]:
        self.sent.append(dict(headers))
        yield RawResponse(self.status, self.headers, self._chunks())

    def close(self) -> None:
        pass


class AsyncScripted(Scripted):
    async def _achunks(self) -> AsyncIterator[bytes]:
        for piece in self._chunks():
            yield piece

    @contextlib.asynccontextmanager
    async def send(  # type: ignore[override]
        self, url: str, headers: Mapping[str, str], timeout: tuple[float, float]
    ) -> AsyncGenerator[AsyncRawResponse, None]:
        self.sent.append(dict(headers))
        yield AsyncRawResponse(self.status, self.headers, self._achunks())

    async def aclose(self) -> None:
        pass


class Ticking:
    """A timer that moves on by `step` seconds each time it is read."""

    def __init__(self, step: float) -> None:
        self.step = step
        self.now = 0.0

    def __call__(self) -> float:
        now = self.now
        self.now += self.step
        return now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T)


def make_gate(clock: FakeClock, **policy: object) -> Gate:
    options: dict[str, object] = {"daily_cap": 50, "min_interval": "10m"}
    options.update(policy)
    return Gate(MemoryStore(), {"example.org": HostPolicy(**options)}, clock=clock)  # type: ignore[arg-type]


@pytest.fixture
def gate(clock: FakeClock) -> Gate:
    return make_gate(clock)


def last_outcome(gate: Gate) -> Outcome:
    outcome = gate.store.rows()[-1].outcome
    assert outcome is not None
    return outcome


def split(data: bytes, size: int) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]


# --- pieces ---------------------------------------------------------------------------


def test_a_body_in_pieces_is_joined(gate: Gate) -> None:
    transport = Scripted(split(JSON, 7))
    result = Fetcher(gate, transport).get(URL)
    assert result.body == JSON
    assert (result.wire_bytes, result.decoded_bytes) == (len(JSON), len(JSON))
    assert transport.taken == len(split(JSON, 7))  # read to its end


def test_a_body_that_breaks_off_keeps_what_came(gate: Gate) -> None:
    transport = Scripted([b'{"series"', IncompleteBody()])
    with pytest.raises(SourceNotReady):
        Fetcher(gate, transport).get(URL)
    outcome = last_outcome(gate)
    assert (outcome.failure, outcome.status, outcome.wire_bytes) == (
        "not_ready",
        200,
        9,
    )


def test_a_connection_lost_in_the_body_keeps_what_came(gate: Gate) -> None:
    transport = Scripted(
        [b'{"series"', TransportError(FailureShape.READ_TIMEOUT)],
        status=429,
        headers={"retry-after": "120"},
    )
    with pytest.raises(FetchFailed) as failed:
        Fetcher(gate, transport).get(URL)
    assert failed.value.shape == FailureShape.READ_TIMEOUT
    outcome = last_outcome(gate)
    assert (outcome.failure, outcome.status, outcome.wire_bytes) == (
        "read_timeout",
        429,
        9,
    )
    # The 429 the source sent before the connection broke still freezes.
    with pytest.raises(Refused) as refused:
        Fetcher(gate, Scripted()).get(URL)
    assert refused.value.reason is RefusalReason.FROZEN


# --- the deadline ---------------------------------------------------------------------


def test_a_slow_body_is_cut_at_the_deadline(gate: Gate) -> None:
    transport = Scripted([b"x"] * 20)
    fetcher = Fetcher(gate, transport, deadline="5m", timer=Ticking(60))
    with pytest.raises(FetchFailed) as failed:
        fetcher.get(URL)
    assert (failed.value.shape, failed.value.status) == (FailureShape.DEADLINE, 200)
    assert transport.taken < 20
    outcome = last_outcome(gate)
    assert (outcome.ok, outcome.failure, outcome.status) == (False, "deadline", 200)
    assert outcome.wire_bytes == transport.taken


def test_a_body_within_the_deadline_passes(gate: Gate) -> None:
    transport = Scripted(split(JSON, 1000))
    fetcher = Fetcher(gate, transport, deadline="5m", timer=Ticking(60))
    assert fetcher.get(URL).body == JSON


def test_a_head_after_the_deadline_is_cut(gate: Gate) -> None:
    transport = Scripted([JSON])
    fetcher = Fetcher(gate, transport, deadline="5m", timer=Ticking(301))
    with pytest.raises(FetchFailed) as failed:
        fetcher.get(URL)
    assert failed.value.shape == FailureShape.DEADLINE
    assert transport.taken == 0


def test_a_slow_error_keeps_its_status(gate: Gate) -> None:
    transport = Scripted([b"x"] * 20, status=503)
    fetcher = Fetcher(gate, transport, deadline="5m", timer=Ticking(60))
    with pytest.raises(FetchFailed) as failed:
        fetcher.get(URL)
    assert (failed.value.shape, failed.value.status) == (FailureShape.HTTP, 503)
    assert transport.taken < 20
    assert last_outcome(gate).failure == "http"


def test_the_async_fetcher_cuts_at_the_deadline(gate: Gate) -> None:
    transport = AsyncScripted([b"x"] * 20)

    async def run() -> None:
        fetcher = AsyncFetcher(gate, transport, deadline="5m", timer=Ticking(60))
        await fetcher.get(URL)

    with pytest.raises(FetchFailed) as failed:
        asyncio.run(run())
    assert failed.value.shape == FailureShape.DEADLINE
    assert transport.taken < 20


@pytest.mark.parametrize(
    ("deadline", "timeout"),
    [("10m", 1.0), ("9m", (30.0, 30.0)), ("1h", 1.0)],
    ids=["equal", "with the timeouts", "longer"],
)
def test_the_deadline_ends_before_the_reconciliation(
    clock: FakeClock, deadline: str, timeout: float | tuple[float, float]
) -> None:
    gate = Gate(
        MemoryStore(),
        {"example.org": HostPolicy(daily_cap=5, min_interval="10m")},
        clock=clock,
        reconcile_after="10m",
    )
    with pytest.raises(ValueError, match="reconcile_after"):
        Fetcher(gate, Scripted(), deadline=deadline, timeout=timeout)


def test_a_deadline_just_inside_the_reconciliation(clock: FakeClock) -> None:
    gate = Gate(
        MemoryStore(),
        {"example.org": HostPolicy(daily_cap=5, min_interval="10m")},
        clock=clock,
        reconcile_after="10m",
    )
    Fetcher(gate, Scripted(), deadline="9m", timeout=(10.0, 29.0))


def test_the_deadline_must_be_positive(gate: Gate) -> None:
    with pytest.raises(ValueError, match="deadline"):
        Fetcher(gate, Scripted(), deadline=timedelta(0))


# --- the size limit -------------------------------------------------------------------


def test_max_bytes_stops_the_read(gate: Gate) -> None:
    transport = Scripted([b"x" * 100] * 10)
    with pytest.raises(BodyTooLarge, match="250"):
        Fetcher(gate, transport).get(URL, max_bytes=250)
    assert transport.taken == 3
    outcome = last_outcome(gate)
    assert (outcome.ok, outcome.failure, outcome.status) == (False, "too_large", 200)
    assert outcome.wire_bytes == 300


def test_a_body_exactly_at_the_limit_passes(gate: Gate) -> None:
    transport = Scripted(split(JSON, 100))
    assert Fetcher(gate, transport).get(URL, max_bytes=len(JSON)).body == JSON


def test_max_bytes_reads_sizes(gate: Gate) -> None:
    transport = Scripted([b"x" * 1000] * 3)
    with pytest.raises(BodyTooLarge):
        Fetcher(gate, transport).get(URL, max_bytes="2kB")


def test_the_byte_budget_left_limits_the_body(clock: FakeClock) -> None:
    gate = make_gate(clock, byte_budget="1kB")
    fetcher = Fetcher(gate, Scripted([b'{"a": "' + b"x" * 391 + b'"}']))
    assert fetcher.get(URL).wire_bytes == 400
    clock.advance(timedelta(minutes=10))
    transport = Scripted([b"x" * 100] * 10)
    with pytest.raises(BodyTooLarge, match="byte budget"):
        Fetcher(gate, transport).get(URL)
    assert last_outcome(gate).wire_bytes == 700  # 600 bytes were left
    clock.advance(timedelta(hours=1))
    with pytest.raises(Refused) as refused:
        fetcher.get(URL)
    assert refused.value.reason is RefusalReason.BYTE_BUDGET


def test_the_tighter_limit_wins(clock: FakeClock) -> None:
    gate = make_gate(clock, byte_budget="1kB")
    transport = Scripted([b"x" * 100] * 10)
    with pytest.raises(BodyTooLarge, match="max_bytes"):
        Fetcher(gate, transport).get(URL, max_bytes=150)
    assert transport.taken == 2


def test_an_error_body_over_the_limit_keeps_its_status(gate: Gate) -> None:
    transport = Scripted([b"x" * 100] * 10, status=503)
    with pytest.raises(FetchFailed) as failed:
        Fetcher(gate, transport).get(URL, max_bytes=250)
    assert (failed.value.shape, failed.value.status) == (FailureShape.HTTP, 503)
    assert transport.taken == 3


# --- decoding -------------------------------------------------------------------------


def zlib_deflate(data: bytes) -> bytes:
    return zlib.compress(data)


def raw_deflate(data: bytes) -> bytes:
    packer = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return packer.compress(data) + packer.flush()


@pytest.mark.parametrize(
    ("encoding", "encode"),
    [
        ("gzip", gzip.compress),
        ("x-gzip", gzip.compress),
        ("GZIP", gzip.compress),
        ("deflate", zlib_deflate),
        ("deflate", raw_deflate),
        ("identity", bytes),
    ],
    ids=["gzip", "x-gzip", "upper case", "zlib deflate", "raw deflate", "identity"],
)
def test_a_compressed_body_is_decoded(
    gate: Gate, encoding: str, encode: object
) -> None:
    packed = encode(JSON)  # type: ignore[operator]
    transport = Scripted(split(packed, 50), headers={"content-encoding": encoding})
    result = Fetcher(gate, transport).get(URL)
    assert result.content == {"series": list(range(400))}
    assert (result.wire_bytes, result.decoded_bytes) == (len(packed), len(JSON))
    outcome = last_outcome(gate)
    assert (outcome.wire_bytes, outcome.decoded_bytes) == (len(packed), len(JSON))


def test_gzip_in_several_members(gate: Gate) -> None:
    packed = gzip.compress(JSON[:100]) + gzip.compress(JSON[100:])
    transport = Scripted([packed], headers={"content-encoding": "gzip"})
    assert Fetcher(gate, transport).get(URL).body == JSON


@pytest.mark.parametrize("expect", ["json", "text", "bytes"])
def test_a_truncated_gzip_is_not_ready(gate: Gate, expect: str) -> None:
    text = "".join(f"2026-10-25T{h:02d}:00Z,{h * 1.5:.1f}\n" for h in range(24))
    packed = gzip.compress(text.encode())
    cut = packed[: len(packed) // 2]  # framed right: Content-Length matches
    transport = Scripted(
        [cut],
        headers={
            "content-encoding": "gzip",
            "content-length": str(len(cut)),
            "content-type": "text/csv",
        },
    )
    with pytest.raises(SourceNotReady, match="compressed"):
        Fetcher(gate, transport).get(URL, expect=expect)  # type: ignore[arg-type]
    assert last_outcome(gate).failure == "not_ready"


def test_a_broken_gzip_is_unexpected(gate: Gate) -> None:
    transport = Scripted([b"not gzip at all"], headers={"content-encoding": "gzip"})
    with pytest.raises(UnexpectedContent, match="gzip"):
        Fetcher(gate, transport).get(URL)


def test_an_encoding_it_cannot_decode_is_unexpected(gate: Gate) -> None:
    transport = Scripted([b"\x1b\x00"], headers={"content-encoding": "br"})
    with pytest.raises(UnexpectedContent, match="br"):
        Fetcher(gate, transport).get(URL)


def test_max_bytes_also_bounds_the_decoded_body(gate: Gate) -> None:
    bomb = gzip.compress(b"0" * 10_000_000)
    transport = Scripted([bomb], headers={"content-encoding": "gzip"})
    with pytest.raises(BodyTooLarge, match="decoded"):
        Fetcher(gate, transport).get(URL, expect="bytes", max_bytes="1MB")
    outcome = last_outcome(gate)
    assert (outcome.failure, outcome.wire_bytes) == ("too_large", len(bomb))


def test_decoding_stops_at_the_limit(gate: Gate) -> None:
    # Beyond max_bytes the stream is broken (its checksum is wrong): only a
    # decoder that stops at the limit never gets there.
    letters = bytes(random.Random(0).choices(b"acgt", k=2_000_000))
    packed = bytearray(gzip.compress(letters))  # about a quarter of the size
    packed[-8] ^= 0xFF
    transport = Scripted([bytes(packed)], headers={"content-encoding": "gzip"})
    with pytest.raises(BodyTooLarge, match="decoded"):
        Fetcher(gate, transport).get(URL, expect="bytes", max_bytes="1MB")


def test_an_error_body_that_does_not_decode_keeps_its_status(gate: Gate) -> None:
    transport = Scripted([b"garbage"], status=503, headers={"content-encoding": "gzip"})
    with pytest.raises(FetchFailed) as failed:
        Fetcher(gate, transport).get(URL)
    assert (failed.value.shape, failed.value.status) == (FailureShape.HTTP, 503)
    assert last_outcome(gate).failure == "http"


def test_an_empty_compressed_body(gate: Gate) -> None:
    transport = Scripted([], status=204, headers={"content-encoding": "gzip"})
    result = Fetcher(gate, transport).get(URL)
    assert (result.status, result.body) == (204, b"")


def test_the_async_fetcher_decodes_too(gate: Gate) -> None:
    packed = gzip.compress(JSON)
    transport = AsyncScripted(split(packed, 50), headers={"content-encoding": "gzip"})

    async def run() -> bytes:
        return (await AsyncFetcher(gate, transport).get(URL)).body

    assert asyncio.run(run()) == JSON


# --- headers --------------------------------------------------------------------------


def test_it_asks_for_what_it_can_decode(gate: Gate) -> None:
    transport = Scripted()
    Fetcher(gate, transport).get(URL)
    assert transport.sent[0]["Accept-Encoding"] == "gzip, deflate"


def test_a_header_of_the_caller_replaces_ours_whatever_its_case(gate: Gate) -> None:
    transport = Scripted()
    Fetcher(gate, transport).get(
        URL, headers={"user-agent": "mine/1.0", "ACCEPT-ENCODING": "identity"}
    )
    sent = {name.lower(): value for name, value in transport.sent[0].items()}
    assert len(sent) == len(transport.sent[0])  # no name sent twice
    assert sent["user-agent"] == "mine/1.0"
    assert sent["accept-encoding"] == "identity"


def test_a_content_length_with_other_digits_is_ignored(gate: Gate) -> None:
    transport = Scripted([b"{}"], headers={"content-length": "²"})
    assert Fetcher(gate, transport).get(URL).content == {}


def test_a_content_length_longer_than_the_body_is_not_ready(gate: Gate) -> None:
    transport = Scripted([b"{}"], headers={"content-length": "20"})
    with pytest.raises(SourceNotReady):
        Fetcher(gate, transport).get(URL)
