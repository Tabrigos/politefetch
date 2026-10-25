"""The exceptions politefetch raises.

    PoliteFetchError
    ├── Refused              the gate said no: no request was sent
    ├── FetchFailed          a request was sent: no answer, an HTTP error, or
    │                        past its deadline
    ├── OutcomeNotSaved      a request was sent: the store could not save how
    │                        it ended
    ├── NoPolicy             a host without a policy, and no default: nothing
    │                        was sent (also a ValueError)
    └── ContentError         a request was sent, but its response is unusable
        ├── SourceNotReady   truncated or empty body; transient
        ├── UnexpectedContent  HTML where data was expected, foreign redirect
        ├── BodyTooLarge     the body is longer than the fetch may read
        ├── StaleData        older than max_age: an answer all the same, in
        │                    `result`
        └── StuckSource      the data time stopped advancing: the same

Every error keeps its constructor arguments in `args`, so it survives
pickling across process boundaries.
"""

from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from politefetch.clock import as_utc

if TYPE_CHECKING:
    from politefetch.adapters.base import FetchResult

__all__ = [
    "BodyTooLarge",
    "ContentError",
    "FetchFailed",
    "NoPolicy",
    "OutcomeNotSaved",
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

    IN_FLIGHT = "in_flight"
    """Another request to the host is still open: one at a time per host."""
    MIN_INTERVAL = "min_interval"
    """The minimum interval since the previous request has not elapsed."""
    BACKOFF = "backoff"
    """The pause after a failure has not elapsed."""
    FROZEN = "frozen"
    """The host answered 403, 429 or 451, or asked with Retry-After for a pause
    longer than any of its policy's, and waits for a person or an expiry."""
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


class FetchFailed(PoliteFetchError):
    """A request was sent but no usable HTTP answer came back.

    `shape` says how it failed: a connect or read timeout, a refused or reset
    connection, TLS, DNS, another connection error, `deadline` for an answer
    still arriving when its time was up, or `http` for an error status.
    `status` is the HTTP status, when an answer had started. The gate has
    already recorded it. `note` says what the failure did to the host, such
    as a freeze and how to lift it.
    """

    url: str
    shape: str
    status: int | None
    note: str

    def __init__(
        self, url: str, shape: str, status: int | None = None, note: str = ""
    ) -> None:
        super().__init__(url, shape, status, note)
        self.url = url
        self.shape = shape
        self.status = status
        self.note = note

    def __str__(self) -> str:
        if self.status is None:
            what = self.shape
        elif self.shape == "http":
            what = f"HTTP {self.status}"
        else:
            what = f"{self.shape} after HTTP {self.status}"
        note = f": {self.note}" if self.note else ""
        return f"{self.url}: request failed ({what}){note}"


class NoPolicy(PoliteFetchError, ValueError):
    """The gate has no policy for `host` and no default one: no request was
    sent. A `ValueError` too, as it was before it had a class of its own."""

    host: str

    def __init__(self, host: str) -> None:
        super().__init__(host)
        self.host = host

    def __str__(self) -> str:
        return (
            f"no policy for host {self.host!r} and no default policy: add one to"
            " the gate's policies, or give the gate a default"
        )


class OutcomeNotSaved(PoliteFetchError):
    """A request was sent, but the store could not save how it ended, even
    after trying again.

    The row stays in flight, so the host stays blocked until the
    reconciliation closes it as failed; what the source answered (`shape`,
    `status`) is lost to the store, which is why this error carries it. The
    store's own exception is the `__cause__`; an exception raised inside
    the attempt is the `__context__`.
    """

    url: str
    row_id: int
    shape: str | None
    status: int | None

    def __init__(
        self, url: str, row_id: int, shape: str | None, status: int | None = None
    ) -> None:
        super().__init__(url, row_id, shape, status)
        self.url = url
        self.row_id = row_id
        self.shape = shape
        self.status = status

    def __str__(self) -> str:
        what = f"HTTP {self.status}" if self.status is not None else self.shape
        return (
            f"{self.url}: the outcome of the request ({what}) could not be saved;"
            " its row stays in flight and the host stays blocked until the"
            " reconciliation closes it"
        )


class ContentError(PoliteFetchError):
    """A request was sent and answered, but the answer cannot be used.

    `shape` is a short, stable name for the kind of problem, recorded as the
    failure shape of the request; except for `StaleData` and `StuckSource`,
    which come from sources that answered well and are recorded as answers.
    """

    shape = "content"
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

    shape = "not_ready"


class UnexpectedContent(_DetailedContentError):
    """The body is not what was asked for: HTML instead of JSON or text, or a
    redirect to another host."""

    shape = "unexpected_content"


class BodyTooLarge(_DetailedContentError):
    """The body is longer than the fetch may read: `max_bytes`, or the bytes
    left in the host's byte budget. Reading stopped there."""

    shape = "too_large"


class StaleData(ContentError):
    """The time extracted from the body is older than `max_age`.

    The source answered well: a fetch records it as an answer (validators
    kept, no failure counted) and puts what came back in `result`, for a
    caller that would rather use old data than none.
    """

    shape = "stale_data"

    data_time: datetime
    max_age: timedelta
    age: timedelta
    result: "FetchResult | None"

    def __init__(
        self,
        *,
        data_time: datetime,
        max_age: timedelta,
        age: timedelta,
        url: str | None = None,
        result: "FetchResult | None" = None,
    ) -> None:
        data_time = as_utc(data_time, "data_time")
        super().__init__(data_time, max_age, age, url)
        self.data_time = data_time
        self.max_age = max_age
        self.age = age
        self.url = url
        self.result = result

    def __reduce__(self) -> tuple[object, ...]:
        fields = {
            "data_time": self.data_time,
            "max_age": self.max_age,
            "age": self.age,
            "url": self.url,
            "result": self.result,
        }
        return (_rebuild, (type(self), fields))

    def __str__(self) -> str:
        return (
            f"data from {self.data_time.isoformat()} is {self.age} old, "
            f"over max_age {self.max_age}{self._where()}"
        )


class StuckSource(ContentError):
    """The data time has not advanced for `reads` consecutive reads, the
    first of them at `since`, even though every request succeeded.

    Like `StaleData`, it is recorded as an answer, and `result` holds what
    came back.
    """

    shape = "stuck_source"

    data_time: datetime
    reads: int
    since: datetime | None
    result: "FetchResult | None"

    def __init__(
        self,
        *,
        data_time: datetime,
        reads: int,
        since: datetime | None = None,
        url: str | None = None,
        result: "FetchResult | None" = None,
    ) -> None:
        if reads < 2:
            raise ValueError(f"reads must be at least 2, got {reads}")
        data_time = as_utc(data_time, "data_time")
        since = None if since is None else as_utc(since, "since")
        super().__init__(data_time, reads, since, url)
        self.data_time = data_time
        self.reads = reads
        self.since = since
        self.url = url
        self.result = result

    def __reduce__(self) -> tuple[object, ...]:
        fields = {
            "data_time": self.data_time,
            "reads": self.reads,
            "since": self.since,
            "url": self.url,
            "result": self.result,
        }
        return (_rebuild, (type(self), fields))

    def __str__(self) -> str:
        first = "" if self.since is None else f" since {self.since.isoformat()}"
        return (
            f"data time stuck at {self.data_time.isoformat()} "
            f"for {self.reads} reads{first}{self._where()}"
        )


def _rebuild(
    cls: type[PoliteFetchError], fields: dict[str, object]
) -> PoliteFetchError:
    return cls(**fields)
