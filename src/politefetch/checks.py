"""Content checks, as pure functions: truncated or empty bodies, HTML where
data was expected, redirects to another host, freshness measured on the data
itself, and sources whose data time stopped advancing.

Each check returns quietly or raises a `ContentError`:

- `check_length`: body shorter than its Content-Length -> `SourceNotReady`;
- `check_body`: empty body or JSON ending mid-way -> `SourceNotReady`; HTML
  where JSON or text was expected, invalid JSON, undecodable text ->
  `UnexpectedContent`; otherwise the parsed content;
- `check_redirect`: final URL on another host -> `UnexpectedContent`;
- `read_data_time`: the caller's extractor, with its failures turned into
  `UnexpectedContent`;
- `check_fresh`: data older than `max_age` -> `StaleData`;
- `check_advancing`: data time stuck for N reads -> `StuckSource`, from the
  history the store keeps (`Store.note_data_time`).

A 304 or a recent Last-Modified says nothing about freshness: only the time
read from the data does (SPEC.md).
"""

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from email.message import Message
from typing import Any, Literal
from urllib.parse import urlsplit

from politefetch.clock import as_utc
from politefetch.errors import SourceNotReady, StaleData, StuckSource, UnexpectedContent
from politefetch.store.base import DataTimeRecord

__all__ = [
    "Expect",
    "check_advancing",
    "check_body",
    "check_fresh",
    "check_length",
    "check_redirect",
    "read_data_time",
]

Expect = Literal["json", "text", "bytes"]

_HTML_TYPES = ("text/html", "application/xhtml+xml")
_HTML_STARTS = (b"<!doctype html", b"<html")
_BOM = b"\xef\xbb\xbf"


def check_length(
    *, content_length: int | None, received: int, url: str | None = None
) -> None:
    """Compare the bytes received on the wire with the declared Content-Length."""
    if content_length is None:
        return
    if received < content_length:
        raise SourceNotReady(
            f"body shorter than Content-Length: {received} of {content_length} bytes",
            url=url,
        )
    if received > content_length:
        raise UnexpectedContent(
            f"body longer than Content-Length: {received} of {content_length} bytes",
            url=url,
        )


def _charset(content_type: str | None) -> str:
    if not content_type:
        return "utf-8"
    message = Message()
    message["content-type"] = content_type
    return message.get_content_charset() or "utf-8"


def _looks_like_html(body: bytes, content_type: str | None) -> bool:
    if content_type and content_type.split(";")[0].strip().lower() in _HTML_TYPES:
        return True
    start = body.removeprefix(_BOM).lstrip()[:16].lower()
    return start.startswith(_HTML_STARTS)


def _open_at_end(text: str) -> bool:
    """Tell whether a JSON text ends inside a string or an open container."""
    depth = 0
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
    return in_string or depth > 0


def check_body(
    body: bytes,
    *,
    expect: Expect = "json",
    content_type: str | None = None,
    allow_empty: bool = False,
    url: str | None = None,
) -> Any:
    """Check a body against what was expected and return its content: the
    parsed JSON, the decoded text, or the bytes themselves."""
    blank = not body.strip() if expect != "bytes" else not body
    if blank:
        if not allow_empty:
            raise SourceNotReady("empty body where data was expected", url=url)
        return body if expect == "bytes" else ""
    if expect == "bytes":
        return body
    if _looks_like_html(body, content_type):
        raise UnexpectedContent(f"HTML where {expect} was expected", url=url)
    charset = _charset(content_type)
    try:
        text = body.removeprefix(_BOM).decode(charset)
    except (UnicodeDecodeError, LookupError):
        if expect == "json" and _open_at_end(body.decode("utf-8", errors="replace")):
            raise SourceNotReady("JSON ends mid-way", url=url) from None
        raise UnexpectedContent(f"text that is not valid {charset}", url=url) from None
    if expect == "text":
        return text
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        if _open_at_end(text):
            raise SourceNotReady("JSON ends mid-way", url=url) from error
        raise UnexpectedContent(f"not valid JSON: {error.msg}", url=url) from error


def check_redirect(
    requested_url: str, final_url: str | None, *, url: str | None = None
) -> None:
    """A redirect may change scheme, port or path, not host."""
    if final_url is None:
        return
    asked = (urlsplit(requested_url).hostname or "").lower()
    landed = (urlsplit(final_url).hostname or "").lower()
    if asked != landed:
        raise UnexpectedContent(
            f"redirected from {asked or requested_url!r} to another host,"
            f" {landed or final_url!r}",
            url=url or requested_url,
        )


def read_data_time(
    content: Any, extractor: Callable[[Any], datetime], *, url: str | None = None
) -> datetime:
    """Run the caller's extractor on the content and return the data time.

    If the extractor cannot find it (missing key, unparsable text...) the
    content is not what was expected. A naive datetime is a bug of the
    extractor and raises `ValueError`.
    """
    try:
        data_time = extractor(content)
    except Exception as error:
        raise UnexpectedContent(
            f"cannot read the data time: {error!r}", url=url
        ) from error
    return as_utc(data_time, "data time")


def check_fresh(
    data_time: datetime, *, now: datetime, max_age: timedelta, url: str | None = None
) -> None:
    """Data older than `max_age` is stale. Data from the future passes: some
    sources publish forecasts."""
    age = as_utc(now, "now") - as_utc(data_time, "data_time")
    if age > max_age:
        raise StaleData(data_time=data_time, max_age=max_age, age=age, url=url)


def check_advancing(
    record: DataTimeRecord, *, stuck_after: int, url: str | None = None
) -> None:
    """A source whose data time has not advanced for `stuck_after`
    consecutive reads is stuck, even if every request succeeded."""
    if stuck_after < 2:
        raise ValueError(f"stuck_after must be at least 2, got {stuck_after}")
    if record.reads >= stuck_after:
        raise StuckSource(
            data_time=record.latest, reads=record.reads, url=url or record.url
        )
