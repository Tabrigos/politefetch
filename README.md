# politefetch

A polite, crash-safe gate between an ingest service and the external sources
it polls. It keeps the source safe from you (one attempt per call, persistent
per-host promises and daily caps that survive restarts, freezing on 403/429/451)
and keeps you safe from the source (truncated bodies, HTML instead of JSON,
data that stopped advancing even though every request "succeeds").

**Status: in development, not released.** The gate, the host policies, the
stores (memory and SQLite) and the test kit exist; content checks,
conditional requests and the HTTP adapters are coming. See `SPEC.md` for the
design and `docs/TAPPE.md` for the plan.

## Why another library

Retry, rate-limit and circuit-breaker libraries assume that trying again is
good and keep their state in memory. politefetch assumes the opposite, because
it was born from sources that put the caller's address in their firewall:
**every request has a cost, and the memory of what was promised must outlive
the process.**

- The promise "do not call this host before T" is written to persistent state
  *before* the request, with T computed as if the request failed. A good
  answer shortens it; a crash halfway leaves it cautious.
- One attempt per call, no automatic retries.
- A daily cap per host, consumed before the request, that does not reset when
  the process restarts.
- 403, 429 and 451 freeze the host until a person unlocks it.
- Freshness is measured on the data itself, not on `304` or `Last-Modified`.

## Planned usage

Not implemented yet; names may change.

```python
from politefetch import Gate, HostPolicy
from politefetch.store import SqliteStore
from politefetch.adapters.httpx import HttpxFetcher

gate = Gate(
    SqliteStore("ingest.db"),
    policies={
        "services.swpc.noaa.gov": HostPolicy(daily_cap=2000, min_interval="60s"),
        "celestrak.org": HostPolicy(daily_cap=40, min_interval="2h"),
    },
)
fetch = HttpxFetcher(gate)
result = fetch.get(url, expect="json", data_time=lambda body: ..., max_age="3h")
```

## What is there today

- `politefetch.Gate` and `politefetch.HostPolicy`: the gate every request
  goes through, sync or async, with the client of your choice:

  ```python
  from politefetch import Gate, HostPolicy
  from politefetch.store import SqliteStore

  gate = Gate(
      SqliteStore("ingest.db"),
      {"celestrak.org": HostPolicy(daily_cap=40, min_interval="2h")},
  )
  with gate.attempt(url) as attempt:  # raises Refused: nothing is sent
      response = client.get(url)
      attempt.response(
          response.status_code, retry_after=response.headers.get("Retry-After")
      )
  ```

  `gate.may_fetch(url)` says whether a request may start now and, if not,
  why and until when; `gate.unfreeze(host, by="you")` lifts a freeze and
  leaves a trace.
- `politefetch.store`: `SqliteStore` (the default, safe to share between
  processes) and `MemoryStore` (tests).
- `politefetch.errors`: `PoliteFetchError`, `Refused` (with a reason and a
  retry-after time), and the content errors `SourceNotReady`,
  `UnexpectedContent`, `StaleData`, `StuckSource`.
- `politefetch.clock`: the `Clock` protocol, `SystemClock` and `FakeClock`.
  Nothing in the library reads the wall clock directly.
- `politefetch.testing`, the test kit, with a pytest plugin that is
  installed with the package and does nothing until asked:
  - `politefetch_barrier = true` in your pytest configuration puts every test
    behind a socket barrier: any attempt to leave the machine raises
    `NetworkBlockedError` before a packet is sent, loopback and Unix sockets
    keep working, and a test whose code swallowed the error fails anyway;
  - the `source_simulator` fixture is a real HTTP server on 127.0.0.1 that
    plays the bad days of a source (never answers, resets, 429, truncated
    body, HTML, stuck data) and counts the requests that actually reached it;
  - `politefetch.testing.processes.spawn` runs a function in a child process
    behind the barrier, and can kill it mid-request.

  ```python
  from politefetch.testing.simulator import NoAnswer

  def test_my_ingest_gives_up_politely(source_simulator):
      source_simulator.scenario = NoAnswer()
      run_my_ingest_for_a_day(source_simulator.url("/data.json"))
      assert source_simulator.counter.count <= 11
  ```

## Development

Python 3.12 or later and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-extras
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

The core depends only on the standard library; HTTP clients and the Postgres
driver live in the `httpx`, `requests` and `postgres` extras. Tests never use
the network.
