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

### Fixed

- A Retry-After with non-ASCII digits (`"²"`) raised `ValueError`
  inside the attempt: the HTTP status was lost and a 403 or 429 did not
  freeze the host. Retry-After parsing never raises now.
- A store error while closing a row (SQLite locked past its busy timeout)
  replaced the caller's error and lost the outcome. Closing is now tried
  again, then raises `OutcomeNotSaved`.

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
