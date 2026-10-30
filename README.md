# politefetch

A polite, crash-safe gate between an ingest service and the external sources
it polls. It keeps the source safe from you (one attempt per call, persistent
per-host promises and daily caps that survive restarts, freezing on 403/429/451)
and keeps you safe from the source (truncated bodies, HTML instead of JSON,
data that stopped advancing even though every request "succeeds").

**Status: on PyPI since 0.1.0.** Everything in `SPEC.md` exists; see
`CHANGELOG.md`.

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
  the process restarts. The day is the UTC day: the cap starts again at
  midnight UTC, whatever the time zone of the machine.
- 403, 429 and 451 freeze the host until a person unlocks it.
- Freshness is measured on the data itself, not on `304` or `Last-Modified`.

## A complete example

A job that cron starts every few minutes to read the planetary K index from
NOAA SWPC. Every run is a new process; the promises, the daily count and the
log live in `ingest.db`, so a run started too early sends nothing.

```python
"""Read the planetary K index from NOAA SWPC, politely. Run it from cron."""

from datetime import UTC, datetime
from typing import Any

from politefetch import ContentError, FetchFailed, Gate, HostPolicy, Refused
from politefetch.adapters.urllib import UrllibFetcher
from politefetch.store import SqliteStore

URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
POLICIES = {
    "services.swpc.noaa.gov": HostPolicy(daily_cap=120, min_interval="10m"),
}


def newest_time(rows: list[dict[str, Any]]) -> datetime:
    """The data time: the `time_tag` of the last row, which SWPC gives in UTC."""
    return datetime.fromisoformat(rows[-1]["time_tag"]).replace(tzinfo=UTC)


def poll(gate: Gate, url: str = URL) -> list[dict[str, Any]] | None:
    """Read the source once if the gate allows it; `None` if there is
    nothing new to use."""
    fetch = UrllibFetcher(gate)
    try:
        result = fetch.get(
            url,
            expect="json",
            data_time=newest_time,
            max_age="6h",  # Kp comes every 3 hours: two missed means trouble
            stuck_after="6h",  # the data time has not moved for six hours
        )
    except Refused as refused:  # nothing was sent
        print(f"not now: {refused}")
        return None
    except FetchFailed as failed:  # sent, no usable answer
        print(f"no answer: {failed}")
        return None
    except ContentError as error:  # an answer, but not data to trust
        print(f"rejected: {error}")
        return None
    if result.not_modified:
        return None
    rows: list[dict[str, Any]] = result.content
    return rows


if __name__ == "__main__":
    with SqliteStore("ingest.db") as store:
        gate = Gate(store, POLICIES)
        rows = poll(gate)
        if rows is not None:
            print(f"Kp {rows[-1]['Kp']} at {rows[-1]['time_tag']}")
        print(gate.status(URL).to_json(indent=2))
```

`fetch.get` sends the request once, if the gate allows it, with the
conditional headers of the URL's last answer, and returns a `FetchResult`
(`content` is the parsed JSON). Otherwise it raises:

- `Refused`: nothing was sent (promise, daily cap, byte budget, freeze, or a
  request already in flight); `retry_after` says when to ask again, and is
  `None` while the host is frozen;
- `FetchFailed`: no usable answer (`shape`: `connect_timeout`,
  `read_timeout`, `refused`, `reset`, `tls`, `dns`, `connection`, `deadline`
  for a body still arriving when its time was up, or `http` with `status`);
- a `ContentError`: truncated (`SourceNotReady`), HTML or redirected
  (`UnexpectedContent`), too large (`BodyTooLarge`), stale (`StaleData`) or
  stuck (`StuckSource`) data. Stale and stuck data are still answers: the
  source is not slowed down for them, and the error's `result` holds what
  came back.

Adapters: `politefetch.adapters.urllib.UrllibFetcher` (no extras),
`politefetch.adapters.httpx.HttpxFetcher` and `AsyncHttpxFetcher` (extra
`httpx`), `politefetch.adapters.requests.RequestsFetcher` (extra
`requests`). They never follow redirects and never retry, and they refuse a
client or session of yours that retries on its own. Each request has a
deadline for its whole body (`deadline=`, default five minutes), and the
body is read up to `max_bytes` and to what is left of the host's byte
budget. Decoding (gzip, deflate) happens in politefetch, the same way for
every client.

The fetchers say who they are with `User-Agent: politefetch/<version>`.
Many sources ask for a way to reach whoever polls them: that is yours to
give, with `user_agent="my-ingest/1.0 (https://example.org/contact)"`.

With the `postgres` extra, add a libpq too: `psycopg[binary]` for most
projects (`pip install "politefetch[postgres]" "psycopg[binary]"`), or
`psycopg[c]` or the system's libpq. politefetch leaves the choice to the
application, as psycopg asks of libraries.

