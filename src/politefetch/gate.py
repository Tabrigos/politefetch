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
worker thread. If the task is cancelled the row is not closed, because a
cancelled task cannot safely wait: the reconciliation closes it later, as
failed (SPEC.md, "Sincrono e asincrono").

If the store itself fails (for SQLite, a lock that stays taken past the busy
timeout), its exception propagates from entering the attempt and nothing is
sent.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import Message
from types import TracebackType
from typing import Self
from urllib.parse import urlsplit

from politefetch.clock import Clock, SystemClock
from politefetch.errors import Refused
from politefetch.policy import HostPolicy, parse_duration, parse_retry_after
from politefetch.store.base import (
    DataTimeRecord,
    Outcome,
    RequestRow,
    Store,
    Unfreeze,
    Validators,
    check_admission,
)

__all__ = ["Attempt", "Gate", "Verdict"]

UNRECORDED = "unrecorded"
"""Failure shape of an attempt that ended without an outcome."""

Headers = Mapping[str, str] | Message
"""Response headers: any mapping (httpx, requests) or an `email.message.Message`
(urllib, http.client)."""


def _lowercase(headers: Headers | None) -> dict[str, str]:
    if headers is None:
        return {}
    return {str(name).lower(): str(value) for name, value in headers.items()}


@dataclass(frozen=True, slots=True)
class Verdict:
    """The answer of `Gate.may_fetch`: true if a request may start now."""

    refusal: Refused | None = None

    def __bool__(self) -> bool:
        return self.refusal is None


def host_of(url: str) -> str:
    """The host a URL points at, lowercased, without port."""
    host = urlsplit(url).hostname
    if not host:
        raise ValueError(f"no host in URL {url!r}")
    return host.lower()


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
        self._policies = {host.lower(): policy for host, policy in policies.items()}
        self._default = default
        self.reconcile_after = parse_duration(reconcile_after)
        if self.reconcile_after <= timedelta(0):
            raise ValueError("reconcile_after must be positive")

    def policy_for(self, host: str) -> HostPolicy:
        policy = self._policies.get(host.lower(), self._default)
        if policy is None:
            raise ValueError(f"no policy for host {host!r} and no default policy")
        return policy

    def reconcile(self, now: datetime | None = None) -> list[RequestRow]:
        """Close as failed the rows in flight for longer than `reconcile_after`."""
        now = self.clock.now() if now is None else now
        return self.store.reconcile(now=now, older_than=self.reconcile_after)

    def may_fetch(self, url: str) -> Verdict:
        """Tell whether a request to `url` may start now, and if not why and
        until when. Advisory: `attempt` decides again, atomically. Sends
        nothing and opens no row; it does reconcile abandoned rows."""
        host = host_of(url)
        policy = self.policy_for(host)
        now = self.clock.now()
        self.reconcile(now)
        state = self.store.host_state(host, now)
        record = self.store.url_record(url)
        reserve = record.size if record is not None and record.size else 0
        return Verdict(
            check_admission(state, now, policy.daily_cap, policy.byte_budget, reserve)
        )

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
        host = host_of(host_or_url) if "://" in host_or_url else host_or_url.lower()
        return self.store.unfreeze(host, now=self.clock.now(), by=by, note=note)


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
        """The closed row, once the block has ended (`None` if cancelled)."""

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

        2xx and 3xx (304 included) are good answers; anything else is a
        failure. From `headers` the gate reads Retry-After, ETag and
        Last-Modified; `retry_after`, if given, wins over the header.
        `wire_bytes` is the body as transferred, `decoded_bytes` after
        decoding.
        """
        if not 100 <= status <= 599:
            raise ValueError(f"not an HTTP status: {status}")
        ok = 200 <= status < 400
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
        wire_bytes: int | None = None,
        decoded_bytes: int | None = None,
    ) -> None:
        """Record that no usable HTTP answer came, and how it failed (connect
        timeout, reset, refusal, TLS, truncated body...), with the bytes that
        did arrive, if any."""
        outcome = Outcome(
            ok=False, failure=shape, wire_bytes=wire_bytes, decoded_bytes=decoded_bytes
        )
        self._record(outcome, None, None)

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
        record = gate.store.url_record(self.url)
        if record is not None:
            if record.validators.etag is not None:
                self._conditional["If-None-Match"] = record.validators.etag
            if record.validators.last_modified is not None:
                self._conditional["If-Modified-Since"] = record.validators.last_modified

    def _close(self, exc: BaseException | None) -> None:
        outcome = self._outcome
        if outcome is None:
            shape = UNRECORDED if exc is None else f"exception:{type(exc).__name__}"
            outcome = Outcome(ok=False, failure=shape)
            self._outcome = outcome
        retry_at, policy = self._retry_at, self._policy
        now = self._gate.clock.now()
        self.result = self._gate.store.close_request(
            self.row.id,
            now=now,
            outcome=outcome,
            update=lambda state: policy.after(state, now, outcome, retry_at),
            validators=self._validators,
        )

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
        await asyncio.to_thread(self._open)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if isinstance(exc, asyncio.CancelledError):
            return  # cannot wait safely: the reconciliation closes the row
        await asyncio.to_thread(self._close, exc)
