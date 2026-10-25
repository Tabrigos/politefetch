"""Run the same calls through every adapter, sync or async.

`getter(kind, gate)` returns a function `get(url, **options)` that fetches
through the chosen adapter; further keyword arguments of `getter` go to the
fetcher (`deadline`, `timer`). The async adapter runs each call in its own
event loop with a fresh client: the gate and its store carry the state
between calls, as they would between runs of a real service.
"""

import asyncio
from collections.abc import Callable
from typing import Any

from politefetch.adapters import Fetcher, FetchResult
from politefetch.adapters.httpx import AsyncHttpxFetcher, HttpxFetcher
from politefetch.adapters.requests import RequestsFetcher
from politefetch.adapters.urllib import UrllibFetcher
from politefetch.gate import Gate

KINDS = ["urllib", "httpx", "httpx-async", "requests"]
TIMEOUT = (2.0, 0.3)  # connect, read: short, the simulator is local

Get = Callable[..., FetchResult]


def getter(
    kind: str,
    gate: Gate,
    timeout: tuple[float, float] = TIMEOUT,
    **fetcher_options: Any,
) -> Get:
    if kind == "httpx-async":

        def get_async(url: str, **options: Any) -> FetchResult:
            async def run() -> FetchResult:
                async with AsyncHttpxFetcher(
                    gate, timeout=timeout, **fetcher_options
                ) as fetcher:
                    return await fetcher.get(url, **options)

            return asyncio.run(run())

        return get_async
    factories: dict[str, Callable[..., Fetcher]] = {
        "urllib": UrllibFetcher,
        "httpx": HttpxFetcher,
        "requests": RequestsFetcher,
    }
    fetcher = factories[kind](gate, timeout=timeout, **fetcher_options)

    def get(url: str, **options: Any) -> FetchResult:
        return fetcher.get(url, **options)

    return get
