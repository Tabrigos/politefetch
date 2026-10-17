"""Host policies: pure functions from host state and outcome to promises."""

from datetime import UTC, date, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from politefetch.errors import RefusalReason
from politefetch.policy import HostPolicy, parse_duration, parse_retry_after, parse_size
from politefetch.store import HostState, Outcome

T = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
OK = Outcome(ok=True, status=200)
TIMEOUT = Outcome(ok=False, failure="connect_timeout")


def state(failures: int = 0, throttled: int = 0) -> HostState:
    return HostState(
        host="example.org",
        day=date(2026, 10, 16),
        failures=failures,
        throttled=throttled,
    )


def http(status: int) -> Outcome:
    return Outcome(
        ok=200 <= status < 400, status=status, failure=None if status < 400 else "http"
    )


# --- durations ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("60s", timedelta(seconds=60)),
        ("2h", timedelta(hours=2)),
        ("15m", timedelta(minutes=15)),
        ("1d", timedelta(days=1)),
        ("1h30m", timedelta(hours=1, minutes=30)),
        ("500ms", timedelta(milliseconds=500)),
        (" 2h ", timedelta(hours=2)),
        ("0s", timedelta(0)),
    ],
)
def test_parse_duration(text: str, expected: timedelta) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "60", "1x", "h", "1h 30m", "-5m", "1.5h", "m1"])
def test_parse_duration_rejects_ambiguous_text(text: str) -> None:
    with pytest.raises(ValueError, match="duration"):
        parse_duration(text)


def test_parse_duration_passes_timedeltas_through() -> None:
    assert parse_duration(timedelta(minutes=3)) == timedelta(minutes=3)


def test_parse_duration_rejects_negative_timedeltas() -> None:
    with pytest.raises(ValueError, match="negative"):
        parse_duration(timedelta(seconds=-1))


# --- Retry-After ----------------------------------------------------------------


def test_retry_after_in_seconds() -> None:
    assert parse_retry_after("120", T) == T + timedelta(seconds=120)


def test_retry_after_as_http_date() -> None:
    assert parse_retry_after("Fri, 16 Oct 2026 14:00:00 GMT", T) == datetime(
        2026, 10, 16, 14, tzinfo=UTC
    )


def test_retry_after_in_the_past_means_now() -> None:
    assert parse_retry_after("Fri, 16 Oct 2026 10:00:00 GMT", T) == T


@pytest.mark.parametrize("value", [None, "", "soon", "-5", "1.5", "Fri, 99 Oct 2026"])
def test_unreadable_retry_after_is_ignored(value: str | None) -> None:
    assert parse_retry_after(value, T) is None


# --- validation -------------------------------------------------------------------


def test_policy_accepts_strings_and_timedeltas() -> None:
    policy = HostPolicy(
        daily_cap=40, min_interval="2h", backoff_base=timedelta(minutes=2)
    )
    assert policy.min_interval == timedelta(hours=2)
    assert policy.backoff_base == timedelta(minutes=2)
    assert policy.freeze_for is None
    assert policy.respect_retry_after_429 is False


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"daily_cap": -1}, "daily_cap"),
        ({"backoff_base": "0s"}, "backoff_base"),
        ({"backoff_factor": 0.5}, "backoff_factor"),
        ({"backoff_max": "30s", "backoff_base": "1m"}, "backoff_max"),
        ({"freeze_for": "0s"}, "freeze_for"),
    ],
)
def test_policy_rejects_unsafe_settings(
    kwargs: dict[str, object], message: str
) -> None:
    settings: dict[str, object] = {"daily_cap": 10, "min_interval": "1m", **kwargs}
    with pytest.raises(ValueError, match=message):
        HostPolicy(**settings)  # type: ignore[arg-type]


def test_policy_is_immutable() -> None:
    policy = HostPolicy(daily_cap=10, min_interval="1m")
    with pytest.raises(AttributeError):
        policy.daily_cap = 20  # type: ignore[misc]


# --- the growing pause ------------------------------------------------------------


def test_backoff_grows_exponentially_up_to_the_cap() -> None:
    policy = HostPolicy(
        daily_cap=10,
        min_interval="0s",
        backoff_base="1m",
        backoff_factor=2,
        backoff_max="10m",
    )
    assert [policy.backoff(n) for n in range(1, 7)] == [
        timedelta(minutes=m) for m in (1, 2, 4, 8, 10, 10)
    ]


def test_backoff_survives_huge_failure_counts() -> None:
    policy = HostPolicy(daily_cap=10, min_interval="0s", backoff_max="6h")
    assert policy.backoff(10_000) == timedelta(hours=6)


@given(st.integers(min_value=1, max_value=200))
def test_backoff_never_shrinks(failures: int) -> None:
    policy = HostPolicy(
        daily_cap=10, min_interval="0s", backoff_base="7s", backoff_factor=1.7
    )
    assert policy.backoff(failures + 1) >= policy.backoff(failures)
    assert policy.backoff(failures) <= policy.backoff_max


# --- the cautious promise ---------------------------------------------------------


def test_cautious_promise_assumes_the_request_fails() -> None:
    policy = HostPolicy(daily_cap=10, min_interval="10s", backoff_base="1m")
    assert policy.cautious_until(state(failures=0), T) == T + timedelta(minutes=1)
    assert policy.cautious_until(state(failures=2), T) == T + timedelta(minutes=4)


def test_cautious_promise_is_at_least_the_minimum_interval() -> None:
    policy = HostPolicy(daily_cap=10, min_interval="2h", backoff_base="1m")
    assert policy.cautious_until(state(), T) == T + timedelta(hours=2)


# --- after a request --------------------------------------------------------------


