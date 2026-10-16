"""SQLite store shared by several processes on the same file."""

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from politefetch.store import RowState, SqliteStore
from politefetch.testing.processes import ChildFailed, spawn

from . import children

HOST = "example.org"
T = datetime(2026, 10, 16, 12, tzinfo=UTC)


def test_processes_together_never_exceed_the_cap(tmp_path: Path) -> None:
    path = str(tmp_path / "shared.db")
    SqliteStore(path).close()  # create the schema once
    cap = 30
    start_ns = time.monotonic_ns()
    workers = [spawn(children.hammer, path, HOST, cap, 300, start_ns) for _ in range(4)]
    admitted = [w.join(timeout=300) for w in workers]
    assert sum(admitted) == cap
    with SqliteStore(path) as store:
        assert store.requests_on(HOST, children.DAY_START.date()) == cap
        rows = store.rows()
        assert len(rows) == cap
        assert all(r.state is RowState.CLOSED for r in rows)


def test_a_process_dying_mid_request_leaves_a_row_to_reconcile(tmp_path: Path) -> None:
    path = str(tmp_path / "shared.db")
    with pytest.raises(ChildFailed) as died:
        spawn(children.open_and_die, path, HOST, T).join(timeout=60)
    assert died.value.exitcode == 3
    with SqliteStore(path) as store:
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


def test_a_process_killed_inside_a_transaction_writes_nothing(tmp_path: Path) -> None:
    path = str(tmp_path / "shared.db")
    marker = tmp_path / "inside"
    SqliteStore(path).close()
    child = spawn(children.hang_inside_open, path, HOST, T, str(marker))
    deadline = time.monotonic() + 60
    while not marker.exists():
        assert time.monotonic() < deadline, "the child never entered the transaction"
        time.sleep(0.02)
    child.kill()
    with pytest.raises(ChildFailed):
        child.join(timeout=60)
    with SqliteStore(path, busy_timeout=10) as store:
        assert store.rows() == []
        assert store.host_state(HOST, T).requests_today == 0
        store.open_request(
            HOST, "https://x/", now=T, daily_cap=1, cautious=lambda _s: T + timedelta(1)
        )
