"""Adapter for requests, behind the `requests` extra (synchronous only).

Pass your own `requests.Session` to share adapters, proxies or certificates;
the fetcher then leaves it open. Redirects are never followed and requests
does not retry by default. Wire bytes come from the underlying urllib3
response (compressed, if the answer was).
"""

from collections.abc import Mapping

import requests
import urllib3.exceptions

from politefetch.adapters.base import (
    USER_AGENT,
    FailureShape,
    Fetcher,
    IncompleteBody,
    RawResponse,
    Timeout,
    TransportError,
    _chain,
    classify,
)
from politefetch.gate import Gate

__all__ = ["RequestsFetcher", "RequestsTransport", "failure_shape"]


def failure_shape(error: BaseException) -> FailureShape:
    if isinstance(error, requests.exceptions.ConnectTimeout):
        return FailureShape.CONNECT_TIMEOUT
    if isinstance(error, requests.exceptions.ReadTimeout):
        return FailureShape.READ_TIMEOUT
    if isinstance(error, requests.exceptions.SSLError):
        return FailureShape.TLS
    for found in _chain(error):
        if isinstance(found, urllib3.exceptions.NewConnectionError):
            # A subclass of ConnectTimeoutError for historical reasons: what
            # failed is behind it (refused, DNS, timeout...).
            continue
        if isinstance(found, urllib3.exceptions.ConnectTimeoutError):
            return FailureShape.CONNECT_TIMEOUT
        if isinstance(found, urllib3.exceptions.ReadTimeoutError):
            return FailureShape.READ_TIMEOUT
        if isinstance(found, urllib3.exceptions.NameResolutionError):
            return FailureShape.DNS
    return classify(error, phase="connect")


def _wire_bytes(response: requests.Response) -> int:
    raw = response.raw
    told = raw.tell() if raw is not None and hasattr(raw, "tell") else None
    return told if isinstance(told, int) else len(response.content)


class RequestsTransport:
    def __init__(self, session: requests.Session | None = None) -> None:
        self._owned = session is None
        self._session = session if session is not None else requests.Session()

    def send(
        self, url: str, headers: Mapping[str, str], timeout: tuple[float, float]
    ) -> RawResponse:
        try:
            response = self._session.get(
                url,
                headers=dict(headers),
                timeout=timeout,
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as error:
            raise TransportError(failure_shape(error)) from error
        with response:
            status = response.status_code
            found = {name.lower(): value for name, value in response.headers.items()}
            try:
                body = response.content
            except requests.exceptions.ChunkedEncodingError as broken:
                raise IncompleteBody(status, found, _wire_bytes(response)) from broken
            except requests.RequestException as error:
                raise TransportError(failure_shape(error)) from error
            return RawResponse(status, found, body, _wire_bytes(response))

    def close(self) -> None:
        if self._owned:
            self._session.close()


class RequestsFetcher(Fetcher):
    """`Fetcher` over a requests session."""

    def __init__(
        self,
        gate: Gate,
        session: requests.Session | None = None,
        *,
        timeout: Timeout = (10.0, 30.0),
        user_agent: str = USER_AGENT,
    ) -> None:
        super().__init__(
            gate, RequestsTransport(session), timeout=timeout, user_agent=user_agent
        )
