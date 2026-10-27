"""Write `store-0.1.1.db`, a SQLite store as politefetch 0.1.1 leaves it.

Run it with politefetch 0.1.1 installed from PyPI, not with this checkout:

    uv venv /tmp/pf011 && uv pip install --python /tmp/pf011 politefetch==0.1.1
    cd tests/fixtures && /tmp/pf011/bin/python -I make_store_0_1.py store-0.1.1.db

The file holds one of each kind of state a store keeps, with the values that
`tests/test_store_schema.py` expects: a promise, daily counts, validators, a
data time, a freeze, a lifted freeze, a backoff, a row left in flight and a
reconciled row.
"""

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from importlib import metadata
from pathlib import Path

from politefetch import FakeClock, Gate, HostPolicy
from politefetch.store import SqliteStore

assert metadata.version("politefetch") == "0.1.1", "run with politefetch 0.1.1"

T0 = datetime(2026, 10, 20, 12, tzinfo=UTC)
path = Path(sys.argv[1])
path.unlink(missing_ok=True)

clock = FakeClock(T0)
policy = HostPolicy(daily_cap=100, min_interval="10m")
hosts = ["ok.example", "frozen.example", "lifted.example", "failing.example"]
store = SqliteStore(path)
gate = Gate(store, dict.fromkeys([*hosts, "stuck.example"], policy), clock=clock)

# A good answer with validators and a data time: a promise for T0 + 10m.
with gate.attempt("https://ok.example/data.json") as attempt:
    attempt.response(
        200,
        headers={"ETag": '"v1"', "Last-Modified": "Tue, 20 Oct 2026 11:00:00 GMT"},
        wire_bytes=120,
        decoded_bytes=480,
    )
gate.note_data_time("https://ok.example/data.json", T0 - timedelta(minutes=30))

# A 429: frozen until a person lifts it.
with gate.attempt("https://frozen.example/feed") as attempt:
    attempt.response(429)

# A 403, lifted by a person.
with gate.attempt("https://lifted.example/feed") as attempt:
    attempt.response(403)
gate.unfreeze("lifted.example", by="operator", note="checked by hand")

# Two failures in a row: a backoff.
for _ in range(2):
    with gate.attempt("https://failing.example/feed") as attempt:
        attempt.failure("connect")
    clock.advance(timedelta(hours=1))

# A row whose process died, then reconciled an hour later.
gate.attempt("https://stuck.example/feed").__enter__()
clock.advance(timedelta(hours=2))
gate.reconcile()

# A row left in flight by a process killed mid-request.
gate.attempt("https://ok.example/other.json").__enter__()

store.close()
conn = sqlite3.connect(path)
conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
conn.execute("PRAGMA journal_mode=DELETE")
conn.execute("VACUUM")
conn.close()
print("wrote", path)
