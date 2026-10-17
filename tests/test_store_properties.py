"""Store invariants under random operations, crashes and restarts.

The machine drives a store the way processes would: it opens requests, closes
some, crashes (forgetting every request it had open), restarts the store,
reconciles abandoned rows, and moves the clock, sometimes backwards. After
every step it checks the invariants of SPEC.md that belong to the store:

- the daily count never loses a request and never exceeds the cap;
- a host never has two requests in flight;
- a promise is never shortened except by a good answer;
- after a reconciliation no row older than the threshold is left open;
- a refused or failed operation writes nothing;
- a restart changes nothing.
"""

import contextlib
from datetime import UTC, date, datetime, timedelta

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    consumes,
    invariant,
    multiple,
    precondition,
    rule,
)

from politefetch.errors import RefusalReason, Refused
from politefetch.store import (
    HostState,
    HostUpdate,
    Outcome,
    RequestRow,
    RowState,
    Store,
    Validators,
)

from .stores import Harness

HOSTS = ["a.example", "b.example"]
PATHS = ["/a", "/b"]
CAP = 3
BUDGET = 5000
START = datetime(2026, 10, 16, 22, 0, tzinfo=UTC)  # near midnight: days change

hosts = st.sampled_from(HOSTS)
pauses = st.timedeltas(
    min_value=timedelta(microseconds=1), max_value=timedelta(hours=3)
)
steps = st.timedeltas(min_value=timedelta(0), max_value=timedelta(hours=2))


