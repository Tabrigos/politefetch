"""The injectable clock: a real one and a fake one tests can drive."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from politefetch.clock import Clock, FakeClock, SystemClock, as_utc

T = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def test_both_clocks_satisfy_the_protocol() -> None:
    clocks: list[Clock] = [SystemClock(), FakeClock(T)]
    for clock in clocks:
        assert clock.now().tzinfo is UTC


def test_system_clock_is_close_to_real_time() -> None:
    before = datetime.now(UTC)  # noqa: TID251 - checking the real clock
    now = SystemClock().now()
    after = datetime.now(UTC)  # noqa: TID251 - checking the real clock
    assert before <= now <= after


def test_fake_clock_stands_still_until_moved() -> None:
    clock = FakeClock(T)
    assert clock.now() == T
    assert clock.now() == T


def test_fake_clock_advances() -> None:
    clock = FakeClock(T)
    clock.advance(timedelta(minutes=90))
    assert clock.now() == T + timedelta(minutes=90)


def test_fake_clock_refuses_to_advance_backwards() -> None:
    clock = FakeClock(T)
    with pytest.raises(ValueError, match="set"):
        clock.advance(timedelta(seconds=-1))
    assert clock.now() == T


def test_fake_clock_can_jump_anywhere() -> None:
    # Wall clocks do jump (NTP, VM resume); tests must be able to say so.
    clock = FakeClock(T)
    clock.set(T - timedelta(days=1))
    assert clock.now() == T - timedelta(days=1)


def test_fake_clock_rejects_naive_times() -> None:
    with pytest.raises(ValueError, match="timezone"):
        FakeClock(datetime(2026, 10, 16))  # noqa: DTZ001
    clock = FakeClock(T)
    with pytest.raises(ValueError, match="timezone"):
        clock.set(datetime(2026, 10, 16))  # noqa: DTZ001


def test_as_utc_normalises_zero_offset_zones() -> None:
    zero = timezone(timedelta(0), "GMT")
    value = as_utc(datetime(2026, 10, 16, 12, tzinfo=zero))
    assert value.tzinfo is UTC
    assert value == T


def test_as_utc_rejects_other_offsets() -> None:
    rome = timezone(timedelta(hours=2))
    with pytest.raises(ValueError, match="UTC"):
        as_utc(datetime(2026, 10, 16, 14, tzinfo=rome))


@given(
    start=st.datetimes(
        min_value=datetime(2000, 1, 1),  # noqa: DTZ001 - hypothesis bounds are naive
        max_value=datetime(2100, 1, 1),  # noqa: DTZ001
        timezones=st.just(UTC),
    ),
    steps=st.lists(st.timedeltas(min_value=timedelta(0), max_value=timedelta(days=30))),
)
def test_fake_clock_time_is_the_sum_of_its_steps(
    start: datetime, steps: list[timedelta]
) -> None:
    clock = FakeClock(start)
    previous = clock.now()
    for step in steps:
        clock.advance(step)
        assert clock.now() >= previous
        previous = clock.now()
    assert clock.now() == start + sum(steps, timedelta(0))