`PostgresStore` keeps its tables in a schema of its own, shared by every
machine that polls. Opened with a conninfo it creates the schema if missing,
which needs a role that may create schemas and tables. To run the
application with a role that may only read and write, create the schema, or
migrate it after an upgrade, in a deploy step, and open the store with
`create=False`, which runs no DDL:

```python
from politefetch.store.postgres import PostgresStore

PostgresStore.setup("host=db dbname=ingest user=owner")  # the deploy step
store = PostgresStore("host=db dbname=ingest user=ingest", create=False)
```

A schema with migrations pending is refused until `setup` has run, with the
processes that use it stopped. The store also takes a
`psycopg_pool.ConnectionPool` in place of the conninfo
(`PostgresStore(pool=pool, create=False)`): each transaction borrows a
connection, so the threads of a process write at once, and closing the store
leaves the pool open. The writers of one host wait for each other; the
writers of different hosts do not.

## Status per host

`gate.status(host_or_url)` says what the gate would decide now and why, with
today's counts and bytes, the last good answer and the latest log rows; given
a URL, `url` adds that URL's last good answer, its not-ready answers since
then and its pause after a 404; `gate.statuses()` does it
for every host with a policy. Both send nothing and serialise to JSON for a
dashboard:

```python
from politefetch.status import to_json

status = gate.status("services.swpc.noaa.gov")
if not status.may_fetch:
    print(status.reason, status.next_allowed)  # next_allowed is None if frozen
print(to_json(gate.statuses(), indent=2))
```

```json
{
  "host": "services.swpc.noaa.gov",
  "at": "2026-10-16T12:00:32+00:00",
  "may_fetch": false,
  "reason": "min_interval",
  "next_allowed": "2026-10-16T12:10:32+00:00",
  "frozen": null,
  "failures": 0,
  "throttled": 0,
  "in_flight": 0,
  "abandoned": 0,
  "last_good": "2026-10-16T12:00:32+00:00",
  "url": null,
  "today": {"day": "2026-10-16", "requests": 1, "daily_cap": 120,
            "wire_bytes": 2310, "decoded_bytes": 5120, "byte_budget": null},
  "limits": [],
  "recent": [{"id": 1, "url": "https://services.swpc.noaa.gov/...",
              "opened_at": "2026-10-16T12:00:30+00:00", "state": "closed",
              "closed_at": "2026-10-16T12:00:32+00:00", "ok": true,
              "status": 200, "failure": null, "wire_bytes": 2310,
              "decoded_bytes": 5120, "reserved_bytes": 0,
              "key": "https://services.swpc.noaa.gov/...", "tag": "kp"}]
}
```

A row keeps the `tag` given to `fetch.get(url, tag="kp")` or
`gate.attempt(url, tag=...)`: the name of the logical call it belongs to.

A frozen host stays frozen until a person lifts the freeze, which leaves a
trace: `gate.unfreeze("services.swpc.noaa.gov", by="alice", note="asked them")`.
The error of the request that froze it says so, and how to lift it.

The log grows with every request. Prune it from a daily job:
`gate.prune("30d")` removes the closed rows opened more than 30 days ago,
and the records of the URLs no row refers to any more (their validators,
their last good answer). At least a day: the limits count the rows of the
last day. The hosts' state stays. SQLite does not shrink the file on its
own: run `VACUUM` now and then if its size matters.

## Several endpoints of one host

The promise and the in-flight block are kept per host, not per URL: after
`/products/noaa-planetary-k-index.json`, the gate refuses
`/products/solar-wind/plasma-5-minute.json` of the same host until
`min_interval` has passed. That is on purpose: the source sees one client,
whatever it asks for, and the interval is the pause it gets between two
requests. A job that cron starts every `min_interval` can read one endpoint
per run, the one read least recently:

```python
from politefetch import Gate
from politefetch.adapters.urllib import UrllibFetcher
from politefetch.gate import host_of

ENDPOINTS = [
    "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json",
    "https://services.swpc.noaa.gov/products/solar-wind/plasma-5-minute.json",
]


def next_endpoint(gate: Gate, urls: list[str]) -> str:
    """The endpoint read least recently; one never read comes first."""
    last = {}
    for row in gate.status(host_of(urls[0]), recent=10 * len(urls)).recent:
        last.setdefault(row.url, row.opened_at)  # newest first
    return min(urls, key=lambda url: (url in last, last.get(url)))


def run(gate: Gate) -> None:
    UrllibFetcher(gate).get(next_endpoint(gate, ENDPOINTS))  # Refused if too soon
```

With two endpoints and `min_interval="10m"`, each is read every twenty
minutes. To read them more often, ask the source what it allows and lower
`min_interval`, not the other way round.

## The pieces

