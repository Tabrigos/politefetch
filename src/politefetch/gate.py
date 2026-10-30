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
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from email.message import Message
from types import TracebackType
from typing import Self
from urllib.parse import urlsplit

from politefetch.clock import Clock, SleepingClock, SystemClock
from politefetch.errors import NoPolicy, OutcomeNotSaved, RefusalReason, Refused
from politefetch.policy import HostPolicy, parse_duration, parse_retry_after
from politefetch.status import HostStatus, UrlStatus
from politefetch.store.base import (
    REJECTED,
    REJECTIONS,
    DataTimeRecord,
    HostState,
    HostUpdate,
    Outcome,
    Pruned,
    RequestRow,
    RowState,
    Store,
    Unfreeze,
    Validators,
    caps_in_force,
    check_admission,
    limits_in_force,
)

__all__ = ["Attempt", "Gate", "Verdict", "normalize_host"]

_log = logging.getLogger("politefetch")

UNRECORDED = "unrecorded"
"""Failure shape of an attempt that ended without an outcome."""

CANCELLED = "cancelled"
"""Failure shape of an async attempt cancelled before it had an outcome."""

CLOSE_TRIES = 3
"""How many times closing a row is tried before `OutcomeNotSaved`."""

_RECONCILE_WITHOUT_FETCHERS = timedelta(hours=1)
"""The default `reconcile_after` while no fetcher bounds the requests."""

_CAPS = (RefusalReason.DAILY_CAP, RefusalReason.BYTE_BUDGET, RefusalReason.RATE_LIMIT)
"""The refusals a lower cap or limit of another process can cause."""

TAG_LENGTH = 200
"""The longest tag a row keeps (#17)."""

