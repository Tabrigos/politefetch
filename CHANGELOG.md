# Changelog

All notable changes to politefetch. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## Unreleased

The first version, not published yet. It will become 0.1.0 with the agent
skill (stage T9 in `docs/TAPPE.md`).

### Added

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
