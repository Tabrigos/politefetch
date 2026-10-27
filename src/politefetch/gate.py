"""The gate: decides whether a request may start and keeps the request log.

    gate = Gate(store, {"example.org": HostPolicy(daily_cap=40, min_interval="2h")})
    with gate.attempt(url) as attempt:      # raises Refused: nothing is sent
        response = client.get(url)
        attempt.response(response.status_code,
                         retry_after=response.headers.get("Retry-After"))

Entering `attempt` writes the cautious promise, consumes the daily cap,
reserves bytes against the byte budget and opens the log row in one atomic
store operation; leaving it closes the row
with the outcome the caller recorded, in a `finally`. An attempt left without
an outcome counts as failed, and so does one ended by an exception, which is
re-raised. The gate never retries (rule 2).

`async with gate.attempt(url)` has the same meaning; the store is called in a
worker thread. A cancelled attempt is closed too, with the outcome already
recorded or as `cancelled`: the close runs in its own thread, which a second
cancellation does not stop. Only if the process ends first is the row left
to the reconciliation (SPEC.md, "Architecture").

An answer that arrives after the reconciliation has closed its row (a source
that drips its body for longer than `reconcile_after`) still counts, but only
to make the host more careful: a freeze, a longer pause, never a shorter one.

If the store itself fails (for SQLite, a lock that stays taken past the busy
timeout), its exception propagates from entering the attempt and nothing is
sent. Closing is tried again `CLOSE_TRIES` times; if every try fails, the
attempt raises `OutcomeNotSaved`, which carries what the source answered.
"""

import asyncio
import contextlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import Message
from types import TracebackType
from typing import Self
from urllib.parse import urlsplit

from politefetch.clock import Clock, SystemClock
from politefetch.errors import NoPolicy, OutcomeNotSaved, Refused
from politefetch.policy import HostPolicy, parse_duration, parse_retry_after
from politefetch.status import HostStatus
from politefetch.store.base import (
    DataTimeRecord,
    HostState,
    HostUpdate,
    Outcome,
    RequestRow,
    Store,
    Unfreeze,
    Validators,
    check_admission,
)

__all__ = ["Attempt", "Gate", "Verdict", "normalize_host"]

UNRECORDED = "unrecorded"
"""Failure shape of an attempt that ended without an outcome."""

CANCELLED = "cancelled"
"""Failure shape of an async attempt cancelled before it had an outcome."""

CLOSE_TRIES = 3
"""How many times closing a row is tried before `OutcomeNotSaved`."""

Headers = Mapping[str, str] | Message
"""Response headers: any mapping (httpx, requests) or an `email.message.Message`
(urllib, http.client)."""


def _lowercase(headers: Headers | None) -> dict[str, str]:
    if headers is None:
        return {}
    return {str(name).lower(): str(value) for name, value in headers.items()}


@dataclass(frozen=True, slots=True)
class Verdict:
    """The answer of `Gate.may_fetch`: true if a request may start now.

    Attributes:
        refusal: Why a request may not start now, and until when: the
            `Refused` the gate's decision would raise. `None` when it may.
    """

    refusal: Refused | None = None

    def __bool__(self) -> bool:
        return self.refusal is None


def normalize_host(host: str) -> str:
    """The one spelling of a host name that caps and promises are kept under:
    lowercase, without the final dot, international names in their ASCII
    (IDNA) form. `example.org.`, `EXAMPLE.org` and `example.org` are one
    host; `localhost` and `127.0.0.1` stay two, as nothing short of a DNS
    lookup could tell they are the same."""
    name = host.strip().lower()
    if name.endswith("."):
        name = name[:-1]
    if not name.isascii():
        try:
            name = name.encode("idna").decode("ascii")
        except UnicodeError:
            raise ValueError(f"not a host name: {host!r}") from None
    if not name or any(not label for label in name.split(".")):
        raise ValueError(f"not a host name: {host!r}")
    return name


def host_of(url: str) -> str:
    """The host a URL points at, normalized (`normalize_host`), without port."""
    host = urlsplit(url).hostname
    if not host:
        raise ValueError(f"no host in URL {url!r}")
    return normalize_host(host)


