"""Every client's errors map to the same failure shapes.

Built by hand: a real DNS failure cannot happen behind the barrier, and a
real connect timeout needs an unroutable address.
"""

import socket
import ssl
import urllib.error

import httpx
import pytest
import requests
import urllib3.exceptions

from politefetch.adapters import FailureShape
from politefetch.adapters import httpx as httpx_adapter
from politefetch.adapters import requests as requests_adapter
from politefetch.adapters import urllib as urllib_adapter
from politefetch.adapters.base import classify


def new_connection_error() -> BaseException:
    # Its constructor wants a live connection object; the class is what counts.
    error_type = urllib3.exceptions.NewConnectionError
    error = error_type.__new__(error_type)
    error.args = ("Failed to establish a new connection",)
    return error


def caused(error: BaseException, cause: BaseException) -> BaseException:
    error.__cause__ = cause
    return error


@pytest.mark.parametrize(
    ("error", "phase", "shape"),
    [
        (ConnectionRefusedError(), "connect", FailureShape.REFUSED),
        (socket.gaierror(-2, "Name or service not known"), "connect", FailureShape.DNS),
        (ssl.SSLCertVerificationError("bad certificate"), "connect", FailureShape.TLS),
        (TimeoutError(), "connect", FailureShape.CONNECT_TIMEOUT),
        (TimeoutError(), "read", FailureShape.READ_TIMEOUT),
        (ConnectionResetError(), "read", FailureShape.RESET),
        (BrokenPipeError(), "read", FailureShape.RESET),
        (OSError("something else"), "connect", FailureShape.CONNECTION),
        (
            caused(RuntimeError("wrapper"), ConnectionRefusedError()),
            "connect",
            FailureShape.REFUSED,
        ),
    ],
)
def test_classify_walks_the_cause_chain(
    error: BaseException, phase: str, shape: FailureShape
) -> None:
    assert classify(error, phase=phase) == shape  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("error", "shape"),
    [
        (urllib.error.URLError(ConnectionRefusedError()), FailureShape.REFUSED),
        (urllib.error.URLError(socket.gaierror(-2, "unknown")), FailureShape.DNS),
        (urllib.error.URLError(ssl.SSLError("wrong version")), FailureShape.TLS),
        (urllib.error.URLError(TimeoutError()), FailureShape.CONNECT_TIMEOUT),
        (TimeoutError(), FailureShape.READ_TIMEOUT),
        (ConnectionResetError(), FailureShape.RESET),
    ],
)
def test_urllib_shapes(error: BaseException, shape: FailureShape) -> None:
    assert urllib_adapter.failure_shape(error) == shape


@pytest.mark.parametrize(
    ("error", "shape"),
    [
        (httpx.ConnectTimeout("t"), FailureShape.CONNECT_TIMEOUT),
        (httpx.ReadTimeout("t"), FailureShape.READ_TIMEOUT),
        (httpx.PoolTimeout("t"), FailureShape.CONNECT_TIMEOUT),
        (httpx.RemoteProtocolError("Server disconnected"), FailureShape.RESET),
        (
            caused(httpx.ConnectError("x"), socket.gaierror(-2, "unknown")),
            FailureShape.DNS,
        ),
        (
            caused(httpx.ConnectError("x"), ssl.SSLError("wrong version")),
            FailureShape.TLS,
        ),
        (
            caused(httpx.ConnectError("x"), ConnectionRefusedError()),
            FailureShape.REFUSED,
        ),
        (httpx.ConnectError("x"), FailureShape.CONNECTION),
    ],
)
def test_httpx_shapes(error: BaseException, shape: FailureShape) -> None:
    assert httpx_adapter.failure_shape(error) == shape


@pytest.mark.parametrize(
    ("error", "shape"),
    [
        (requests.exceptions.ConnectTimeout(), FailureShape.CONNECT_TIMEOUT),
        (requests.exceptions.ReadTimeout(), FailureShape.READ_TIMEOUT),
        (requests.exceptions.SSLError(), FailureShape.TLS),
        (
            requests.exceptions.ConnectionError(socket.gaierror(-2, "unknown")),
            FailureShape.DNS,
        ),
        (
            requests.exceptions.ConnectionError(ConnectionRefusedError()),
            FailureShape.REFUSED,
        ),
        (requests.exceptions.ConnectionError("other"), FailureShape.CONNECTION),
        (
            requests.exceptions.ConnectionError(
                caused(new_connection_error(), ConnectionRefusedError())
            ),
            FailureShape.REFUSED,
        ),
    ],
)
def test_requests_shapes(error: BaseException, shape: FailureShape) -> None:
    assert requests_adapter.failure_shape(error) == shape


def test_shapes_are_stable_strings() -> None:
    assert {s.value for s in FailureShape} == {
        "connect_timeout",
        "read_timeout",
        "refused",
        "reset",
        "tls",
        "dns",
        "connection",
        "http",
    }
