"""The gate: decides whether a request may start and keeps the request log.

Before a request it writes the cautious promise and consumes the daily cap in
one atomic store operation; after it, it closes the log row with the outcome
in a `finally`. Filled in by stage T2.
"""