def test_a_good_answer_promises_the_minimum_interval() -> None:
    policy = HostPolicy(daily_cap=10, min_interval="60s")
    update = policy.after(state(failures=3), T, OK, None)
    assert update.not_before == T + timedelta(seconds=60)
    assert update.reason is RefusalReason.MIN_INTERVAL
    assert update.freeze is None


def test_a_failure_promises_the_next_backoff_step() -> None:
    policy = HostPolicy(daily_cap=10, min_interval="10s", backoff_base="1m")
    update = policy.after(state(failures=2), T, TIMEOUT, None)
    assert update.not_before == T + timedelta(minutes=4)  # the third failure
    assert update.reason is RefusalReason.BACKOFF
    assert update.freeze is None


@pytest.mark.parametrize("outcome", [OK, TIMEOUT, http(503)])
def test_retry_after_is_a_minimum(outcome: Outcome) -> None:
    policy = HostPolicy(daily_cap=10, min_interval="10s", backoff_base="1m")
    far = T + timedelta(hours=5)
    assert policy.after(state(), T, outcome, far).not_before == far


def test_retry_after_shorter_than_the_pause_changes_nothing() -> None:
    policy = HostPolicy(daily_cap=10, min_interval="10s", backoff_base="1m")
    update = policy.after(state(), T, http(503), T + timedelta(seconds=5))
    assert update.not_before == T + timedelta(minutes=1)


@pytest.mark.parametrize("status", [403, 451])
def test_403_and_451_always_freeze(status: int) -> None:
    for respect in (False, True):
        policy = HostPolicy(
            daily_cap=10, min_interval="1m", respect_retry_after_429=respect
        )
        update = policy.after(state(), T, http(status), T + timedelta(seconds=30))
        assert update.freeze is not None
        assert update.freeze.status == status
        assert update.freeze.since == T
        assert update.freeze.until is None


def test_429_freezes_by_default_even_with_retry_after() -> None:
    policy = HostPolicy(daily_cap=10, min_interval="1m")
    update = policy.after(state(), T, http(429), T + timedelta(seconds=120))
    assert update.freeze is not None
    assert update.freeze.until is None
    assert update.not_before >= T + timedelta(seconds=120)


def test_freeze_expiry_is_never_shorter_than_retry_after() -> None:
    policy = HostPolicy(daily_cap=10, min_interval="1m", freeze_for="1h")
    short = policy.after(state(), T, http(429), T + timedelta(minutes=10))
    assert short.freeze is not None
    assert short.freeze.until == T + timedelta(hours=1)
    long = policy.after(state(), T, http(429), T + timedelta(hours=5))
    assert long.freeze is not None
    assert long.freeze.until == T + timedelta(hours=5)


def test_freeze_for_applies_to_403_too() -> None:
    policy = HostPolicy(daily_cap=10, min_interval="1m", freeze_for="1d")
    update = policy.after(state(), T, http(403), None)
    assert update.freeze is not None
    assert update.freeze.until == T + timedelta(days=1)


def test_option_respects_retry_after_on_the_first_429() -> None:
    policy = HostPolicy(
        daily_cap=10, min_interval="1m", backoff_base="1m", respect_retry_after_429=True
    )
    update = policy.after(state(throttled=0), T, http(429), T + timedelta(minutes=30))
    assert update.freeze is None
    assert update.not_before == T + timedelta(minutes=30)
    assert update.reason is RefusalReason.BACKOFF


def test_option_uses_the_backoff_when_it_is_longer_than_retry_after() -> None:
    policy = HostPolicy(
        daily_cap=10, min_interval="1m", backoff_base="1h", respect_retry_after_429=True
    )
    update = policy.after(state(throttled=0), T, http(429), T + timedelta(minutes=5))
    assert update.freeze is None
    assert update.not_before == T + timedelta(hours=1)


def test_option_freezes_on_the_second_429() -> None:
    policy = HostPolicy(daily_cap=10, min_interval="1m", respect_retry_after_429=True)
    update = policy.after(
        state(failures=1, throttled=1), T, http(429), T + timedelta(minutes=5)
    )
    assert update.freeze is not None
    assert update.freeze.status == 429


def test_option_freezes_a_429_without_retry_after() -> None:
    policy = HostPolicy(daily_cap=10, min_interval="1m", respect_retry_after_429=True)
    update = policy.after(state(), T, http(429), None)
    assert update.freeze is not None


@pytest.mark.parametrize("status", [400, 404, 500, 502, 503])
def test_other_errors_back_off_without_freezing(status: int) -> None:
    policy = HostPolicy(daily_cap=10, min_interval="1m")
    update = policy.after(state(), T, http(status), None)
    assert update.freeze is None
    assert update.reason is RefusalReason.BACKOFF


# --- byte budget (T4) --------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0),
        (1500, 1500),
        ("500B", 500),
        ("2kB", 2000),
        ("2KiB", 2048),
        ("50MB", 50_000_000),
        ("50MiB", 50 * 1024**2),
        ("1GB", 10**9),
        ("1GiB", 1024**3),
        (" 3MB ", 3_000_000),
    ],
)
def test_parse_size(value: int | str, expected: int) -> None:
    assert parse_size(value) == expected


@pytest.mark.parametrize(
    "value", ["", "50", "50 MB", "1.5MB", "-1MB", "5mb", "5Kb", -1]
)
def test_parse_size_rejects_ambiguous_values(value: int | str) -> None:
    with pytest.raises(ValueError, match="size"):
        parse_size(value)


def test_policy_byte_budget() -> None:
    assert HostPolicy(daily_cap=10, min_interval="1m").byte_budget is None
    assert HostPolicy(
        daily_cap=10, min_interval="1m", byte_budget="5MB"
    ).byte_budget == (5_000_000)
