"""Adapter for httpx, behind the `httpx` extra: `HttpxFetcher` (sync) and
`AsyncHttpxFetcher` (async).

Pass your own `httpx.Client` / `httpx.AsyncClient` to share pools, proxies or
certificates; the fetcher then leaves it open. Redirects are never followed.
Wire bytes come from `Response.num_bytes_downloaded` (compressed, if the
answer was).
"""

from collections.abc import Mapping

import httpx

from politefetch.adapters import base
from politefetch.adapters.base import (
    USER_AGENT,
    AsyncFetcher,
    FailureShape,
    Fetcher,
    IncompleteBody,
    RawResponse,
    Timeout,
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


def _timeout(timeout: tuple[float, float]) -> httpx.Timeout:
    connect, read = timeout
    return httpx.Timeout(connect=connect, read=read, write=read, pool=connect)


def _headers(response: httpx.Response) -> dict[str, str]:
    return {name.lower(): value for name, value in response.headers.items()}


class HttpxTransport:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self._owned = client is None
        self._client = client if client is not None else httpx.Client()

    def send(
        self, url: str, headers: Mapping[str, str], timeout: tuple[float, float]
    ) -> RawResponse:
        try:
            with self._client.stream(
                "GET",
                url,
                headers=dict(headers),
                timeout=_timeout(timeout),
                follow_redirects=False,
            ) as response:
                status, found = response.status_code, _headers(response)
                try:
                    body = response.read()
                except httpx.RemoteProtocolError as broken:
                    raise IncompleteBody(
                        status, found, response.num_bytes_downloaded
                    ) from broken
                return RawResponse(status, found, body, response.num_bytes_downloaded)
        except httpx.TransportError as error:
            raise base.TransportError(failure_shape(error)) from error

    def close(self) -> None:
        if self._owned:
            self._client.close()


class AsyncHttpxTransport:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owned = client is None
        self._client = client if client is not None else httpx.AsyncClient()

    async def send(
        self, url: str, headers: Mapping[str, str], timeout: tuple[float, float]
    ) -> RawResponse:
        try:
            async with self._client.stream(
                "GET",
                url,
                headers=dict(headers),
                timeout=_timeout(timeout),
                follow_redirects=False,
            ) as response:
                status, found = response.status_code, _headers(response)
                try:
                    body = await response.aread()
                except httpx.RemoteProtocolError as broken:
                    raise IncompleteBody(
                        status, found, response.num_bytes_downloaded
                    ) from broken
                return RawResponse(status, found, body, response.num_bytes_downloaded)
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
        user_agent: str = USER_AGENT,
    ) -> None:
        super().__init__(
            gate, HttpxTransport(client), timeout=timeout, user_agent=user_agent
        )


class AsyncHttpxFetcher(AsyncFetcher):
    """`AsyncFetcher` over an asynchronous httpx client."""

    def __init__(
        self,
        gate: Gate,
        client: httpx.AsyncClient | None = None,
        *,
        timeout: Timeout = (10.0, 30.0),
        user_agent: str = USER_AGENT,
    ) -> None:
        super().__init__(
            gate, AsyncHttpxTransport(client), timeout=timeout, user_agent=user_agent
        )
