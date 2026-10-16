"""The persistent state behind the gate: promises, counts, open log rows,
freezes, validators.

Critical operations are atomic, across processes too: two processes sharing a
store never exceed a cap together. Implementations: memory (tests), SQLite
(default), Postgres (extra). The interface is defined by stage T1.
"""
