"""Stores shared by several processes: SQLite on one file, Postgres on one
schema."""

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from politefetch.store import RowState
from politefetch.testing.processes import ChildFailed, spawn

from . import children
from .stores import POSTGRES_DSN, drop_schema, fresh_schema, needs_postgres

HOST = "example.org"
T = datetime(2026, 10, 16, 12, tzinfo=UTC)


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=needs_postgres)])
def spec(
    request: pytest.FixtureRequest, tmp_path: Path
) -> Iterator[children.StoreSpec]:
    if request.param == "sqlite":
        found: children.StoreSpec = ("sqlite", str(tmp_path / "shared.db"), "")
    else:
        found = ("postgres", POSTGRES_DSN, fresh_schema())
    children.open_store(found).close()  # create the schema once
    yield found
    if found[0] == "postgres":
        drop_schema(found[2])


def test_processes_together_never_exceed_the_cap(spec: children.StoreSpec) -> None:
    cap = 30
    start_ns = time.monotonic_ns()
    workers = [spawn(children.hammer, spec, HOST, cap, 300, start_ns) for _ in range(4)]
    admitted = [w.join(timeout=300) for w in workers]
    assert sum(admitted) == cap
    with children.open_store(spec) as store:
        assert store.requests_on(HOST, children.DAY_START.date()) == cap
        rows = store.rows()
        assert len(rows) == cap
        assert all(r.state is RowState.CLOSED for r in rows)


def test_a_process_dying_mid_request_leaves_a_row_to_reconcile(
    spec: children.StoreSpec,
) -> None:
    with pytest.raises(ChildFailed) as died:
        spawn(children.open_and_die, spec, HOST, T).join(timeout=60)
    assert died.value.exitcode == 3
    with children.open_store(spec) as store:
        state = store.host_state(HOST, T)
        assert state.in_flight == 1
        assert state.requests_today == 1
        assert state.not_before == T + timedelta(minutes=5)
        reconciled = store.reconcile(
            now=T + timedelta(hours=2), older_than=timedelta(hours=1)
        )
        assert len(reconciled) == 1
        assert store.rows(state=RowState.IN_FLIGHT) == []
        assert store.host_state(HOST, T).failures == 1


def test_a_process_killed_inside_a_transaction_writes_nothing(
    spec: children.StoreSpec, tmp_path: Path
) -> None:
    marker = tmp_path / "inside"
    child = spawn(children.hang_inside_open, spec, HOST, T, str(marker))
    deadline = time.monotonic() + 60
    while not marker.exists():
        assert time.monotonic() < deadline, "the child never entered the transaction"
        time.sleep(0.02)
    child.kill()
    with pytest.raises(ChildFailed):
        child.join(timeout=60)
    with children.open_store(spec, wait=10) as store:
        assert store.rows() == []
        assert store.host_state(HOST, T).requests_today == 0
        store.open_request(
            HOST, "https://x/", now=T, daily_cap=1, cautious=lambda _s: T + timedelta(1)
        )
