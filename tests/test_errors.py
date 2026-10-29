"""The error hierarchy: who catches what, and what each error carries."""

import pickle
from datetime import UTC, datetime, timedelta, timezone

import pytest

from politefetch import errors
from politefetch.errors import (
    BodyTooLarge,
    ContentError,
    FetchFailed,
    NoPolicy,
    OutcomeNotSaved,
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
        FetchFailed("https://e.org/a", "read_timeout"),
        FetchFailed("https://e.org/a", "http", 503),
        SourceNotReady("body shorter than Content-Length", url="https://e.org/a"),
        UnexpectedContent("HTML where JSON was expected"),
        BodyTooLarge("body larger than max_bytes 10", url="https://e.org/a"),
        FetchFailed("https://e.org/a", "deadline", 200),
        StaleData(data_time=T, max_age=timedelta(hours=3), age=timedelta(hours=5)),
        StuckSource(data_time=T, reads=4, url="https://e.org/a"),
        OutcomeNotSaved("https://e.org/a", 7, "http", 403),
        NoPolicy("elsewhere.example"),
        FetchFailed("https://e.org/a", "http", 403, "the host is now frozen"),
    ]


def test_every_error_is_a_politefetch_error() -> None:
    for exc in sample_errors():
        assert isinstance(exc, PoliteFetchError)
        assert isinstance(exc, Exception)


def test_refused_is_not_a_content_error() -> None:
    # Refused means no request was sent; a ContentError means one was.
    assert not issubclass(Refused, ContentError)
    for cls in (
        SourceNotReady,
        UnexpectedContent,
        BodyTooLarge,
        StaleData,
        StuckSource,
    ):
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
        "FetchFailed",
        "ContentError",
        "SourceNotReady",
        "UnexpectedContent",
        "BodyTooLarge",
        "StaleData",
        "StuckSource",
        "OutcomeNotSaved",
        "NoPolicy",
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
        "in_flight",
        "min_interval",
        "backoff",
        "frozen",
        "daily_cap",
        "byte_budget",
        "rate_limit",
        "url_backoff",
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


def test_fetch_failed_says_how() -> None:
    timeout = FetchFailed("https://e.org/a", "read_timeout")
    assert (timeout.url, timeout.shape, timeout.status) == (
        "https://e.org/a",
        "read_timeout",
        None,
    )
    assert "read_timeout" in str(timeout)
    assert "HTTP 503" in str(FetchFailed("https://e.org/a", "http", 503))
    late = str(FetchFailed("https://e.org/a", "deadline", 200))
    assert "deadline after HTTP 200" in late
    assert ContentError not in type(timeout).__mro__


def test_outcome_not_saved_says_what_was_lost() -> None:
    exc = OutcomeNotSaved("https://e.org/a", 7, "http", 403)
    assert (exc.url, exc.row_id, exc.shape, exc.status) == (
        "https://e.org/a",
        7,
        "http",
        403,
    )
    assert "HTTP 403" in str(exc)
    assert "in flight" in str(exc)
    assert not isinstance(exc, (FetchFailed, ContentError, Refused))


def test_a_note_on_a_failure_survives_pickling() -> None:
    failed = FetchFailed("https://e.org/a", "http", 403, "the host is now frozen")
    copy = pickle.loads(pickle.dumps(failed))
    assert copy.note == "the host is now frozen"
    assert failed.args == ("https://e.org/a", "http", 403, "the host is now frozen")
    assert str(copy) == str(failed)
    assert str(copy).endswith(": the host is now frozen")
