"""Adapter for requests, behind the `requests` extra (synchronous only).

Pass your own `requests.Session` to share adapters, proxies or certificates;
the fetcher then leaves it open. A session with an adapter that retries
(`HTTPAdapter(max_retries=...)`) is refused: each retry would be a request
the gate never admitted. Redirects are never followed. The body is read raw
from the underlying urllib3 response: the core decodes it and counts the
bytes.
"""

import contextlib
import http.client
import time
from collections.abc import Generator, Iterator, Mapping
from datetime import timedelta

import requests
import urllib3.exceptions
from urllib3.util.retry import Retry

from politefetch.adapters.base import (
    DEFAULT_DEADLINE,
    USER_AGENT,
    FailureShape,
    Fetcher,
    IncompleteBody,
    RawResponse,
    Timeout,
    Timer,
    TransportError,
    _chain,
    classify,
)
from politefetch.gate import Gate

__all__ = ["RequestsFetcher", "RequestsTransport", "failure_shape"]

CHUNK = 64 * 1024


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


def _may_retry(retry: Retry) -> bool:
    """Whether urllib3 would try again after one failure of some kind.

    It counts each kind against its own budget and against `total`; a count
    that falls below zero ends the retries, `None` sets no bound, and `False`
    turns that kind off.
    """
    if retry.total is False:
        return False
    for kind in (retry.connect, retry.read, retry.status, retry.other):
        if kind is False:
            continue
        left = [count - 1 for count in (retry.total, kind) if count is not None]
        if all(count >= 0 for count in left):
            return True
    return False


def _refuse_retries(session: requests.Session) -> None:
    for prefix, adapter in session.adapters.items():
        retry = getattr(adapter, "max_retries", None)
        if isinstance(retry, Retry) and _may_retry(retry):
            raise ValueError(
                f"the session's adapter for {prefix!r} retries ({retry!r}): each "
                "retry is a request the gate never admitted. Give the fetcher a "
                "session whose adapters do not retry (max_retries=0)."
            )


def _chunks(response: requests.Response) -> Iterator[bytes]:
    raw = response.raw
    try:
        while chunk := raw.read1(CHUNK, decode_content=False):
            yield chunk
    except urllib3.exceptions.ProtocolError as broken:
        if any(
            isinstance(found, http.client.IncompleteRead) for found in _chain(broken)
        ):
            raise IncompleteBody from broken
        raise TransportError(failure_shape(broken)) from broken
    except (urllib3.exceptions.HTTPError, OSError) as error:
        raise TransportError(failure_shape(error)) from error


class RequestsTransport:
    def __init__(self, session: requests.Session | None = None) -> None:
        if session is not None:
            _refuse_retries(session)
        self._owned = session is None
        self._session = session if session is not None else requests.Session()

    @contextlib.contextmanager
    def send(
        self, url: str, headers: Mapping[str, str], timeout: tuple[float, float]
    ) -> Generator[RawResponse, None, None]:
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
            found = {name.lower(): value for name, value in response.headers.items()}
            yield RawResponse(response.status_code, found, _chunks(response))

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
        deadline: timedelta | str = DEFAULT_DEADLINE,
        user_agent: str = USER_AGENT,
        timer: Timer = time.monotonic,
    ) -> None:
        super().__init__(
            gate,
            RequestsTransport(session),
            timeout=timeout,
            deadline=deadline,
            user_agent=user_agent,
            timer=timer,
        )