def _host(host_or_url: str) -> str:
    return host_of(host_or_url) if "://" in host_or_url else normalize_host(host_or_url)


class Gate:
    """The single point every request to a protected host goes through."""

    def __init__(
        self,
        store: Store,
        policies: Mapping[str, HostPolicy],
        *,
        clock: Clock | None = None,
        default: HostPolicy | None = None,
        reconcile_after: timedelta | str = "1h",
    ) -> None:
        """`policies` maps host names to their policy. Hosts without a policy
        are refused with `ValueError` unless a `default` policy is given.
        Rows left in flight for longer than `reconcile_after` are closed as
        failed before each decision.
        """
        self.store = store
        self.clock: Clock = clock if clock is not None else SystemClock()
        self._policies = {
            normalize_host(host): policy for host, policy in policies.items()
        }
        self._default = default
        self.reconcile_after = parse_duration(reconcile_after)
        if self.reconcile_after <= timedelta(0):
            raise ValueError("reconcile_after must be positive")

    def policy_for(self, host: str) -> HostPolicy:
        name = normalize_host(host)
        policy = self._policies.get(name, self._default)
        if policy is None:
            raise NoPolicy(name)
        return policy

    def reconcile(self, now: datetime | None = None) -> list[RequestRow]:
        """Close as failed the rows in flight for longer than `reconcile_after`."""
        now = self.clock.now() if now is None else now
        return self.store.reconcile(now=now, older_than=self.reconcile_after)

    def may_fetch(self, url: str) -> Verdict:
        """Tell whether a request to `url` may start now, and if not why and
        until when. Advisory: `attempt` decides again, atomically. Sends
        nothing and opens no row; it does reconcile abandoned rows."""
        _, _, _, refusal = self._decide(url)
        return Verdict(refusal)

    def status(self, host_or_url: str, *, recent: int = 10) -> HostStatus:
        """The readable state of a host: the decision `may_fetch` would take,
        today's counts and bytes, and its latest `recent` log rows.

        Given a URL, the decision counts the bytes that URL would reserve
        against the byte budget, as `may_fetch` does; given a host, it counts
        none. Like `may_fetch`, it sends nothing, opens no row and reconciles
        abandoned rows first.
        """
        if recent < 0:
            raise ValueError(f"recent must not be negative, got {recent}")
        host, now, state, refusal = self._decide(host_or_url)
        policy = self.policy_for(host)
        rows = self.store.rows(host=host, latest=recent) if recent else []
        freeze = state.freeze
        return HostStatus(
            host=host,
            at=now,
            may_fetch=refusal is None,
            reason=None if refusal is None else refusal.reason,
            next_allowed=now if refusal is None else refusal.retry_after,
            frozen=freeze if freeze is not None and freeze.active(now) else None,
            failures=state.failures,
            throttled=state.throttled,
            in_flight=state.in_flight,
            day=state.day,
            requests_today=state.requests_today,
            daily_cap=policy.daily_cap,
            wire_bytes_today=state.wire_bytes_today,
            decoded_bytes_today=state.decoded_bytes_today,
            byte_budget=policy.byte_budget,
            recent=tuple(reversed(rows)),
        )

    def statuses(
        self, hosts: Iterable[str] | None = None, *, recent: int = 10
    ) -> list[HostStatus]:
        """`status` of each of `hosts` (hosts or URLs), in their order; by
        default of every host with a policy of its own, sorted by name."""
        names = sorted(self._policies) if hosts is None else list(hosts)
        return [self.status(name, recent=recent) for name in names]

    def _decide(
        self, host_or_url: str
    ) -> tuple[str, datetime, HostState, Refused | None]:
        is_url = "://" in host_or_url
        host = _host(host_or_url)
        policy = self.policy_for(host)
        now = self.clock.now()
        self.reconcile(now)
        state = self.store.host_state(host, now)
        reserve = 0
        if is_url:
            record = self.store.url_record(host_or_url)
            reserve = record.size if record is not None and record.size else 0
        refusal = check_admission(
            state, now, policy.daily_cap, policy.byte_budget, reserve
        )
        return host, now, state, refusal

    def attempt(self, url: str) -> "Attempt":
        """A context manager (sync or async) around one request to `url`."""
        host = host_of(url)
        return Attempt(self, url, host, self.policy_for(host))

    def note_data_time(self, url: str, data_time: datetime) -> DataTimeRecord:
        """Record the data time a read of `url` returned, for stuck-source
        detection (`politefetch.checks.check_advancing`)."""
        return self.store.note_data_time(url, data_time, now=self.clock.now())

    def unfreeze(self, host_or_url: str, *, by: str, note: str = "") -> Unfreeze | None:
        """Lift a host's freeze by hand, leaving a trace of who did it."""
        return self.store.unfreeze(
            _host(host_or_url), now=self.clock.now(), by=by, note=note
        )