- `politefetch.Gate` and `politefetch.HostPolicy`: the gate every request
  goes through, sync or async, with the client of your choice when no
  adapter fits:

  ```python
  import httpx

  from politefetch import Gate, HostPolicy
  from politefetch.store import SqliteStore

  POLICIES = {
      "celestrak.org": HostPolicy(daily_cap=40, min_interval="2h", byte_budget="50MB")
  }


  def fetch(gate: Gate, client: httpx.Client, url: str) -> bytes | None:
      """One request with your own client; the gate decides and records."""
      with gate.attempt(url) as attempt:  # raises Refused: nothing is sent
          try:
              response = client.get(url, headers=attempt.conditional_headers)
          except httpx.TimeoutException:
              attempt.failure("read_timeout")
              return None
          attempt.response(
              response.status_code,
              headers=response.headers,  # Retry-After, ETag, Last-Modified
              wire_bytes=response.num_bytes_downloaded,  # as transferred
              decoded_bytes=len(response.content),
          )
          if response.status_code in (200, 304):
              attempt.accept()  # you keep it: now its validators are saved
          return response.content if response.status_code == 200 else None


  # In the job: fetch(Gate(SqliteStore("ingest.db"), POLICIES), httpx.Client(), url)
  ```

  The client must not retry on its own: each retry would be a request the
  gate never admitted.

  `attempt.conditional_headers` carries `If-None-Match` and
  `If-Modified-Since` from the URL's last accepted answer: `attempt.accept()`
  says you kept the body, `attempt.reject(reason)` that you did not
  (`"not_ready"`, `"unusable"`), and without either the next request asks
  for the whole body. `gate.may_fetch(url)` says whether a request may start
  now. A policy also takes the backoff
  (`backoff_base`, `backoff_factor`, `backoff_max`), a freeze with an expiry
  (`freeze_for`) and `respect_retry_after_429`, which honours the first 429
  that carries a Retry-After and freezes on the second.
- `politefetch.checks`: the pure functions behind the content errors, for
  bodies you fetch yourself (`check_length`, `check_body`, `check_redirect`,
  `read_data_time`, `check_fresh`, `check_advancing`).
- `politefetch.store`: `SqliteStore` (the default, safe to share between
  processes on one machine), `PostgresStore` (extra `postgres`, safe to
  share between machines; tables in their own schema) and `MemoryStore`
  (tests).
- `politefetch.clock`: the `Clock` protocol, `SystemClock` and `FakeClock`.
  Nothing in the library reads the wall clock directly.
- `politefetch.testing`, the test kit, with a pytest plugin that is
  installed with the package and does nothing until asked:
  - `politefetch_barrier = true` in your pytest configuration puts every test
    behind a socket barrier: any attempt to leave the machine raises
    `NetworkBlockedError` before a packet is sent, loopback and Unix sockets
    keep working, and a test whose code swallowed the error fails anyway;
    `politefetch_private_networks` lists the private networks it lets
    through as well (containers next to the tests), empty by default;
  - the `source_simulator` fixture is a real HTTP server on 127.0.0.1 that
    plays the bad days of a source (never answers, resets, 429, truncated
    body, HTML, stuck data) and counts the requests that actually reached it;
  - `politefetch.testing.processes.spawn` runs a function in a child process
    behind the barrier, and can kill it mid-request.

  With the plugin switched off (`-p no:politefetch`) its marker
  `politefetch_expects_blocked` is not registered, and `--strict-markers`
  rejects the tests that use it. On Windows a refused connection on
  loopback arrives as a connect timeout: Windows retries the SYN for about
  two seconds before it reports the refusal, so give such tests a connect
  timeout longer than that.

  ```python
  from politefetch.testing.simulator import NoAnswer

  def test_my_ingest_gives_up_politely(source_simulator):
      source_simulator.scenario = NoAnswer()
      run_my_ingest_for_a_day(source_simulator.url("/data.json"))
      assert source_simulator.counter.count <= 11
  ```

## For coding agents

The package ships a skill for AI coding assistants in
`src/politefetch/skill/politefetch/`, versioned with the package. A project
copies it into `.claude/skills/politefetch/`, keeps the copy in git and
checks it against the installed version by hash, so the agent reads the
guide of the version it actually uses:

```bash
python -m politefetch.skill install --target .claude/skills/politefetch
python -m politefetch.skill check     # exit 1 if the copy differs
```

`path` prints where the installed skill is and `show` prints its
`SKILL.md`. `install` writes only into `.claude/skills/politefetch` or
`.agents/skills/politefetch`, and never replaces a directory it did not
write.

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
the network; the example above is run by `tests/test_readme.py` against the
local simulator.

The Postgres tests need a server: `POLITEFETCH_TEST_POSTGRES` holds its
connection string, and each test works in a schema of its own. On a machine
with PostgreSQL 16 installed, `eval "$(scripts/postgres-for-tests.sh)"`
starts it and sets the variable; without it those tests are skipped, and CI
refuses to skip them.

`SPEC.md` and `AGENTS.md` are in English. `docs/DECISIONI.md`, the reasons
behind each design choice, is in Italian.

## License

MIT, see `LICENSE`.
