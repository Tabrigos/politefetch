# Changelog

All notable changes to politefetch. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## Unreleased

### Added

- `OutcomeNotSaved`: raised when the store cannot save how a request ended,
  even after `CLOSE_TRIES` tries. It carries the URL, the row, the failure
  shape and the HTTP status that would otherwise be lost.
- `politefetch.gate.normalize_host`.
- A deadline for each whole request, body included: `deadline=` on every
  fetcher (default five minutes, measured with `timer=`, by default
  `time.monotonic`). With the two timeouts it must end before the gate's
  `reconcile_after`. A good answer still arriving then fails with the new
  shape `deadline`.
- `max_bytes=` on `get`, and `Attempt.bytes_left`: reading stops at the
  tighter of `max_bytes` and what is left of the host's byte budget, and
  raises the new `BodyTooLarge` with the bytes it read. `max_bytes` also
  bounds the decoded body.
- `politefetch.testing.simulator.Drip`, a source that sends its body one
  byte at a time.
- `max_ahead=` on `get` (default 15 minutes) and `checks.check_not_ahead`: a
  data time further ahead of now is `UnexpectedContent` and is not kept.
- `stuck_after=` also takes a duration (`"6h"`): stuck when the data time
  has not moved for that long since its first read. `DataTimeRecord.since`
  and `StuckSource.since` say when that was.
- `StaleData.result` and `StuckSource.result`: the `FetchResult` that came
  back, for a caller that would rather use old data than none.
- `NoPolicy`, raised for a host without a policy (a `ValueError` too).
- `FetchFailed.note`: when the answer froze the host, the error says until
  when and how to lift it.
- `PostgresStore(clock=, max_clock_skew="1m")`: the store refuses to open
  when the machine's clock is that far from the server's.
- A CI job with the lowest versions the extras allow.
- README: several endpoints of one host, read one per run; the
  `gate.attempt` snippet runs, checked by a test.

### Changed

- An answer that arrives after the reconciliation closed its row (a source
  dripping its body past `reconcile_after`) is no longer ignored. It is
  written on the row, which stays `reconciled`, and it can only make the
  host more careful: a freeze, a later promise, one more 429.
- A cancelled async attempt now closes its row, with the outcome it had
  recorded or as `cancelled`. Before, the row stayed in flight until the
  reconciliation, and an outcome already recorded (a 403) was lost.
- `Attempt.response` counts a 3xx other than 304 as a failure.
- Host names are normalized (lowercase, no final dot, IDNA) in policies,
  URLs, `status` and `unfreeze`. Before, `example.org.` had a cap of its own.
- A Retry-After longer than any pause of the policy (`min_interval`,
  `backoff_max`) becomes a freeze until that time, after any answer. It is
  still honoured in full, but `status` shows it and `unfreeze` lifts it.
  After a 403, 429 or 451 it also stays a promise, as before.
- Decoding happens in the core for every client: the fetchers ask for
  `gzip, deflate` (urllib too), read the body raw and decode it with the
  standard library, so wire and decoded bytes count the same way in every
  adapter. A compressed stream that stops before its end is
  `SourceNotReady`; one that does not decode, or an encoding it cannot undo,
  is `UnexpectedContent`.
- The `Transport` protocol: `send` returns a context manager holding a
  `RawResponse(status, headers, chunks)` with the raw body in pieces;
  `TransportError` and `IncompleteBody` carry no byte counts, the core
  counts them.
- A client or session that retries on its own is refused with `ValueError`
  when the fetcher is built: each retry is a request the gate never
  admitted.
- Headers of the caller replace ours whatever their case.
- The source simulator joins a header sent twice into one value.
- The agent skill follows the common library-skill schema: `python -m
  politefetch.skill path | show | check | install --target DIR`, copies
  compared by content hash (line endings normalised) and marked with
  `.skill-libreria.json`; `install` writes only into
  `.claude/skills/politefetch` or `.agents/skills/politefetch`. `sync`, the
  `agent_skills` entry point and the git-ignored copies are gone: a project
  that used `sync` removes the hook and its ignored copy, then installs the
  skill with `install --target` and keeps it in git.
- `references/api.md` comes from the common generator: one section per
  module and field tables for the data classes.
- Stale or stuck data is recorded as an answer: the host waits for
  `min_interval`, failures do not grow and the validators are kept. Before,
  it counted as a failure of the source.
