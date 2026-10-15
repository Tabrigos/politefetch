# fetchguard

A polite, crash-safe gate between an ingest service and the external sources
it polls. It keeps the source safe from you (one attempt per call, persistent
per-host promises and daily caps that survive restarts, freezing on 403/429)
and keeps you safe from the source (truncated bodies, HTML instead of JSON,
data that stopped advancing even though every request "succeeds").

**Status: in development, not released.** See `SPEC.md`.
