"""Scripted transports: they hand back the pieces a test chose, so the core
can be driven without a socket, and a timer that moves on each reading."""

import contextlib
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Generator,
    Iterable,
    Iterator,
    Mapping,
)

from politefetch.adapters.base import AsyncRawResponse, RawResponse

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
