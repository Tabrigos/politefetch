# politefetch — specification

## Goal

A Python library that sits between an ingest service and the external
sources it polls (public APIs, periodically updated files, institutional
servers with no contract) and guarantees two things:

1. **the source is never hammered**, not even if the process dies midway,
   restarts in a loop or the caller's code has a bug;
2. **broken or stale data does not pass as good**: truncated, HTML instead of
   JSON, stuck for hours even though the request "succeeds".

Existing libraries (tenacity, stamina, pybreaker, aiolimiter, hishel…) start
from the idea that retrying is good and keep state in memory. This one starts
from the opposite idea, born from sources that put the caller's address in
their firewall: **every request is a cost, and the memory of what was
promised must outlive the process**.

## The rules the library makes automatic

1. **The promise is written before the call.** Before each request, the
   persistent state records "do not call this host again before T", with T
   computed as if the request failed. A good response shortens T; a crash
   midway leaves the cautious promise. It is the only order that holds up
   against a process that dies.
2. **A single attempt.** No automatic retries: a timeout does not distinguish
   "the request did not arrive" from "the source's firewall drops the
   packets". Whoever wants to try again calls again later, and the library
   decides whether it can.
3. **The pause after an error grows** (exponential with a cap, configurable);
   `Retry-After` is a minimum. **403, 429 and 451 freeze the host** until a
   person unblocks it (or until a configured expiry): they are not retried on
   their own.
4. **A daily cap per host**, in the persistent state, consumed *before* the
   request by a single point that everything passes through. It is not reset
   on restart. It is the safety net that does not depend on any other logic.
5. **Tests are an outbound path.** The library offers a socket barrier for
   pytest, with proof that a real call fails and a simulated one passes. Only
   loopback and Unix sockets pass, private networks are blocked too, and the
   proof uses a real public IP, not a TEST-NET documentation address.
6. **The worst case is counted.** A source simulator with scenarios (refuses,
   does not respond, 429, responds truncated, HTML, stuck data, process
   killed with the request already sent, restarts in a loop) and a counter
   that checks how many requests actually go out in each one.
7. **An open request is not invisible.** Every request has an `in-flight`
   state that is closed in a `finally`; those that no `finally` can close
   (killed process, cancellation) are closed by a **reconciliation** with a
   wide threshold, and they count as failed. No statistic excludes them.

## Content checks

- **Truncated**: body shorter than `Content-Length`, JSON that ends midway,
  empty body where data is needed → `SourceNotReady` (transient).
- **Unexpected content**: HTML (by `Content-Type`, or by the first tag of
  the body past white space, comments and an XML declaration) where JSON or
  text is expected, redirect to another host, a data time too far ahead of
  now → `UnexpectedContent`. A data time from the future is not kept: one
  alone would make every later read look stuck.
- **Freshness on the content**: the caller provides a function that extracts
  the data's time from the body; the library compares it with `max_age`. A
  `304` or a recent `Last-Modified` does not mean that the data is new.
- **Stuck source**: if the data's time does not advance for N consecutive
  reads, or for a length of time, the library reports it (`StuckSource`),
  even if every request succeeded.
- Old or stuck data comes from a source that answered well: it counts as an
  answer for the source (the minimum interval, no growing backoff, the
  validators kept), and the error carries what came back, for a caller that
  would rather use it with a warning.

## Traffic

- Conditional requests: ETag and Last-Modified saved per URL and sent back.
- Bytes per host and per day, both on the wire (compressed) and decoded,
  counted the same way whatever the client: the core decodes the body
  (gzip, deflate) and checks that a compressed stream reaches its end.
- Byte budget per host, consumed like the request cap. A body is read only
  up to what is left of it (and up to a limit of the caller's), then the
  request fails with the bytes it read.
- A deadline for each whole request, body included, shorter than the
  reconciliation: a source that drips its body cannot keep a request alive.

## Architecture

- **Core** (standard library only): `Gate` decides whether a request can go
  out, opens and closes the ledger rows, applies the policies; the content
  checks are pure functions.
- **Store**: an interface with three implementations: memory (tests), SQLite
  (default, the standard library's `sqlite3`), Postgres (`postgres` extra).
  The critical operations are atomic: two processes sharing the store do not
  exceed the cap together. The SQLite and Postgres stores record a schema
  version; a later version is a migration that only adds, applied in one
  transaction (SQLite when it opens, an existing Postgres schema only when
  opened with `migrate=True`), and a database newer than the code is
  refused.
- **Transport adapters**: `urllib` (standard library, in the core), `httpx`
  and `requests` (extras). An adapter sends one GET and hands back the body
  as transferred, piece by piece; reading, the deadline, the size limit and
  decoding happen in the core. The core imports no HTTP client: changing
  library touches only the adapter. A client that retries on its own is
  refused.
- **Test kit** (`politefetch.testing`): socket barrier, source simulator,
  worst-case counter.
- **Readable state**: for each host, next allowed request, reason for any
  block, today's counts and bytes, latest rows; serializable to JSON for a
  dashboard.

Synchronous and asynchronous with the same semantics. A cancelled
asynchronous request is closed in a thread of its own, which a second
cancellation does not stop; only a process that ends before that thread
leaves the row to the reconciliation, and the specification states this
instead of pretending. An answer that arrives after the reconciliation can
only make the host more careful.

```python
from politefetch import Gate, HostPolicy
from politefetch.store import SqliteStore
from politefetch.adapters.httpx import HttpxFetcher

gate = Gate(SqliteStore("ingest.db"), policies={
    "services.swpc.noaa.gov": HostPolicy(daily_cap=2000, min_interval="60s"),
    "celestrak.org": HostPolicy(daily_cap=40, min_interval="2h"),
})
fetch = HttpxFetcher(gate)
result = fetch.get(url, expect="json", data_time=lambda body: ..., max_age="3h")
```

This is the overall design; the exact names and signatures are in the API
reference of the skill (`src/politefetch/skill/politefetch/references/api.md`).

## What it does not do

- It is not a crawler and does not schedule: the caller decides when to try,
  the library says whether it can.
- It does not cache bodies (hishel exists for that): it only remembers
  validators, promises and counts.
- No authentication, no rotating proxies, no circumventing a source's
  limits: the purpose is the opposite.
- It does not tell a block from an outage on its own (that would need a check
  from several networks): it records the shape of the failure (connection
  timeout, reset, refusal, TLS, HTTP code) so that a person or an external
  tool can do it.

## Quality

- Python 3.12 or later; `uv` and `hatchling`; `mypy --strict`, `ruff`,
  `pytest`, CI on GitHub Actions.
- The invariants (the cap is not exceeded, a promise is not shortened without
  a good response, every row is closed) have property-based tests
  (`hypothesis`), with simulated crashes and restarts.
- The Postgres store is tested against a real Postgres (the cloud
  environment has PostgreSQL 16).