class Attempt:
    """One request through the gate. Record its outcome with `response` or
    `failure` before the block ends."""

    def __init__(self, gate: Gate, url: str, host: str, policy: HostPolicy) -> None:
        self.url = url
        self.host = host
        self._gate = gate
        self._policy = policy
        self._row: RequestRow | None = None
        self._outcome: Outcome | None = None
        self._retry_at: datetime | None = None
        self._validators: Validators | None = None
        self._conditional: dict[str, str] = {}
        self.result: RequestRow | None = None
        """The closed row, once the block has ended (`None` if it could not be
        closed)."""
        self.bytes_left: int | None = None
        """The wire bytes this request may read before the host's byte budget
        for the day is spent, as of its start; `None` without a budget."""

    @property
    def row(self) -> RequestRow:
        """The log row opened for this request."""
        if self._row is None:
            raise RuntimeError("the attempt has not started")
        return self._row

    @property
    def outcome(self) -> Outcome | None:
        return self._outcome

    @property
    def conditional_headers(self) -> dict[str, str]:
        """`If-None-Match` and `If-Modified-Since` for this URL, from the
        validators of its last answer; send them with the request."""
        return dict(self._conditional)

    def response(
        self,
        status: int,
        *,
        headers: Headers | None = None,
        retry_after: str | None = None,
        wire_bytes: int | None = None,
        decoded_bytes: int | None = None,
    ) -> None:
        """Record that the source answered with HTTP `status`.

        2xx and 304 are good answers; anything else is a failure, a redirect
        too (following it would be another request). From `headers` the gate
        reads Retry-After, ETag and Last-Modified; `retry_after`, if given,
        wins over the header. `wire_bytes` is the body as transferred,
        `decoded_bytes` after decoding.
        """
        if not 100 <= status <= 599:
            raise ValueError(f"not an HTTP status: {status}")
        ok = 200 <= status < 300 or status == 304
        found = _lowercase(headers)
        if retry_after is None:
            retry_after = found.get("retry-after")
        etag, last_modified = found.get("etag"), found.get("last-modified")
        validators = None
        if etag is not None or last_modified is not None:
            validators = Validators(etag=etag, last_modified=last_modified)
        outcome = Outcome(
            ok=ok,
            status=status,
            failure=None if ok else "http",
            wire_bytes=wire_bytes,
            decoded_bytes=decoded_bytes,
        )
        retry_at = parse_retry_after(retry_after, self._gate.clock.now())
        self._record(outcome, retry_at, validators)

    def failure(
        self,
        shape: str,
        *,
        status: int | None = None,
        headers: Headers | None = None,
        wire_bytes: int | None = None,
        decoded_bytes: int | None = None,
    ) -> None:
        """Record that no usable answer came, and how it failed (connect
        timeout, reset, refusal, TLS, truncated body, stale data...).

        If an HTTP answer did arrive but could not be used, pass its `status`
        and `headers`: Retry-After still counts, its validators do not (they
        would vouch for a body that was rejected).
        """
        found = _lowercase(headers)
        outcome = Outcome(
            ok=False,
            status=status,
            failure=shape,
            wire_bytes=wire_bytes,
            decoded_bytes=decoded_bytes,
        )
        retry_at = parse_retry_after(found.get("retry-after"), self._gate.clock.now())
        self._record(outcome, retry_at, None)

    def _record(
        self, outcome: Outcome, retry_at: datetime | None, validators: Validators | None
    ) -> None:
        if self._outcome is not None:
            raise RuntimeError("the outcome of this attempt is already recorded")
        self._outcome = outcome
        self._retry_at = retry_at
        self._validators = validators

    # --- opening and closing ------------------------------------------------

    def _open(self) -> None:
        if self._row is not None:
            raise RuntimeError("an attempt can only be used once")
        gate, policy = self._gate, self._policy
        now = gate.clock.now()
        gate.reconcile(now)
        self._row = gate.store.open_request(
            self.host,
            self.url,
            now=now,
            daily_cap=policy.daily_cap,
            byte_budget=policy.byte_budget,
            cautious=lambda state: policy.cautious_until(state, now),
        )
        if policy.byte_budget is not None:
            # Today's bytes include this request's reservation: it is part of
            # what the request may read, not something already read.
            used = gate.store.host_state(self.host, now).wire_bytes_today
            spent = used - self._row.reserved_bytes
            self.bytes_left = max(0, policy.byte_budget - spent)
        record = gate.store.url_record(self.url)
        if record is not None:
            if record.validators.etag is not None:
                self._conditional["If-None-Match"] = record.validators.etag
            if record.validators.last_modified is not None:
                self._conditional["If-Modified-Since"] = record.validators.last_modified

    def _close(self, exc: BaseException | None) -> None:
        outcome = self._outcome
        if outcome is None:
            if exc is None:
                shape = UNRECORDED
            elif isinstance(exc, asyncio.CancelledError):
                shape = CANCELLED
            else:
                shape = f"exception:{type(exc).__name__}"
            outcome = Outcome(ok=False, failure=shape)
            self._outcome = outcome
        retry_at, policy = self._retry_at, self._policy
        # The store is our own database, not the source: trying it again
        # sends nothing. Each try waits as long as the store's own timeout.
        failed: Exception | None = None
        for _ in range(CLOSE_TRIES):
            now = self._gate.clock.now()

            def update(state: HostState, now: datetime = now) -> HostUpdate:
                return policy.after(state, now, outcome, retry_at)

            try:
                self.result = self._gate.store.close_request(
                    self.row.id,
                    now=now,
                    outcome=outcome,
                    update=update,
                    validators=self._validators,
                )
                return
            except Exception as error:
                failed = error
        # Raised outside the handler, so an exception from the block stays the
        # context and the store's is the cause.
        raise OutcomeNotSaved(
            self.url, self.row.id, outcome.failure, outcome.status
        ) from failed

    def __enter__(self) -> Self:
        self._open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._close(exc)

    async def __aenter__(self) -> Self:
        opening = asyncio.ensure_future(asyncio.to_thread(self._open))
        try:
            await asyncio.shield(opening)
        except asyncio.CancelledError:
            # The open goes on in its thread and may still open the row, and
            # `__aexit__` will not run: close the row here, as it would.
            closing = asyncio.ensure_future(self._close_once_open(opening))
            _CLOSING.add(closing)
            closing.add_done_callback(_closed)
            with contextlib.suppress(Exception):
                await asyncio.shield(closing)
            raise
        return self

    async def _close_once_open(self, opening: "asyncio.Future[None]") -> None:
        try:
            await opening
        except Exception:
            return  # refused, or the store failed: no row was opened
        await asyncio.to_thread(self._close, asyncio.CancelledError())

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        closing = asyncio.ensure_future(asyncio.to_thread(self._close, exc))
        if not isinstance(exc, asyncio.CancelledError):
            await closing
            return
        # The task is being cancelled. The close runs in its own thread, so
        # even a second cancellation, which stops this wait, does not stop
        # it; if the process ends first, the reconciliation closes the row.
        _CLOSING.add(closing)
        closing.add_done_callback(_closed)
        # The cancellation goes on either way: an outcome the store could not
        # save leaves the row to the reconciliation, as a crash would.
        with contextlib.suppress(Exception):
            await asyncio.shield(closing)


_CLOSING: set["asyncio.Future[None]"] = set()
"""Closes started during a cancellation, kept alive until they end."""


def _closed(closing: "asyncio.Future[None]") -> None:
    _CLOSING.discard(closing)
    if not closing.cancelled():
        closing.exception()  # retrieved, even when nobody waits any more
