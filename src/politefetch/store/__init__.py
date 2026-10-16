"""The persistent state behind the gate: promises, counts, open log rows,
freezes.

Critical operations are atomic, across processes too: two processes sharing a
store never exceed a cap together. Implementations: `MemoryStore` (tests),
`SqliteStore` (default), Postgres (extra, stage T7). The interface and the
rules every store applies are in `politefetch.store.base`.
"""

from politefetch.store.base import (
    Freeze,
    HostState,
    HostUpdate,
    Outcome,
    RequestRow,
    RowState,
    Store,
    Unfreeze,
)
from politefetch.store.memory import MemoryData, MemoryStore
from politefetch.store.sqlite import SqliteStore

__all__ = [
    "Freeze",
    "HostState",
    "HostUpdate",
    "MemoryData",
    "MemoryStore",
    "Outcome",
    "RequestRow",
    "RowState",
    "SqliteStore",
    "Store",
    "Unfreeze",
]
