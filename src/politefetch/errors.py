"""The exceptions politefetch raises.

    PoliteFetchError
    ├── Refused              the gate said no: no request was sent
    └── ContentError         a request was sent, but its response is unusable
        ├── SourceNotReady   truncated or empty body; transient
        ├── UnexpectedContent  HTML where data was expected, foreign redirect
        ├── StaleData        the data is older than max_age
        └── StuckSource      the data time stopped advancing

Every error keeps its constructor arguments in `args`, so it survives
pickling across process boundaries.
"""

from datetime import datetime, timedelta
from enum import StrEnum

from politefetch.clock import as_utc

__all__ = [
    "ContentError",
    "PoliteFetchError",
    "RefusalReason",
    "Refused",
    "SourceNotReady",
    "StaleData",
    "StuckSource",
    "UnexpectedContent",
]


class PoliteFetchError(Exception):
    """Base class for every error raised by politefetch."""


class RefusalReason(StrEnum):
    """Why the gate refused a request.

    The values are stored and serialised: renaming one is a breaking change.
    """

    MIN_INTERVAL = "min_interval"
    """The minimum interval since the previous request has not elapsed."""
    BACKOFF = "backoff"
    """The pause after a failure has not elapsed."""
    FROZEN = "frozen"
    """The host answered 403, 429 or 451 and waits for a person or an expiry."""
    DAILY_CAP = "daily_cap"
    """Today's request cap for the host is used up."""
    BYTE_BUDGET = "byte_budget"
    """Today's byte budget for the host is used up."""


class Refused(PoliteFetchError):
    """The gate refused the request; nothing was sent to the host.

    `retry_after` is the earliest time at which asking again may succeed. It
    is `None` only for a freeze without expiry, which lasts until a person
    unlocks the host.
    """

    host: str
    reason: RefusalReason
    retry_after: datetime | None

    def __init__(
        self, host: str, reason: RefusalReason, retry_after: datetime | None
    ) -> None:
        reason = RefusalReason(reason)
        if retry_after is None:
            if reason is not RefusalReason.FROZEN:
                raise ValueError(f"retry_after is required for reason {reason}")
        else:
            retry_after = as_utc(retry_after, "retry_after")
        super().__init__(host, reason, retry_after)
        self.host = host
        self.reason = reason
        self.retry_after = retry_after

    def __str__(self) -> str:
        if self.retry_after is None:
            when = "until manually unlocked"
        else:
            when = f"retry after {self.retry_after.isoformat()}"
        return f"{self.host}: refused ({self.reason.value}), {when}"


class ContentError(PoliteFetchError):
    """A request was sent and answered, but the answer cannot be used."""

    url: str | None

    def _where(self) -> str:
        return f" [{self.url}]" if self.url else ""


class _DetailedContentError(ContentError):
    detail: str

    def __init__(self, detail: str, *, url: str | None = None) -> None:
        super().__init__(detail, url)
        self.detail = detail
        self.url = url

    def __reduce__(self) -> tuple[object, ...]:
        return (_rebuild, (type(self), {"detail": self.detail, "url": self.url}))

    def __str__(self) -> str:
        return f"{self.detail}{self._where()}"


class SourceNotReady(_DetailedContentError):
    """The body is truncated or empty where data was required.

    Transient: asking again later, when the gate allows it, may succeed.
    """


class UnexpectedContent(_DetailedContentError):
    """The body is not what was asked for: HTML instead of JSON or text, or a
    redirect to another host."""


class StaleData(ContentError):
    """The time extracted from the body is older than `max_age`."""

    data_time: datetime
    max_age: timedelta
    age: timedelta

    def __init__(
        self,
        *,
        data_time: datetime,
        max_age: timedelta,
        age: timedelta,
        url: str | None = None,
    ) -> None:
        data_time = as_utc(data_time, "data_time")
        super().__init__(data_time, max_age, age, url)
        self.data_time = data_time
        self.max_age = max_age
        self.age = age
        self.url = url

    def __reduce__(self) -> tuple[object, ...]:
        fields = {
            "data_time": self.data_time,
            "max_age": self.max_age,
            "age": self.age,
            "url": self.url,
        }
        return (_rebuild, (type(self), fields))

    def __str__(self) -> str:
        return (
            f"data from {self.data_time.isoformat()} is {self.age} old, "
            f"over max_age {self.max_age}{self._where()}"
        )


class StuckSource(ContentError):
    """The data time has not advanced for `reads` consecutive reads, even
    though every request succeeded."""

    data_time: datetime
    reads: int

    def __init__(
        self, *, data_time: datetime, reads: int, url: str | None = None
    ) -> None:
        if reads < 2:
            raise ValueError(f"reads must be at least 2, got {reads}")
        data_time = as_utc(data_time, "data_time")
        super().__init__(data_time, reads, url)
        self.data_time = data_time
        self.reads = reads
        self.url = url

    def __reduce__(self) -> tuple[object, ...]:
        fields = {"data_time": self.data_time, "reads": self.reads, "url": self.url}
        return (_rebuild, (type(self), fields))

    def __str__(self) -> str:
        return (
            f"data time stuck at {self.data_time.isoformat()} "
            f"for {self.reads} reads{self._where()}"
        )


def _rebuild(
    cls: type[PoliteFetchError], fields: dict[str, object]
) -> PoliteFetchError:
    return cls(**fields)
