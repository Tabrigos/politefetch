# Changelog

All notable changes to politefetch. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## Unreleased

### Added

- `evals/trigger.json`: twenty requests that check the skill activates when
  a task needs the library, mostly without naming it, and stays quiet on
  near misses. On 0.1.1 with Sonnet 5.5 (Claude Code 2.1.312, three runs
  each, 2026-10-27): 10 of 10 positives and 10 of 10 near misses passed.
- The SQLite and Postgres stores record a schema version, and later versions
  come as migrations that only add, each in one transaction with the version
  update (#11). SQLite migrates when it opens; an existing Postgres schema
  with migrations pending is refused until it is opened once with
  `PostgresStore(..., migrate=True)`. A store newer than the code is refused.
  A store written by 0.1 opens with every promise, count and freeze, as two
  fixtures written by 0.1.1 show.
- A `politefetch` logger (#30): WARNING when a host freezes and when a row is
  closed as abandoned, ERROR when the store could not save an outcome, the
  one lost during a cancellation included. Business outcomes log nothing.
- `Store.hosts()`, and `gate.statuses()` with a `default` policy covers every
  host the store knows, not only those with a policy of their own (#30).

### Changed

- `status`, `statuses` and `may_fetch` write nothing (#29): a dashboard no
  longer competes for the write lock with the process that fetches, and can
  run with read-only access. A row in flight past `reconcile_after` shows in
  the new `HostStatus.abandoned`, already counted in `failures` and left out
  of `in_flight`; the next request to its host closes it, in the transaction
  that opens that request, so an attempt takes one transaction instead of
  two. `Store.open_request` takes `abandon_before` for that.
- The default `reconcile_after` follows the fetchers built on the gate (#32):
  twice their longest request (deadline plus one connect and one read
  timeout, 11 minutes 20 seconds with the defaults), or an hour while there
  are none. A process killed mid-request blocked its host for an hour; a
  value given by hand still wins. `Gate.bound_requests` is how the fetchers
  tell the gate.
- With `expect="json"`, a body that parses as JSON is data even when served
  as `text/html`; an HTML page is still recognised by its first tag, and a
  body served as HTML that does not parse is still `UnexpectedContent`.
  Text keeps the Content-Type check (#15).

### Fixed

- Several processes opening a new SQLite store at the same time: all but one
  could fail with `database is locked`, because switching a new file to WAL
  takes a lock that SQLite does not wait for. The switch is now tried again
  until the busy timeout ends. A failed open had sent nothing.

## 0.1.1 — 2026-10-26

### Fixed

- A task cancelled while `async with gate.attempt()` was opening its row
  left the row in flight, and the host blocked, until the reconciliation
  (an hour by default). The row now closes as `cancelled`, as it does when
  the cancellation arrives later.
- The skill's example of `gate.attempt` with httpx recorded the decoded size
  as `wire_bytes`; it now uses `response.num_bytes_downloaded`.
- The README no longer says the package is not on PyPI.
- `SPEC.md` says that the barrier blocks private networks too.
- `python -m politefetch.skill check` without `--target` looks for the copy
  from the current directory up to the root of the repository, so it works
  from a subfolder too.
- The API reference no longer names private bases or `Generic` in the
  "subclass of" lines.

### Added

- The skill says what the first users missed: `min_interval` counts from the
  end of a request, and how to pair it with a cron; there is no hourly cap;
  a long `Retry-After` freezes the host even on a 200; the conditional
  headers win over the caller's; `SourceNotReady` grows the pause of the
  host; a data time going back counts as not moving; the shape of the
  status JSON; the log is never pruned; the pytest plugin registers itself,
  the `politefetch_expects_blocked` marker, and what the barrier lets
  through.

## 0.1.0 — 2026-10-26

The first version.

### Added

- `Gate`: one attempt per call, with the cautious promise, the daily cap,
  the byte reservation and the log row written in one atomic store
  operation before the request; sync and async attempts, and a cancelled
  task closes its row; reconciliation of abandoned rows, where a late answer
  can still make the host more careful; `may_fetch`, `unfreeze` with a
  trace, `note_data_time`. Host names are normalized (lowercase, no final
  dot, IDNA), also by `politefetch.gate.normalize_host`.
- `HostPolicy`: minimum interval, exponential backoff with a cap,
  `Retry-After` as a minimum (one longer than any pause of the policy
  becomes a freeze that `status` shows), freezes on 403/451 and 429 (or, as
  an option, one honoured 429 with Retry-After before freezing), daily cap,
  byte budget. A host without a policy raises `NoPolicy`.
- Readable state per host: `gate.status(host_or_url)`, `gate.statuses()`,
  `HostStatus.to_dict()` / `to_json()` and `politefetch.status.to_json`.
- Conditional requests: ETag and Last-Modified per URL, sent back as
  `If-None-Match` and `If-Modified-Since`; bytes per host and day, on the
  wire and decoded.
- Fetchers for urllib (no extras), httpx sync and async (extra `httpx`) and
  requests (extra `requests`). They never retry and never follow redirects,
  and refuse with `ValueError` a client or session that retries on its own.
  They ask for `gzip, deflate` and decode in the core, the same way for every
  client. Each request has a deadline, body included (`deadline=`, five
  minutes by default), and reading stops at `max_bytes` or at what is left
  of the host's byte budget (`BodyTooLarge`). Other clients plug in through
  the `Transport` protocol.
- `politefetch.checks`: truncated bodies, HTML where data was expected
  (told by the first tag), redirects to another host, stale data measured on
  the data time (`max_age`), data times too far ahead (`max_ahead`), sources
  whose data time stopped advancing (`stuck_after`, a number of reads or a
  duration). Stale or stuck data is recorded as an answer, and the error
  carries it in `result`.
- Stores: `SqliteStore` (default), `PostgresStore` (extra `postgres`; it
  works behind a pooler in transaction mode and refuses to open when the
  machine's clock is far from the server's), `MemoryStore`; one contract
  suite and property tests with crashes and restarts for all three;
  `rows(latest=N)`.
- Errors, all importable from `politefetch`: `PoliteFetchError`, `Refused`
  (reason and retry-after), `FetchFailed` (shape, HTTP status, and a note
  when the answer froze the host), `OutcomeNotSaved`, `BodyTooLarge`,
  `NoPolicy`, `ContentError` with `SourceNotReady`, `UnexpectedContent`,
  `StaleData` and `StuckSource`.
- Test kit (`politefetch.testing`): socket barrier and pytest plugin
  (`politefetch_barrier = true`), on Windows too, asyncio included; a local
  source simulator with the bad days of a source; a request counter; child
  processes behind the barrier.
- An agent skill for AI coding assistants, bundled with the package
  (`politefetch/skill/politefetch/`: `SKILL.md` and a generated
  `references/api.md`), and `python -m politefetch.skill path | show |
  check | install --target DIR`. Copies are compared by content hash, with
  line endings normalised, and marked with `.skill-libreria.json`; `install`
  writes only into `.claude/skills/politefetch` or
  `.agents/skills/politefetch`.
- `politefetch.__version__`; `parse_duration` and `parse_size`.
- The `postgres` extra requires `psycopg` without `[binary]`: add
  `psycopg[binary]`, `psycopg[c]` or a system libpq to the application. CI
  also tests the lowest versions the extras allow.
