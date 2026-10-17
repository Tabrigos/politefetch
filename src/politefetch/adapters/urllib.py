"""Adapter for the standard-library `urllib`, available without extras.

urllib has one socket timeout for connecting and reading: this adapter uses
the read timeout for both. It does not ask for compressed answers (urllib
would not decode them), so wire and decoded bytes are the same. Proxies come
from the environment, as urllib does by default.
"""

import http.client
import urllib.error
import urllib.request
from collections.abc import Mapping
from email.message import Message
from typing import IO, Any

from politefetch.adapters.base import (
    USER_AGENT,
    FailureShape,
    Fetcher,
    IncompleteBody,
    RawResponse,
    Timeout,
    TransportError,
    classify,
)
from politefetch.gate import Gate

__all__ = ["UrllibFetcher", "UrllibTransport", "failure_shape"]


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


def _read(status: int, headers: dict[str, str], stream: IO[bytes]) -> RawResponse:
    try:
        body = stream.read()
    except http.client.IncompleteRead as incomplete:
        raise IncompleteBody(status, headers, len(incomplete.partial)) from incomplete
    except OSError as error:
        raise TransportError(failure_shape(error)) from error
    return RawResponse(status, headers, body, len(body))


class UrllibTransport:
    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirect)

    def send(
        self, url: str, headers: Mapping[str, str], timeout: tuple[float, float]
    ) -> RawResponse:
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        try:
            response = self._opener.open(request, timeout=timeout[1])
        except urllib.error.HTTPError as answer:
            with answer:
                return _read(answer.code, _headers(answer.headers), answer)
        except (OSError, http.client.HTTPException) as error:
            raise TransportError(
                failure_shape(error)
                if isinstance(error, OSError)
                else FailureShape.CONNECTION
            ) from error
        with response:
            return _read(response.status, _headers(response.headers), response)

    def close(self) -> None:
        pass


class UrllibFetcher(Fetcher):
    """`Fetcher` over urllib."""

    def __init__(
        self,
        gate: Gate,
        *,
        timeout: Timeout = (10.0, 30.0),
        user_agent: str = USER_AGENT,
    ) -> None:
        super().__init__(
            gate, UrllibTransport(), timeout=timeout, user_agent=user_agent
        )