_WAITED = RefusalReason.MIN_INTERVAL
"""The one refusal `wait_up_to` waits out: the host's spacing, short and
ending on its own (D-061)."""

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
        reconcile_after: timedelta | str | None = None,
    ) -> None:
        """`policies` maps host names to their policy. Hosts without a policy
        are refused with `ValueError` unless a `default` policy is given.
        Rows left in flight for longer than `reconcile_after` are closed as
        failed before each decision.

        By default `reconcile_after` follows the fetchers built on this gate:
        twice the longest request they allow (deadline plus one connect and
        one read timeout), or an hour while there are none. Give it by hand
        when you also send requests with `attempt` and a client of your own,
        so that it outlasts the slowest of them.
        """
        self.store = store
        self.clock: Clock = clock if clock is not None else SystemClock()
        self._policies = {
            normalize_host(host): policy for host, policy in policies.items()
        }
        self._default = default
        self._reconcile_after = (
            None if reconcile_after is None else parse_duration(reconcile_after)
        )
        if self._reconcile_after is not None and self._reconcile_after <= timedelta(0):
            raise ValueError("reconcile_after must be positive")
        self._longest_request: timedelta | None = None
        self._warned: set[tuple[str, str, object, object]] = set()

    @property
    def reconcile_after(self) -> timedelta:
        """How long a row may stay in flight before it counts as abandoned."""
        if self._reconcile_after is not None:
            return self._reconcile_after
        if self._longest_request is not None:
            return 2 * self._longest_request
        return _RECONCILE_WITHOUT_FETCHERS

    def bound_requests(self, longest: timedelta) -> None:
        """Note that requests through this gate end within `longest`, as a
        fetcher's do: the default `reconcile_after` follows the longest one
        noted. Called by the fetchers; a value given by hand is kept."""
        if self._longest_request is None or longest > self._longest_request:
            self._longest_request = longest

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

    def prune(self, older_than: timedelta | str) -> Pruned:
        """Remove the closed log rows opened more than `older_than` ago (at
        least a day: the limits count the rows of the last day), and what
        no row refers to any more (`Store.prune`). Run it now and then, as
        a daily job; the hosts' state stays."""
        return self.store.prune(
            now=self.clock.now(), older_than=parse_duration(older_than)
        )

    def may_fetch(self, url: str, *, key: str | None = None) -> Verdict:
        """Tell whether a request to `url` (under `key`, the URL by default)
        may start now, and if not why and until when. Advisory: `attempt`
        decides again, atomically. Sends nothing and writes nothing: rows
        abandoned past `reconcile_after` count as the next request to the
        host will count them."""
        _, _, _, _, refusal = self._decide(url, key)
        return Verdict(refusal)

    def status(
        self, host_or_url: str, *, recent: int = 10, key: str | None = None
    ) -> HostStatus:
        """The readable state of a host: the decision `may_fetch` would take,
        today's counts and bytes, the limits in force, its last good answer,
        and its latest `recent` log rows.

        Given a URL, the decision counts the bytes that URL would reserve
        against the byte budget and the windows of its key (`key`, the URL by
        default), as `may_fetch` does, and `url` tells that URL's last good
        answer, not-ready count and pause; given a host, it counts no bytes,
        the per-key windows only for a `key` given, and `url` is `None`. Like
        `may_fetch`, it
        sends nothing and writes nothing, so it can run beside the process
        that fetches, or without the right to write.
        """
        if recent < 0:
            raise ValueError(f"recent must not be negative, got {recent}")
        host, now, state, abandoned, refusal = self._decide(host_or_url, key)
        policy = self.policy_for(host)
        daily_cap, byte_budget = caps_in_force(
            state, policy.daily_cap, policy.byte_budget
        )
        limits = limits_in_force(state, policy.limits + policy.key_limits)
        rows = self.store.rows(host=host, latest=recent) if recent else []
        freeze = state.freeze
        url = None
        if "://" in host_or_url:
            record = self.store.url_record(host_or_url)
            url = UrlStatus(host_or_url, last_good=None, not_ready=0)
            if record is not None:
                paused = record.not_before
                url = UrlStatus(
                    host_or_url,
                    last_good=record.last_good,
                    not_ready=record.not_ready,
                    failures=record.failures,
                    paused_until=paused
                    if paused is not None and now < paused
                    else None,
                )
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
            abandoned=abandoned,
            day=state.day,
            requests_today=state.requests_today,
            daily_cap=daily_cap,
            wire_bytes_today=state.wire_bytes_today,
            decoded_bytes_today=state.decoded_bytes_today,
            byte_budget=byte_budget,
            recent=tuple(reversed(rows)),
            limits=limits,
            last_good=state.last_good,
            url=url,
        )

    def statuses(
        self, hosts: Iterable[str] | None = None, *, recent: int = 10
    ) -> list[HostStatus]:
        """`status` of each of `hosts` (hosts or URLs), in their order; by
        default of every host with a policy of its own and, when the gate has
        a `default` policy, of every host the store knows, sorted by name."""
        if hosts is None:
            known = set(self._policies)
            if self._default is not None:
                known.update(self.store.hosts())
            names = sorted(known)
        else:
            names = list(hosts)
        return [self.status(name, recent=recent) for name in names]

    def _decide(
        self, host_or_url: str, key: str | None = None
    ) -> tuple[str, datetime, HostState, int, Refused | None]:
        """The decision `attempt` would take now, read without writing: the
        rows past `reconcile_after` count as the next request to the host
        will count them when it closes them, one failure each."""
        is_url = "://" in host_or_url
        if key is None and is_url:
            key = host_or_url
        host = _host(host_or_url)
        policy = self.policy_for(host)
        now = self.clock.now()
        state = self.store.host_state(host, now)
        cutoff = now - self.reconcile_after
        abandoned = sum(
            row.opened_at <= cutoff
            for row in self.store.rows(host=host, state=RowState.IN_FLIGHT)
        )
        if abandoned:
            state = replace(
                state,
                in_flight=max(0, state.in_flight - abandoned),
                failures=state.failures + abandoned,
            )
        reserve, paused = 0, None
        if is_url:
            record = self.store.url_record(host_or_url)
            reserve = record.size if record is not None and record.size else 0
            paused = record.not_before if record is not None else None
        limits = limits_in_force(state, policy.limits + policy.key_limits)
        starts = self.store.limit_starts(host, limits, key=key)
        refusal = check_admission(
            state,
            now,
            policy.daily_cap,
            policy.byte_budget,
            reserve,
            list(zip(limits, starts, strict=True)),
            paused,
        )
        return host, now, state, abandoned, refusal

    def _warn_lower_caps(self, policy: HostPolicy, state: HostState) -> None:
        """Say, once for each pair, that this gate's cap for the host is above
        the one in force: another process sharing the store asked for a
        lower one, and the lower one applies (#31, D-059 for the limits)."""
        in_force = {
            limit.scope: limit
            for limit in limits_in_force(state, policy.limits + policy.key_limits)
        }
        for limit in policy.limits + policy.key_limits:
            lower = in_force[limit.scope]
            seen = (state.host, "limit", str(limit), str(lower))
            if lower.count == limit.count or seen in self._warned:
                continue
            self._warned.add(seen)
            _log.warning(
                "%s: this process asks for %slimit %s, above the %s another"
                " process sharing the store asked for in the last day; the lower"
                " one applies until a day after it was last asked for",
                state.host,
                "per-key " if limit.per_key else "",
                limit,
                lower,
            )
        caps = caps_in_force(state, policy.daily_cap, policy.byte_budget)
        own = (policy.daily_cap, policy.byte_budget)
        for name, mine, held in zip(
            ("daily_cap", "byte_budget"), own, caps, strict=True
        ):
            if held is None or mine == held:
                continue
            key = (state.host, name, mine, held)
            if key in self._warned:
                continue
            self._warned.add(key)
            _log.warning(
                "%s: this process asks for %s %s, above the %d another process"
                " sharing the store asked for in the last day; the lower one"
                " applies until a day after it was last asked for",
                state.host,
                name,
                "None (no limit)" if mine is None else mine,
                held,
            )

    def attempt(
        self,
        url: str,
        *,
        key: str | None = None,
        wait_up_to: timedelta | str | None = None,
        tag: str | None = None,
    ) -> "Attempt":
        """A context manager (sync or async) around one request to `url`.
        `key` names what the request reads for the policy's `key_limits`;
        without it the URL is the key. `tag` names the logical call the
        request belongs to, up to `TAG_LENGTH` characters: the row keeps
        it, for a dashboard.

        With `wait_up_to`, a refusal for `min_interval` that ends within that
        bound, counted from the first refusal, is waited out through the
        gate's clock (a `SleepingClock`) and the gate is asked again; any
        other refusal is raised at once, and so is a wait past the bound.
        Nothing is open and nothing is sent while it waits.
        """
        wait = None if wait_up_to is None else parse_duration(wait_up_to)
        if wait is not None and not isinstance(self.clock, SleepingClock):
            raise TypeError(
                "wait_up_to needs a clock that can sleep: give the gate's clock"
                " sleep() and sleep_async() (SleepingClock), as SystemClock and"
                " FakeClock have"
            )
        if tag is not None and not 0 < len(tag) <= TAG_LENGTH:
            raise ValueError(f"a tag has 1 to {TAG_LENGTH} characters, got {len(tag)}")
        host = host_of(url)
        return Attempt(self, url, host, self.policy_for(host), key, wait, tag)

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
    """One request through the gate. Record what the source did with
    `response` or `failure` before the block ends, and, for a good answer,
    the verdict on its body: `accept` when you take it, `reject` when not.
    `reject("not_sent")` closes an attempt that sent nothing."""

    def __init__(
        self,
        gate: Gate,
        url: str,
        host: str,
        policy: HostPolicy,
        key: str | None = None,
        wait_up_to: timedelta | None = None,
        tag: str | None = None,
    ) -> None:
        self.url = url
        self.host = host
        self.key = url if key is None else key
        """What the per-key limits count this request under."""
        self.tag = tag
        """The caller's name for the logical call, kept in the row."""
        self._gate = gate
        self._policy = policy
        self._wait_up_to = wait_up_to
        self._wait_until: datetime | None = None
        self._asked_at: datetime | None = None
        self._row: RequestRow | None = None
        self._outcome: Outcome | None = None
        self._retry_at: datetime | None = None
        self._validators: Validators | None = None
        self._verdict: str | None = None
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

        The validators of a good answer are saved only if you then `accept`
        it: without a verdict the next request asks for the whole body.
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

    def accept(self) -> None:
        """Record that you took the body of the good answer recorded with
        `response`: its validators are saved, so the next request can be
        conditional, and the host's failures start again from zero."""
        outcome = self._outcome
        if outcome is None or not outcome.ok:
            raise RuntimeError(
                "accept() needs a 2xx or 304 answer recorded with response() first"
            )
        self._give("accepted")

    def reject(self, reason: str) -> None:
        """Record why you did not take a good answer: `"not_ready"` (complete
        but not ready, such as half a JSON), `"unusable"` (your parser
        refused it), or `"not_sent"` for an attempt that sent nothing.

        A rejected answer is still an answer: `min_interval` and Retry-After
        hold, the failures stay as they were, and its validators are not
        saved: those of the last accepted answer stay, so the next request
        stays conditional on the copy you kept. The row records it as
        `"rejected:<reason>"`.

        A page where data was expected (HTML) is a failure, not a rejection:
        check it before `response` and record it with `failure`, as the
        fetchers do.
        """
        if reason not in REJECTIONS:
            known = ", ".join(sorted(REJECTIONS))
            raise ValueError(f"reason must be one of {known}, got {reason!r}")
        outcome = self._outcome
        if reason == "not_sent":
            if outcome is not None:
                raise RuntimeError(
                    "reject('not_sent') is for an attempt that sent nothing,"
                    " and this one has an outcome"
                )
        elif outcome is None or not outcome.ok:
            raise RuntimeError(
                "reject() needs a 2xx or 304 answer recorded with response();"
                " an answer that failed is recorded by response() or failure()"
            )
        self._give(reason)

    def _give(self, verdict: str) -> None:
        if self._verdict is not None:
            raise RuntimeError("the verdict of this attempt is given once")
        self._verdict = verdict

    def _settled(self) -> tuple[Outcome | None, Validators | None]:
        """The outcome to close with, and the validators to save, after the
        verdict."""
        outcome, verdict = self._outcome, self._verdict
        if verdict == "not_sent":
            return Outcome(ok=False, failure=REJECTED + verdict), None
        if verdict in REJECTIONS and outcome is not None:
            return replace(outcome, ok=False, failure=REJECTED + verdict), None
        if verdict is None and outcome is not None and outcome.ok:
            return outcome, None  # not vouched for: no validators
        return outcome, self._validators

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
        try:
            self._row = gate.store.open_request(
                self.host,
                self.url,
                now=now,
                daily_cap=policy.daily_cap,
                byte_budget=policy.byte_budget,
                cautious=lambda state: policy.cautious_until(state, now),
                abandon_before=now - gate.reconcile_after,
                limits=policy.limits + policy.key_limits,
                key=self.key,
                tag=self.tag,
            )
        except Refused as refusal:
            if refusal.reason in _CAPS:
                gate._warn_lower_caps(policy, gate.store.host_state(self.host, now))
            raise
        state = gate.store.host_state(self.host, now)
        gate._warn_lower_caps(policy, state)
        _, budget = caps_in_force(state, policy.daily_cap, policy.byte_budget)
        if budget is not None:
            # Today's bytes include this request's reservation: it is part of
            # what the request may read, not something already read.
            spent = state.wire_bytes_today - self._row.reserved_bytes
            self.bytes_left = max(0, budget - spent)
        record = gate.store.url_record(self.url)
        if record is not None:
            if record.validators.etag is not None:
                self._conditional["If-None-Match"] = record.validators.etag
            if record.validators.last_modified is not None:
                self._conditional["If-Modified-Since"] = record.validators.last_modified

    def _close(self, exc: BaseException | None) -> None:
        outcome, validators = self._settled()
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
        accepted = self._verdict == "accepted"
        # The store is our own database, not the source: trying it again
        # sends nothing. Each try waits as long as the store's own timeout.
        failed: Exception | None = None
        frozen: list[bool] = []
        for _ in range(CLOSE_TRIES):
            now = self._gate.clock.now()

            def update(state: HostState, now: datetime = now) -> HostUpdate:
                decided = policy.after(state, now, outcome, retry_at)
                if accepted:
                    decided = replace(decided, accepted_at=now)
                was = state.freeze is not None and state.freeze.active(now)
                frozen[:] = [decided.freeze is not None and not was]
                return decided

            try:
                self.result = self._gate.store.close_request(
                    self.row.id,
                    now=now,
                    outcome=outcome,
                    update=update,
                    validators=validators,
                )
            except Exception as error:
                failed = error
                continue
            if frozen == [True]:
                _log.warning(
                    "%s frozen after %s from %s: no request until a person"
                    " lifts it with gate.unfreeze(%r, by=...)",
                    self.host,
                    outcome.status if outcome.status is not None else outcome.failure,
                    self.url,
                    self.host,
                )
            return
        _log.error(
            "the outcome of %s (row %d, %s) was not saved: the host stays"
            " blocked until the reconciliation; a 403/429/451 may need to be"
            " applied by hand",
            self.url,
            self.row.id,
            outcome.status if outcome.status is not None else outcome.failure,
            exc_info=failed,
        )
        # Raised outside the handler, so an exception from the block stays the
        # context and the store's is the cause.
        raise OutcomeNotSaved(
            self.url, self.row.id, outcome.failure, outcome.status
        ) from failed

    def _pause(self, refusal: Refused) -> timedelta | None:
        """How long to wait before asking the gate again, or `None` to raise."""
        until = refusal.retry_after
        if self._wait_up_to is None or refusal.reason is not _WAITED or until is None:
            return None
        now = self._gate.clock.now()
        if self._wait_until is None:
            self._wait_until = now + self._wait_up_to
        elif self._asked_at is not None and now <= self._asked_at:
            # The clock did not move while it slept: asking again would spin.
            return None
        self._asked_at = now
        if until <= now or until > self._wait_until:
            return None
        return until - now

    def _sleeper(self) -> SleepingClock:
        clock = self._gate.clock
        assert isinstance(clock, SleepingClock)  # checked by Gate.attempt
        return clock

    def __enter__(self) -> Self:
        while True:
            try:
                self._open()
            except Refused as refusal:
                pause = self._pause(refusal)
                if pause is None:
                    raise
                self._sleeper().sleep(pause)
                continue
            return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._close(exc)

    async def __aenter__(self) -> Self:
        while True:
            opening = asyncio.ensure_future(asyncio.to_thread(self._open))
            try:
                await asyncio.shield(opening)
            except asyncio.CancelledError:
                # The open goes on in its thread and may still open the row,
                # and `__aexit__` will not run: close the row here, as it would.
                closing = asyncio.ensure_future(self._close_once_open(opening))
                _CLOSING.add(closing)
                closing.add_done_callback(_closed)
                with contextlib.suppress(Exception):
                    await asyncio.shield(closing)
                raise
            except Refused as refusal:
                pause = self._pause(refusal)
                if pause is None:
                    raise
                # Nothing is open while it waits: a cancellation here leaves
                # nothing to close.
                await self._sleeper().sleep_async(pause)
                continue
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
