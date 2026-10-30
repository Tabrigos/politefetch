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
- The simulator scenario `HalfJson`: a complete answer, its Content-Length
  matching, whose JSON stops midway, as served by a source rewriting its
  file. The skill tells it from `Truncated` and says which error the caller
  sees for each (#20).
- `SourceRequest.arrived`: the time each request reached the simulator, read
  from its `clock` (`SourceSimulator(clock=...)`, or set on a running
  simulator; the system clock by default). The skill shows how to count the
  requests per simulated hour with a `FakeClock` (#21).
- `politefetch_private_networks` in the pytest configuration, empty by
  default, lists private networks that the barrier lets through, for
  services in containers next to the tests (#19). Only networks inside
  10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 and fc00::/7 are accepted;
  any other stops the session with a usage error, and a public address
  stays blocked. `NetworkBarrier(private_networks=...)` and
  `is_allowed_address` take the same list.
- **Limits over a window, for the host and for a key** (#28, part 2; covers
  #12 and #16). `HostPolicy(limits=["30/h"])` lets at most 30 requests to
  the host start in any span of an hour, so a burst of 30 is allowed and a
  31st waits until the first is an hour old; `key_limits=["1/30m"]` does the
  same for each key, given with `key=` to `gate.attempt`, `may_fetch`,
  `status` and the fetchers, or the URL when none is given. They are counted
  on the request log from the start of each request, whatever its outcome,
  and refuse with `RefusalReason.RATE_LIMIT` until the oldest request leaves
  the window. `min_interval` keeps its meaning, per host and from the end of
  the previous request. Processes sharing a store apply the lowest count
  asked for each period in the last day, as with the caps; `HostStatus`
  shows the limits in force, and the rows their `key`. The schema goes to
  version 3 (a `key` column on the requests, the URL for the rows written
  before); **an existing Postgres schema must be opened once with
  `migrate=True`** again. A store of your own implements `limit_starts` and
  takes `limits` and `key` in `open_request`.
- `wait_up_to=` on `gate.attempt` (sync and async) and on the fetchers' `get`
  (#14): a refusal for `min_interval` that ends within the bound, counted
  from the first refusal, is waited out and the gate asked again, so one
  logical call of a few GETs seconds apart no longer needs a wait of its own.
  Every other refusal is raised at once; nothing is open or sent while it
  waits. It sleeps through the gate's clock: `SystemClock` and `FakeClock`
  gain `sleep` and `sleep_async` (a `FakeClock` moves forward instead of
  waiting), and a clock of your own without them still serves for
  everything but `wait_up_to`, which then raises `TypeError`
  (`SleepingClock`).
- The last good answer of each host and URL, and the not-ready count of each
  URL (#24, #13). When a row closes with an answer the caller accepted, its
  time is written as the host's and the URL's last good answer, in the same
  transaction; an answer rejected as `"not_ready"` adds one to the URL's
  count, and only an accepted answer starts it again. `HostStatus.last_good`
  gives the host's, which outlives the log rows; `gate.status(url).url` is a
  `UrlStatus` with the URL's `last_good`, `not_ready`, `failures` and
  `paused_until`; both are in the JSON. The skill says what to alert on. The
  schema goes to version 5 (`last_good` on the hosts and URLs, filled
  from the log for the rows written before, and `not_ready` on the URLs);
  **an existing Postgres schema must be opened once with `migrate=True`**
  again. A store of your own keeps `HostState.last_good` and the new fields
  of `UrlRecord`, as `apply_close` and `apply_url` set them from
  `HostUpdate.accepted_at`.
- `tag=` on `gate.attempt` and on the fetchers' `get` (#17): the name of the
  logical call a request belongs to, up to 200 characters, kept in its row
  (`RequestRow.tag`, and `"tag"` in the JSON of `status`).
- `gate.prune(older_than)` and `Store.prune` (#17), for a daily job: in one
  transaction, the closed rows opened before `now - older_than`, then the
  URL records, data-time records and daily counts no row refers to any more,
  and it returns how many of each (`Pruned`). It never removes a row in
  flight or reconciled (a late close still finds it), a URL's pause still
  running, a host's state or an unfreeze trace; `older_than` under a day is
  `ValueError`, since the limits count the rows of the last day. A URL
  forgotten starts again without validators, last good answer or 404 count.
  The schema goes to version 6 (`tag` on the rows, and an index on their
  URL); **an existing Postgres schema must be opened once with
  `migrate=True`** again. A store of your own implements `prune` and takes
  `tag` in `open_request`.

### Changed

- **A 404 or a 410 pauses only its URL** (#28, part 2). The host takes it as
  an answer: `min_interval` and Retry-After hold, and its failures stay as
  they were. The URL gets a pause of its own, the policy's backoff after its
  consecutive 404/410, and is refused with `RefusalReason.URL_BACKOFF` until
  then; a good answer on the URL clears it. Transport errors, 5xx, 429 and
  the other 4xx pause the host as before. `UrlRecord` has `failures` and
  `not_before`, `HostUpdate` has `url_not_before`, and the state that
  `close_request` gives `update` has the URL's `url_failures`. The schema
  goes to version 4 (two columns on `urls`); **an existing Postgres schema
  must be opened once with `migrate=True`**.
- **The caller's verdict is recorded apart from the source's answer**
  (#28, part 1; covers #22). `attempt.accept()` says you took the body of a
  good answer: only then are its validators saved and the failures reset.
  `attempt.reject(reason)` says why not (`"not_ready"`, `"unusable"`, or
  `"not_sent"` for an attempt that sent nothing): still an answer,
  `min_interval` holds and the failures stay as they were; the row records
  `"rejected:<reason>"`. **With `gate.attempt` and a client of your own,
  call `accept()`**: a 2xx without a verdict saves no validators, so the next
  request asks for the whole body. The fetchers call it themselves.
- The fetchers record an empty body or half a JSON in a complete answer as
  `reject("not_ready")`, an answer, instead of a failure; a transfer cut
  short stays a failure. They take `parse=`, run inside the attempt (if it
  raises, the answer is rejected as `"unusable"` and the exception goes on),
  and `conditional=False`.

- **Processes sharing a store apply the lowest cap** (#31). The store
  remembers for a day the daily cap and the byte budget each host was
  asked for, a refused request's too, and every process applies the lowest
  when it is lower than its own: a rollout that lowers a cap holds at once,
  one that raises it holds a day after the last process with the old cap
  stopped. A process whose cap is above the one in force logs a WARNING once,
  and is refused only as the lower cap refuses. `HostStatus.daily_cap` and
  `byte_budget`, and `attempt.bytes_left`, give the caps in force;
  `HostState` has `lowest_daily_cap` and `lowest_byte_budget`. The store
  schema goes to version 2 (a `host_caps` table): SQLite migrates when it
  opens; **an existing Postgres schema must be opened once with
  `PostgresStore(..., migrate=True)`**, with the processes that use it
  stopped.
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
- The API reference gives the meaning of every field of the public
  classes (`HostStatus`, `RequestRow`, `Outcome`, the store records, the
  errors...), from an `Attributes:` section of their docstrings; a test
  fails if one is left empty (#23).
- The skill's example of `gate.attempt` names the client's exceptions with
  the adapter's `failure_shape` instead of by hand, and the skill says that
  `to_json` puts the fields of a row's outcome directly in the row, while
  `RequestRow` keeps them in `outcome` (#23).

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
