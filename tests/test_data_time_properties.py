"""The data-time history behind stuck-source detection, against a model.

Whatever sequence of data times a source returns, across restarts of the
store, the record holds the latest time seen and the number of consecutive
reads that did not go past it.
"""

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from .stores import KINDS, Harness

T = datetime(2026, 10, 17, 12, tzinfo=UTC)
URL = "https://example.org/data.json"

observations = st.lists(
    st.one_of(
        st.integers(min_value=-5, max_value=5).map(lambda h: ("read", h)),
        st.just(("restart", 0)),
    ),
    max_size=30,
)


@pytest.mark.parametrize("kind", KINDS)
@settings(max_examples=60, deadline=None)
@given(steps=observations)
def test_the_record_matches_the_model(kind: str, steps: list[tuple[str, int]]) -> None:
    harness = Harness(kind)
    try:
        latest: datetime | None = None
        reads = 0
        now = T
        for action, hours in steps:
            if action == "restart":
                harness.restart()
                continue
            data_time = T + timedelta(hours=hours)
            now += timedelta(minutes=1)
            if latest is None or data_time > latest:
                latest, reads = data_time, 1
            else:
                reads += 1
            record = harness.store.note_data_time(URL, data_time, now=now)
            assert (record.latest, record.reads, record.updated_at) == (
                latest,
                reads,
                now,
            )
        stored = harness.store.data_time_record(URL)
        if latest is None:
            assert stored is None
        else:
            assert stored is not None
            assert (stored.latest, stored.reads) == (latest, reads)
    finally:
        harness.close()
