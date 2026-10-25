"""A client that retries on its own is refused when the fetcher is built.

Its retries would send the source requests the gate never admitted: one row
in the log, several requests at the source (rule 2 and the daily cap).
"""

from datetime import UTC, datetime

import httpx
import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from politefetch.adapters.httpx import AsyncHttpxFetcher, HttpxFetcher
from politefetch.adapters.requests import RequestsFetcher
from politefetch.clock import FakeClock
from politefetch.gate import Gate
from politefetch.policy import HostPolicy
from politefetch.store import MemoryStore
from politefetch.testing.simulator import Respond, SourceSimulator


@pytest.fixture
def gate() -> Gate:
    return Gate(
        MemoryStore(),
        {"127.0.0.1": HostPolicy(daily_cap=1, min_interval="1d")},
        clock=FakeClock(datetime(2026, 10, 25, 12, tzinfo=UTC)),
    )


def session_with(adapter: HTTPAdapter, prefix: str = "https://") -> requests.Session:
    session = requests.Session()
    session.mount(prefix, adapter)
    return session


# --- requests -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "retries",
    [
        1,
        Retry(total=5, status_forcelist=[503]),
        Retry(total=None, connect=2, read=0),
        Retry(total=None, connect=0, read=1, status=0, other=0),
        Retry(total=2, read=False),
        Retry(total=2, connect=False),
        Retry(total=None),
    ],
    ids=[
        "one",
        "on status",
        "on connect",
        "only one read",
        "total",
        "connect off, the rest on",
        "unbounded",
    ],
)
def test_a_session_that_retries_is_refused(gate: Gate, retries: object) -> None:
    session = session_with(HTTPAdapter(max_retries=retries))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="retries"):
        RequestsFetcher(gate, session)


@pytest.mark.parametrize(
    "retries",
    [
        0,
        Retry(0, read=False),
        Retry(total=False),
        Retry(total=3, connect=0, read=0, status=0, other=0),
    ],
    ids=["zero", "the requests default", "disabled", "every kind at zero"],
)
def test_a_session_that_does_not_retry_is_accepted(gate: Gate, retries: object) -> None:
    session = session_with(HTTPAdapter(max_retries=retries))  # type: ignore[arg-type]
    RequestsFetcher(gate, session).close()


def test_a_default_session_is_accepted(gate: Gate) -> None:
    RequestsFetcher(gate, requests.Session()).close()


def test_a_session_that_retries_never_reaches_the_source(gate: Gate) -> None:
    session = session_with(
        HTTPAdapter(max_retries=Retry(total=5, status_forcelist=[503])), "http://"
    )
    with SourceSimulator(Respond(503, b"busy", content_type="text/plain")) as source:
        with pytest.raises(ValueError, match="retries"):
            RequestsFetcher(gate, session).get(source.url())
        assert source.counter.count == 0


# --- httpx ----------------------------------------------------------------------------


def test_an_httpx_client_that_retries_is_refused(gate: Gate) -> None:
    client = httpx.Client(transport=httpx.HTTPTransport(retries=2))
    with pytest.raises(ValueError, match="retries"):
        HttpxFetcher(gate, client)
    client.close()


def test_an_httpx_mount_that_retries_is_refused(gate: Gate) -> None:
    client = httpx.Client(
        mounts={"https://example.org": httpx.HTTPTransport(retries=1)}
    )
    with pytest.raises(ValueError, match="retries"):
        HttpxFetcher(gate, client)
    client.close()


def test_an_async_httpx_client_that_retries_is_refused(gate: Gate) -> None:
    client = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(retries=2))
    with pytest.raises(ValueError, match="retries"):
        AsyncHttpxFetcher(gate, client)


def test_httpx_clients_without_retries_are_accepted(gate: Gate) -> None:
    HttpxFetcher(gate, httpx.Client()).close()
    HttpxFetcher(gate, httpx.Client(transport=httpx.HTTPTransport())).close()
    AsyncHttpxFetcher(gate, httpx.AsyncClient())


def test_the_httpx_internals_it_reads_still_exist() -> None:
    """The retries of an httpx client are not public: if these attributes go
    away, the check would silently pass every client."""
    transport = httpx.HTTPTransport(retries=3)
    client = httpx.Client(transport=transport, mounts={"http://x": transport})
    assert client._transport is transport
    assert list(client._mounts.values()) == [transport]
    assert transport._pool._retries == 3
    client.close()
    async_transport = httpx.AsyncHTTPTransport(retries=4)
    assert async_transport._pool._retries == 4