class StoreMachine(RuleBasedStateMachine):
    kind = ""

    open_rows = Bundle("open_rows")

    def __init__(self) -> None:
        super().__init__()
        self.harness = Harness(self.kind)
        self.now = START
        self.admitted: dict[tuple[str, date], int] = {}
        self.promise: dict[str, datetime | None] = dict.fromkeys(HOSTS)
        self.forgotten: set[int] = set()

    def teardown(self) -> None:
        self.harness.close()

    @property
    def store(self) -> Store:
        return self.harness.store

    def everything(self) -> object:
        urls = [f"https://{h}{p}" for h in HOSTS for p in PATHS]
        return (
            [self.store.host_state(h, self.now) for h in HOSTS],
            self.store.rows(),
            [self.store.url_record(u) for u in urls],
        )

    def check_promise_not_shortened(self, host: str) -> None:
        before = self.promise[host]
        after = self.store.host_state(host, self.now).not_before
        if before is not None:
            assert after is not None
            assert after >= before
        self.promise[host] = after

    # --- operations ---------------------------------------------------------

    @rule(target=open_rows, host=hosts, path=st.sampled_from(PATHS), pause=pauses)
    def open(self, host: str, path: str, pause: timedelta) -> object:
        before = self.everything()
        now = self.now
        url = f"https://{host}{path}"
        used = self.store.host_state(host, now).wire_bytes_today
        record = self.store.url_record(url)
        expected_reserve = record.size if record is not None and record.size else 0
        try:
            row = self.store.open_request(
                host,
                url,
                now=now,
                daily_cap=CAP,
                byte_budget=BUDGET,
                cautious=lambda _s: now + pause,
            )
        except Refused:
            assert self.everything() == before
            return multiple()
        # Admitted: the budget had room for what this request reserves.
        assert row.reserved_bytes == expected_reserve
        assert used < BUDGET
        assert used + row.reserved_bytes <= BUDGET
        key = (host, now.date())
        self.admitted[key] = self.admitted.get(key, 0) + 1
        self.check_promise_not_shortened(host)
        return row

    @rule(host=hosts)
    def open_with_a_broken_policy(self, host: str) -> None:
        before = self.everything()

        def broken(_state: HostState) -> datetime:
            raise RuntimeError("crash inside the transaction")

        with contextlib.suppress(RuntimeError, Refused):
            self.store.open_request(
                host, "https://x/", now=self.now, daily_cap=CAP, cautious=broken
            )
        assert self.everything() == before

    @rule(
        row=consumes(open_rows),
        status=st.sampled_from([200, 304, None]),
        wire=st.one_of(st.none(), st.integers(min_value=0, max_value=3000)),
        etag=st.sampled_from([None, '"v1"', '"v2"']),
        pause=pauses,
    )
    def close(
        self,
        row: RequestRow,
        status: int | None,
        wire: int | None,
        etag: str | None,
        pause: timedelta,
    ) -> None:
        if row.id in self.forgotten:
            return  # the process that opened it is dead
        now = self.now
        ok = status is not None
        reason = RefusalReason.MIN_INTERVAL if ok else RefusalReason.BACKOFF
        outcome = (
            Outcome(ok=True, status=status, wire_bytes=wire)
            if ok
            else Outcome(ok=False, failure="reset", wire_bytes=wire)
        )
        before = self.store.url_record(row.url)
        closed = self.store.close_request(
            row.id,
            now=now,
            outcome=outcome,
            update=lambda _s: HostUpdate(now + pause, reason),
            validators=None if etag is None else Validators(etag=etag),
        )
        after_close = self.store.url_record(row.url)
        if status == 304 and before is not None:
            # A 304 never forgets what the source told us before.
            assert after_close is not None
            assert after_close.size == before.size
            if before.validators.etag is not None:
                assert after_close.validators.etag is not None
        assert closed.state is not RowState.IN_FLIGHT
        if ok and closed.state is RowState.CLOSED:
            # A good answer may shorten the promise: start tracking afresh.
            self.promise[row.host] = self.store.host_state(row.host, now).not_before
        else:
            self.check_promise_not_shortened(row.host)

    @rule()
    def crash(self) -> None:
        # The process dies: nobody will close the requests it had open, and
        # the next process reopens the store.
        self.forgotten |= {r.id for r in self.store.rows(state=RowState.IN_FLIGHT)}
        self.harness.restart()

    @rule()
    def restart(self) -> None:
        before = self.everything()
        self.harness.restart()
        assert self.everything() == before

    @rule(
        older_than=st.timedeltas(
            min_value=timedelta(seconds=1), max_value=timedelta(hours=4)
        )
    )
    def reconcile(self, older_than: timedelta) -> None:
        self.store.reconcile(now=self.now, older_than=older_than)
        cutoff = self.now - older_than
        for row in self.store.rows(state=RowState.IN_FLIGHT):
            assert row.opened_at > cutoff
        for host in HOSTS:
            self.check_promise_not_shortened(host)

    @rule(step=steps)
    def tick(self, step: timedelta) -> None:
        self.now += step

    @precondition(lambda self: self.now > START)
    @rule(
        step=st.timedeltas(min_value=timedelta(seconds=1), max_value=timedelta(hours=1))
    )
    def clock_jumps_back(self, step: timedelta) -> None:
        self.now -= step

    # --- invariants ---------------------------------------------------------

    @invariant()
    def counts_lose_nothing_and_respect_the_cap(self) -> None:
        for (host, day), admitted in self.admitted.items():
            assert self.store.requests_on(host, day) == admitted
            assert admitted <= CAP

    @invariant()
    def bytes_are_real_bytes_or_reservations(self) -> None:
        expected: dict[tuple[str, date], int] = {}
        for row in self.store.rows():
            real = row.outcome.wire_bytes if row.outcome is not None else None
            key = (row.host, row.opened_at.date())
            expected[key] = expected.get(key, 0) + (
                real if real is not None else row.reserved_bytes
            )
        for (host, day), total in expected.items():
            moment = datetime.combine(day, datetime.min.time(), UTC)
            assert self.store.host_state(host, moment).wire_bytes_today == total

    @invariant()
    def every_admitted_request_has_a_row(self) -> None:
        assert len(self.store.rows()) == sum(self.admitted.values())

    @invariant()
    def at_most_one_request_in_flight_per_host(self) -> None:
        for host in HOSTS:
            in_flight = self.store.rows(host=host, state=RowState.IN_FLIGHT)
            assert len(in_flight) <= 1
            assert self.store.host_state(host, self.now).in_flight == len(in_flight)

    @invariant()
    def closed_rows_carry_an_outcome(self) -> None:
        for row in self.store.rows():
            if row.state is RowState.IN_FLIGHT:
                assert row.outcome is None
                assert row.closed_at is None
            else:
                assert row.outcome is not None
                assert row.closed_at is not None
            if row.state is RowState.RECONCILED:
                assert row.outcome is not None
                assert not row.outcome.ok


class MemoryStoreMachine(StoreMachine):
    kind = "memory"


class SqliteStoreMachine(StoreMachine):
    kind = "sqlite"


TestMemoryStore = MemoryStoreMachine.TestCase
TestSqliteStore = SqliteStoreMachine.TestCase
TestSqliteStore.settings = settings(max_examples=50, deadline=None)
TestMemoryStore.settings = settings(deadline=None)
