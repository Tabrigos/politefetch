"""Adapter for httpx, behind the `httpx` extra: `HttpxFetcher` (sync) and
`AsyncHttpxFetcher` (async).

Pass your own `httpx.Client` / `httpx.AsyncClient` to share pools, proxies or
certificates; the fetcher then leaves it open. A client whose transport
retries (`HTTPTransport(retries=...)`) is refused: each retry would be a
request the gate never admitted. Redirects are never followed. The body is
read raw (`iter_raw`): the core decodes it and counts the bytes.
"""

import contextlib
import time
from collections.abc import AsyncGenerator, AsyncIterator, Generator, Iterator, Mapping
from datetime import timedelta

import httpx

from politefetch.adapters import base
from politefetch.adapters.base import (
    DEFAULT_DEADLINE,
    USER_AGENT,
    AsyncFetcher,
    AsyncRawResponse,
    FailureShape,
    Fetcher,
    IncompleteBody,
    RawResponse,
    Timeout,
    Timer,
    classify,
)
from politefetch.gate import Gate

__all__ = [
    "AsyncHttpxFetcher",
    "AsyncHttpxTransport",
    "HttpxFetcher",
    "HttpxTransport",
    "failure_shape",
]


def failure_shape(error: BaseException) -> FailureShape:
    if isinstance(error, httpx.ConnectTimeout | httpx.PoolTimeout):
        return FailureShape.CONNECT_TIMEOUT
    if isinstance(error, httpx.ReadTimeout | httpx.WriteTimeout):
        return FailureShape.READ_TIMEOUT
    if isinstance(error, httpx.RemoteProtocolError):
        return FailureShape.RESET
    if isinstance(error, httpx.ConnectError):
        return classify(error, phase="connect")
    if isinstance(error, httpx.ReadError | httpx.WriteError):
        shape = classify(error, phase="read")
        return FailureShape.RESET if shape is FailureShape.CONNECTION else shape
    return classify(error, phase="read")


def _refuse_retries(client: httpx.Client | httpx.AsyncClient) -> None:
    """httpx keeps a transport's retries on its connection pool, which is not
    public: a test checks that these attributes still exist."""
    transports = [client._transport, *client._mounts.values()]
    for transport in transports:
        pool = getattr(transport, "_pool", None)
        retries = getattr(pool, "_retries", 0)
        if isinstance(retries, int) and retries > 0:
            raise ValueError(
                f"this httpx client retries ({retries} retries on a transport): "
                "each retry is a request the gate never admitted. Give the "
                "fetcher a client whose transports do not retry."
            )


def _timeout(timeout: tuple[float, float]) -> httpx.Timeout:
    connect, read = timeout
    return httpx.Timeout(connect=connect, read=read, write=read, pool=connect)


def _headers(response: httpx.Response) -> dict[str, str]:
    return {name.lower(): value for name, value in response.headers.items()}


def _chunks(response: httpx.Response) -> Iterator[bytes]:
    try:
        yield from response.iter_raw()
    except httpx.RemoteProtocolError as broken:
        raise IncompleteBody from broken
    except httpx.TransportError as error:
        raise base.TransportError(failure_shape(error)) from error


async def _achunks(response: httpx.Response) -> AsyncIterator[bytes]:
    try:
        async for chunk in response.aiter_raw():
            yield chunk
    except httpx.RemoteProtocolError as broken:
        raise IncompleteBody from broken
    except httpx.TransportError as error:
        raise base.TransportError(failure_shape(error)) from error


class HttpxTransport:
    def __init__(self, client: httpx.Client | None = None) -> None:
        if client is not None:
            _refuse_retries(client)
        self._owned = client is None
        self._client = client if client is not None else httpx.Client()

    @contextlib.contextmanager
    def send(
        self, url: str, headers: Mapping[str, str], timeout: tuple[float, float]
    ) -> Generator[RawResponse, None, None]:
        try:
            with self._client.stream(
                "GET",
                url,
                headers=dict(headers),
                timeout=_timeout(timeout),
                follow_redirects=False,
            ) as response:
                yield RawResponse(
                    response.status_code, _headers(response), _chunks(response)
                )
        except httpx.TransportError as error:
            raise base.TransportError(failure_shape(error)) from error

    def close(self) -> None:
        if self._owned:
            self._client.close()


class AsyncHttpxTransport:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        if client is not None:
            _refuse_retries(client)
        self._owned = client is None
        self._client = client if client is not None else httpx.AsyncClient()

    @contextlib.asynccontextmanager
    async def send(
        self, url: str, headers: Mapping[str, str], timeout: tuple[float, float]
    ) -> AsyncGenerator[AsyncRawResponse, None]:
        try:
            async with self._client.stream(
                "GET",
                url,
                headers=dict(headers),
                timeout=_timeout(timeout),
                follow_redirects=False,
            ) as response:
                yield AsyncRawResponse(
                    response.status_code, _headers(response), _achunks(response)
                )
        except httpx.TransportError as error:
            raise base.TransportError(failure_shape(error)) from error

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()


class HttpxFetcher(Fetcher):
    """`Fetcher` over a synchronous httpx client."""

    def __init__(
        self,
        gate: Gate,
        client: httpx.Client | None = None,
        *,
        timeout: Timeout = (10.0, 30.0),
        deadline: timedelta | str = DEFAULT_DEADLINE,
        user_agent: str = USER_AGENT,
        timer: Timer = time.monotonic,
    ) -> None:
        super().__init__(
            gate,
            HttpxTransport(client),
            timeout=timeout,
            deadline=deadline,
            user_agent=user_agent,
            timer=timer,
        )


class AsyncHttpxFetcher(AsyncFetcher):
    """`AsyncFetcher` over an asynchronous httpx client."""

    def __init__(
        self,
        gate: Gate,
        client: httpx.AsyncClient | None = None,
        *,
        timeout: Timeout = (10.0, 30.0),
        deadline: timedelta | str = DEFAULT_DEADLINE,
        user_agent: str = USER_AGENT,
        timer: Timer = time.monotonic,
    ) -> None:
        super().__init__(
            gate,
            AsyncHttpxTransport(client),
            timeout=timeout,
            deadline=deadline,
            user_agent=user_agent,
            timer=timer,
        )
