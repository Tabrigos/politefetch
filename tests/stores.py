"""Store harnesses shared by the contract suite and the property tests.

A harness owns one store and can restart it: close it and open a new one over
the same persistent state, as a process restart would.
"""

import tempfile
from pathlib import Path

from politefetch.store import MemoryStore, SqliteStore, Store

KINDS = ["memory", "sqlite"]


class Harness:
    def __init__(self, kind: str, directory: Path | None = None) -> None:
        self.kind = kind
        self._tmp: tempfile.TemporaryDirectory[str] | None = None
        if directory is None:
            self._tmp = tempfile.TemporaryDirectory()
            directory = Path(self._tmp.name)
        self.path = directory / "store.db"
        self.store: Store = self._open(None)

    def _open(self, previous: Store | None) -> Store:
        if self.kind == "memory":
            data = previous.data if isinstance(previous, MemoryStore) else None
            return MemoryStore(data)
        if self.kind == "sqlite":
            return SqliteStore(self.path)
        raise ValueError(f"unknown store kind {self.kind!r}")

    def restart(self) -> Store:
        """Close the store and open it again over the same state."""
        previous = self.store
        previous.close()
        self.store = self._open(previous)
        return self.store

    def close(self) -> None:
        self.store.close()
        if self._tmp is not None:
            self._tmp.cleanup()
