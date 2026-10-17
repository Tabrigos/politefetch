"""Transport adapters: they run the HTTP request the gate allowed and record
the shape of any failure (connect timeout, reset, refusal, TLS, HTTP status)
the same way whatever the client. The core never imports an HTTP client.

    from politefetch.adapters.urllib import UrllibFetcher      # no extras
    from politefetch.adapters.httpx import HttpxFetcher        # extra "httpx"
    from politefetch.adapters.httpx import AsyncHttpxFetcher
    from politefetch.adapters.requests import RequestsFetcher  # extra "requests"

    result = fetcher.get(url, expect="json", data_time=..., max_age="3h")
"""

from politefetch.adapters.base import AsyncFetcher, FailureShape, Fetcher, FetchResult

__all__ = ["AsyncFetcher", "FailureShape", "FetchResult", "Fetcher"]