- HTML is told by the first tag past a BOM, white space, comments and an
  XML declaration, within the first kB: `<head>` and `<body>` count too.
- The `requests` extra requires `urllib3>=2.2`, the first with the `read1`
  the adapter uses.
- The `postgres` extra requires `psycopg` without `[binary]`: add
  `psycopg[binary]`, `psycopg[c]` or a system libpq to the application.
- `parse_duration` and `parse_size` raise `TypeError` for a value that is
  not text, a `timedelta` or an `int`, saying what to write.
- `PostgresStore` keeps nothing in the session: schema, time zone and lock
  timeout are set in every transaction, and statements are not prepared,
  so a pooler in transaction mode works. A connection found lost when a
  transaction starts is opened again.

### Fixed

- A Retry-After with non-ASCII digits (`"²"`) raised `ValueError`
  inside the attempt: the HTTP status was lost and a 403 or 429 did not
  freeze the host. Retry-After parsing never raises now.
- A store error while closing a row (SQLite locked past its busy timeout)
  replaced the caller's error and lost the outcome. Closing is now tried
  again, then raises `OutcomeNotSaved`.
- A gzip body cut short but framed correctly passed as good with
  `expect="text"` or `"bytes"`.
- With httpx, a lowercase `user-agent` of the caller was sent next to ours.
- A Content-Length with non-ASCII digits raised `ValueError`.
- A single data time from the future (2099) made every later read
  `StuckSource`, with a growing backoff.
- An HTML error page starting with a comment, `<head>` or `<body>` passed
  as text.
- `HostPolicy(min_interval=60)` failed with `AttributeError`.
- Behind pgbouncer in transaction mode, `PostgresStore` could lose its
  schema and its time zone between transactions.

- Windows: the pytest plugin failed to import (`socket.socket` has no
  `sendmsg` there), which broke pytest in every Windows project that
  installed politefetch.
- Windows: the network barrier let asyncio through, because the default
  event loop connects and sends from its proactor without calling
  `socket.socket`; the barrier now guards `IocpProactor.connect` and
  `sendto` too.
- Windows: `Child.join` waited for the whole timeout when the child was
  killed before it started running.

## 0.1.0 — 2026-10-18

The first version. Not published on PyPI yet.

### Added

- An agent skill for AI coding assistants, bundled with the package
  (`politefetch/skill/politefetch/`: `SKILL.md` and a generated
  `references/api.md`), declared in the `agent_skills` entry point group, and
  `python -m politefetch.skill install | check | show | path | sync`; `sync`
  keeps `.claude/skills/` of a project in step with the installed packages,
  from a `SessionStart` hook.
- `politefetch.__version__`.
- `Gate`: one attempt per call, with the cautious promise, the daily cap,
  the byte reservation and the log row written in one atomic store
  operation before the request; sync and async attempts; reconciliation of
  abandoned rows; `may_fetch`, `unfreeze` with a trace, `note_data_time`.
- `HostPolicy`: minimum interval, exponential backoff with a cap,
  `Retry-After` as a minimum, freezes on 403/451 and 429 (or, as an option,
  one honoured 429 with Retry-After before freezing), daily cap, byte budget.
- Readable state per host: `gate.status(host_or_url)`, `gate.statuses()`,
  `HostStatus.to_dict()` / `to_json()` and `politefetch.status.to_json`.
- Conditional requests: ETag and Last-Modified per URL, sent back as
  `If-None-Match` and `If-Modified-Since`; bytes per host and day, on the
  wire and decoded.
- `politefetch.checks`: truncated bodies, HTML where data was expected,
  redirects to another host, stale data measured on the data time, sources
  whose data time stopped advancing.
- Stores: `SqliteStore` (default), `PostgresStore` (extra `postgres`),
  `MemoryStore`; one contract suite and property tests with crashes and
  restarts for all three; `rows(latest=N)`.
- Adapters that never retry and never follow redirects: urllib (no extras),
  httpx sync and async (extra `httpx`), requests (extra `requests`); failure
  shapes and the `FetchFailed` error.
- Test kit (`politefetch.testing`): socket barrier and pytest plugin
  (`politefetch_barrier = true`), local source simulator with the bad days of
  a source, request counter, child processes behind the barrier.
- Errors: `PoliteFetchError`, `Refused` (reason and retry-after),
  `FetchFailed`, `ContentError` with `SourceNotReady`, `UnexpectedContent`,
  `StaleData`, `StuckSource`; all importable from `politefetch`.
