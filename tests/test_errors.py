"""The error hierarchy: who catches what, and what each error carries."""

import pickle
from datetime import UTC, datetime, timedelta, timezone

import pytest

from politefetch import errors
from politefetch.errors import (
    ContentError,
    PoliteFetchError,
    RefusalReason,
    Refused,
    SourceNotReady,
    StaleData,
    StuckSource,
    UnexpectedContent,
)

T = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def sample_errors() -> list[PoliteFetchError]:
    return [
        Refused("example.org", RefusalReason.DAILY_CAP, T),
        Refused("example.org", RefusalReason.FROZEN, None),
        SourceNotReady("body shorter than Content-Length", url="https://e.org/a"),
        UnexpectedContent("HTML where JSON was expected"),
        StaleData(data_time=T, max_age=timedelta(hours=3), age=timedelta(hours=5)),
        StuckSource(data_time=T, reads=4, url="https://e.org/a"),
    ]


def test_every_error_is_a_politefetch_error() -> None:
    for exc in sample_errors():
        assert isinstance(exc, PoliteFetchError)
        assert isinstance(exc, Exception)


def test_refused_is_not_a_content_error() -> None:
    # Refused means no request was sent; a ContentError means one was.
    assert not issubclass(Refused, ContentError)
    for cls in (SourceNotReady, UnexpectedContent, StaleData, StuckSource):
        assert issubclass(cls, ContentError)


def test_public_names_cover_the_hierarchy() -> None:
    exported = {
        name
        for name in errors.__all__
        if isinstance(getattr(errors, name), type)
        and issubclass(getattr(errors, name), PoliteFetchError)
    }
    assert exported == {
        "PoliteFetchError",
        "Refused",
        "ContentError",
        "SourceNotReady",
        "UnexpectedContent",
        "StaleData",
        "StuckSource",
    }


def test_refused_carries_reason_and_retry_after() -> None:
    exc = Refused("example.org", RefusalReason.MIN_INTERVAL, T)
    assert exc.host == "example.org"
    assert exc.reason is RefusalReason.MIN_INTERVAL
    assert exc.retry_after == T
    assert "example.org" in str(exc)
    assert "min_interval" in str(exc)
    assert T.isoformat() in str(exc)


def test_frozen_without_expiry_says_it_needs_a_person() -> None:
    exc = Refused("example.org", RefusalReason.FROZEN, None)
    assert exc.retry_after is None
    assert "manual" in str(exc)


@pytest.mark.parametrize(
    "reason", [r for r in RefusalReason if r is not RefusalReason.FROZEN]
)
def test_only_a_freeze_may_lack_a_retry_time(reason: RefusalReason) -> None:
    with pytest.raises(ValueError, match="retry_after"):
        Refused("example.org", reason, None)


def test_frozen_may_have_an_expiry() -> None:
    assert Refused("example.org", RefusalReason.FROZEN, T).retry_after == T


def test_refusal_reasons_are_stable_strings() -> None:
    # These strings end up in the store and in JSON status: renaming one is a
    # breaking change.
    assert {r.value for r in RefusalReason} == {
        "min_interval",
        "backoff",
        "frozen",
        "daily_cap",
        "byte_budget",
    }


def test_retry_after_must_be_utc() -> None:
    with pytest.raises(ValueError, match="timezone"):
        Refused("example.org", RefusalReason.BACKOFF, datetime(2026, 10, 16))  # noqa: DTZ001
    rome = timezone(timedelta(hours=2))
    with pytest.raises(ValueError, match="UTC"):
        Refused(
            "example.org", RefusalReason.BACKOFF, datetime(2026, 10, 16, tzinfo=rome)
        )


def test_stale_data_carries_times() -> None:
    exc = StaleData(data_time=T, max_age=timedelta(hours=3), age=timedelta(hours=5))
    assert exc.data_time == T
    assert exc.max_age == timedelta(hours=3)
    assert exc.age == timedelta(hours=5)
    assert exc.url is None
    assert "max_age" in str(exc)


def test_stale_data_time_must_be_utc() -> None:
    with pytest.raises(ValueError, match="timezone"):
        StaleData(
            data_time=datetime(2026, 10, 16),  # noqa: DTZ001
            max_age=timedelta(hours=3),
            age=timedelta(hours=5),
        )


def test_stuck_source_carries_reads() -> None:
    exc = StuckSource(data_time=T, reads=4)
    assert exc.data_time == T
    assert exc.reads == 4
    assert "4" in str(exc)


def test_stuck_source_needs_at_least_two_reads() -> None:
    # One read cannot show that time stopped advancing.
    with pytest.raises(ValueError, match="reads"):
        StuckSource(data_time=T, reads=1)


def test_content_errors_carry_url_and_detail() -> None:
    exc = SourceNotReady("empty body", url="https://e.org/a")
    assert exc.detail == "empty body"
    assert exc.url == "https://e.org/a"
    assert "empty body" in str(exc)
    assert "https://e.org/a" in str(exc)


@pytest.mark.parametrize("exc", sample_errors(), ids=lambda e: type(e).__name__)
def test_errors_survive_pickling(exc: PoliteFetchError) -> None:
    # Errors cross process boundaries (multiprocessing, worker pools).
    copy = pickle.loads(pickle.dumps(exc))
    assert type(copy) is type(exc)
    assert str(copy) == str(exc)
    assert vars(copy) == vars(exc)
