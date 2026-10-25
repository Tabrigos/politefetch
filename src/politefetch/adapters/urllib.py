"""Adapter for the standard-library `urllib`, available without extras.

urllib has one socket timeout for connecting and reading: this adapter uses
the read timeout for both. urllib does not decode compressed answers; the
core does, as for every client. Proxies come from the environment, as urllib
does by default.
"""

import contextlib
import http.client
import time
import urllib.error
import urllib.request
from collections.abc import Generator, Iterator, Mapping
from datetime import timedelta
from email.message import Message
from typing import Any

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
    classify,
)
from politefetch.gate import Gate

__all__ = ["UrllibFetcher", "UrllibTransport", "failure_shape"]

CHUNK = 64 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # a 3xx then surfaces as an HTTPError we read as an answer


def failure_shape(error: BaseException) -> FailureShape:
    """urllib wraps errors met while connecting and sending in `URLError`;
    errors met while waiting for the answer come through bare."""
    if isinstance(error, urllib.error.URLError):
        return classify(
            error.reason if isinstance(error.reason, BaseException) else error,
            phase="connect",
        )
    return classify(error, phase="read")


def _headers(message: Message) -> dict[str, str]:
    return {name.lower(): value for name, value in message.items()}


def _chunks(stream: Any) -> Iterator[bytes]:
    """`read1` hands back what has arrived, without waiting for a full
    piece; a body shorter than its Content-Length simply ends, and the core
    compares the two."""
    try:
        while chunk := stream.read1(CHUNK):
            yield chunk
    except http.client.IncompleteRead as incomplete:
        raise IncompleteBody from incomplete
    except OSError as error:
        raise TransportError(failure_shape(error)) from error


class UrllibTransport:
    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirect)

    @contextlib.contextmanager
    def send(
        self, url: str, headers: Mapping[str, str], timeout: tuple[float, float]
    ) -> Generator[RawResponse, None, None]:
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        opened: Any
        try:
            opened = self._opener.open(request, timeout=timeout[1])
            status = opened.status
        except urllib.error.HTTPError as answer:
            opened, status = answer, answer.code
        except (OSError, http.client.HTTPException) as error:
            raise TransportError(
                failure_shape(error)
                if isinstance(error, OSError)
                else FailureShape.CONNECTION
            ) from error
        with opened:
            yield RawResponse(status, _headers(opened.headers), _chunks(opened))

    def close(self) -> None:
        pass


class UrllibFetcher(Fetcher):
    """`Fetcher` over urllib."""

    def __init__(
        self,
        gate: Gate,
        *,
        timeout: Timeout = (10.0, 30.0),
        deadline: timedelta | str = DEFAULT_DEADLINE,
        user_agent: str = USER_AGENT,
        timer: Timer = time.monotonic,
    ) -> None:
        super().__init__(
            gate,
            UrllibTransport(),
            timeout=timeout,
            deadline=deadline,
            user_agent=user_agent,
            timer=timer,
        )
