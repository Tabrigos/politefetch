# politefetch

A polite, crash-safe gate between an ingest service and the external sources
it polls. It keeps the source safe from you (one attempt per call, persistent
per-host promises and daily caps that survive restarts, freezing on 403/429/451)
and keeps you safe from the source (truncated bodies, HTML instead of JSON,
data that stopped advancing even though every request "succeeds").

**Status: in development, not released.** Everything in `SPEC.md` exists
except the readable per-host status; see `docs/TAPPE.md` for the plan.

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

## Usage

```python
from datetime import datetime

from politefetch import Gate, HostPolicy
from politefetch.adapters.httpx import HttpxFetcher
from politefetch.store import SqliteStore

gate = Gate(
    SqliteStore("ingest.db"),
    policies={
        "services.swpc.noaa.gov": HostPolicy(daily_cap=2000, min_interval="60s"),
        "celestrak.org": HostPolicy(daily_cap=40, min_interval="2h"),
    },
)
fetch = HttpxFetcher(gate)
result = fetch.get(
    url,
    expect="json",
    data_time=lambda data: datetime.fromisoformat(data["time"]),
    max_age="3h",
)
```

`fetch.get` sends the request once, if the gate allows it, with the
conditional headers of the URL's last answer, and returns a `FetchResult`
(`content` is the parsed JSON). Otherwise it raises:

- `Refused`: nothing was sent (promise, daily cap, byte budget, freeze, or a
  request already in flight); `retry_after` says when to ask again;
- `FetchFailed`: no usable answer (`shape`: `connect_timeout`,
  `read_timeout`, `refused`, `reset`, `tls`, `dns`, `connection`, or `http`
  with `status`);
- a `ContentError`: truncated, HTML, redirected, stale or stuck data.

Adapters: `politefetch.adapters.urllib.UrllibFetcher` (no extras),
`politefetch.adapters.httpx.HttpxFetcher` and `AsyncHttpxFetcher` (extra
`httpx`), `politefetch.adapters.requests.RequestsFetcher` (extra
`requests`). They never follow redirects and never retry.

## What is there today

- `politefetch.Gate` and `politefetch.HostPolicy`: the gate every request
  goes through, sync or async, with the client of your choice:

  ```python
  from politefetch import Gate, HostPolicy
  from politefetch.store import SqliteStore

  gate = Gate(
      SqliteStore("ingest.db"),
      {
          "celestrak.org": HostPolicy(
              daily_cap=40, min_interval="2h", byte_budget="50MB"
          )
      },
  )
  with gate.attempt(url) as attempt:  # raises Refused: nothing is sent
      response = client.get(url, headers=attempt.conditional_headers)
      attempt.response(
          response.status_code,
          headers=response.headers,  # Retry-After, ETag, Last-Modified
          wire_bytes=wire_size,  # the body as transferred
          decoded_bytes=len(response.content),
      )
  ```

  `attempt.conditional_headers` carries `If-None-Match` and
  `If-Modified-Since` from the URL's last answer. `gate.may_fetch(url)` says
  whether a request may start now and, if not, why and until when;
  `gate.unfreeze(host, by="you")` lifts a freeze and leaves a trace.
- `politefetch.checks`: pure functions that reject what should not pass for
  data. A body shorter than its Content-Length, an empty body or JSON that
  ends mid-way raise `SourceNotReady`; HTML where JSON or text was expected
  and a redirect to another host raise `UnexpectedContent`; data older than
  `max_age`, measured on a time read from the data itself, raises
  `StaleData`; a data time that stopped advancing for N reads raises
  `StuckSource` (`gate.note_data_time(url, data_time)` keeps the history).
- `politefetch.store`: `SqliteStore` (the default, safe to share between
  processes on one machine), `PostgresStore` (extra `postgres`, safe to
  share between machines; tables in their own schema) and `MemoryStore`
  (tests).
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

The Postgres tests need a server: `POLITEFETCH_TEST_POSTGRES` holds its
connection string, and each test works in a schema of its own. On a machine
with PostgreSQL 16 installed, `eval "$(scripts/postgres-for-tests.sh)"`
starts it and sets the variable; without it those tests are skipped, and CI
refuses to skip them.
