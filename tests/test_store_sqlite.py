"""SQLite-specific behaviour, beyond the shared contract."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from politefetch.store import SqliteStore

T = datetime(2026, 10, 16, 12, tzinfo=UTC)


def test_settings_for_safety_across_processes(tmp_path: Path) -> None:
    with SqliteStore(tmp_path / "s.db") as store:
        assert store.path == str(tmp_path / "s.db")
    conn = sqlite3.connect(tmp_path / "s.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    finally:
        conn.close()


def test_a_database_from_an_earlier_stage_gains_new_columns(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    try:
        # The hosts table as stage T1 created it, without `throttled`.
        conn.execute(
            "CREATE TABLE hosts (host TEXT PRIMARY KEY, not_before TEXT,"
            " not_before_reason TEXT, failures INTEGER NOT NULL DEFAULT 0,"
            " freeze_since TEXT, freeze_until TEXT, freeze_status INTEGER,"
            " freeze_note TEXT)"
        )
        conn.execute("INSERT INTO hosts (host, failures) VALUES ('example.org', 3)")
        conn.commit()
    finally:
        conn.close()
    with SqliteStore(path) as store:
        state = store.host_state("example.org", T)
        assert state.failures == 3
        assert state.throttled == 0
    SqliteStore(path).close()  # opening again is harmless
