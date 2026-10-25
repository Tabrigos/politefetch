"""Content checks: pure functions from what arrived to an error, or nothing."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from politefetch.checks import (
    Expect,
    check_advancing,
    check_body,
    check_fresh,
    check_length,
    check_not_ahead,
    check_redirect,
    read_data_time,
)
from politefetch.errors import SourceNotReady, StaleData, StuckSource, UnexpectedContent
from politefetch.store import DataTimeRecord

T = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
URL = "https://example.org/data.json"


# --- truncated: Content-Length --------------------------------------------------------


def test_a_body_shorter_than_content_length_is_not_ready() -> None:
    with pytest.raises(SourceNotReady, match="80 of 100"):
        check_length(content_length=100, received=80, url=URL)


@pytest.mark.parametrize(("declared", "received"), [(100, 100), (None, 5), (0, 0)])
def test_a_complete_body_passes(declared: int | None, received: int) -> None:
    check_length(content_length=declared, received=received)


def test_a_body_longer_than_content_length_is_unexpected() -> None:
    with pytest.raises(UnexpectedContent, match="longer"):
        check_length(content_length=10, received=11)


# --- truncated: empty body ------------------------------------------------------------


@pytest.mark.parametrize(
    ("expect", "body"),
    [
        ("json", b""),
        ("json", b"  \n\t"),
        ("text", b""),
        ("text", b" \n"),
        ("bytes", b""),
    ],
)
def test_an_empty_body_is_not_ready(expect: Expect, body: bytes) -> None:
    with pytest.raises(SourceNotReady, match="empty"):
        check_body(body, expect=expect)


def test_an_empty_body_may_be_allowed() -> None:
    assert check_body(b"", expect="text", allow_empty=True) == ""


# --- truncated: JSON ending mid-way ---------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        b'{"time": "2026-10-17T12:00:00Z", "values": [1, 2',
        b'{"time": "2026-',
        b'[{"a": 1}, {"b": tr',
        b'{"a": {"b": [1, 2, 3]}',
        b'{"text": "a \\" quote',
        b'{"a": "x\\"}',  # the brace is inside the string: still open
        b'["a\\\\", "b',  # an escaped backslash, then an open string
        b'"a top-level string',
        b"[",
    ],
)
def test_json_ending_mid_way_is_not_ready(body: bytes) -> None:
    with pytest.raises(SourceNotReady, match="JSON ends"):
        check_body(body, expect="json")


@pytest.mark.parametrize(
    "body", [b"{]", b'{"a" 1}', b"[1, 2,, 3]", b"nope", b"{'a': 1}"]
)
def test_broken_json_that_is_complete_is_unexpected(body: bytes) -> None:
    with pytest.raises(UnexpectedContent, match="not valid JSON"):
        check_body(body, expect="json")


def test_valid_json_comes_back_parsed() -> None:
    assert check_body(b'{"a": [1, 2]}', expect="json") == {"a": [1, 2]}


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=8),
    lambda children: (
        st.lists(children, max_size=4)
        | st.dictionaries(st.text(max_size=6), children, max_size=4)
    ),
    max_leaves=12,
)
json_documents = st.one_of(
    st.lists(json_values, max_size=4),
    st.dictionaries(st.text(max_size=6), json_values),
    st.text(alphabet=st.sampled_from('ab"\\{}[] '), max_size=8),  # brackets in strings
)


@given(json_documents, st.data())
def test_every_cut_of_a_json_document_is_not_ready(
    document: object, data: st.DataObject
) -> None:
    text = json.dumps(document, ensure_ascii=False).encode()
    cut = data.draw(st.integers(min_value=1, max_value=len(text) - 1))
    prefix = text[:cut]
    try:
        prefix.decode()
    except UnicodeDecodeError:
        return  # cut inside a multi-byte character: the text check's business
    with pytest.raises(SourceNotReady):
        check_body(prefix, expect="json")


@given(json_documents)
def test_every_whole_json_document_passes(document: object) -> None:
    text = json.dumps(document, ensure_ascii=False).encode()
    assert check_body(text, expect="json") == document


# --- HTML where data was expected -----------------------------------------------------


@pytest.mark.parametrize(
    "content_type", ["text/html", "text/html; charset=utf-8", "application/xhtml+xml"]
)
@pytest.mark.parametrize("expect", ["json", "text"])
def test_html_content_type_is_unexpected(content_type: str, expect: Expect) -> None:
    with pytest.raises(UnexpectedContent, match="HTML"):
        check_body(b'{"a": 1}', expect=expect, content_type=content_type)


@pytest.mark.parametrize(
    "body",
    [
        b"<!DOCTYPE html><html><body>Maintenance</body></html>",
        b"\xef\xbb\xbf  \n<!doctype HTML>",
        b"<html lang='en'>",
    ],
)
@pytest.mark.parametrize("expect", ["json", "text"])
def test_html_body_is_unexpected_whatever_the_content_type(
    body: bytes, expect: Expect
) -> None:
    with pytest.raises(UnexpectedContent, match="HTML"):
        check_body(body, expect=expect, content_type="application/json")


@pytest.mark.parametrize(
    "body",
    [
        b"<!-- generated by the proxy -->\n<html><body>502</body></html>",
        b"<head><title>Error</title></head>",
        b"<BODY>Service Unavailable</BODY>",
        b"\xef\xbb\xbf\r\n<!-- a -->  <!-- b -->\t<!DOCTYPE html>",
        b"<?xml version='1.0'?>\n<html xmlns='http://www.w3.org/1999/xhtml'>",
    ],
    ids=["after a comment", "head", "body", "bom, spaces, comments", "xhtml"],
)
@pytest.mark.parametrize("expect", ["json", "text"])
def test_html_after_what_comes_before_it_is_unexpected(
    body: bytes, expect: Expect
) -> None:
    with pytest.raises(UnexpectedContent, match="HTML"):
        check_body(body, expect=expect, content_type="text/plain")


@pytest.mark.parametrize(
    "body",
    [
        b"<!-- a comment, then data -->\ntime,value\n",
        b"<?xml version='1.0'?>\n<feed><entry/></feed>",
        b"<data>1</data>",
        b"<header>news</header>",
        b"value: <html> is a tag name\n",
        b"<!-- " + b"x" * 2000 + b" -->\n<html>",
    ],
    ids=[
        "comment then csv",
        "xml",
        "a tag",
        "a tag that starts like head",
        "html later on",
        "past the first kB",
    ],
)
def test_text_that_is_not_an_html_page_passes(body: bytes) -> None:
    assert check_body(body, expect="text") == body.decode()


def test_bytes_are_not_inspected_for_html() -> None:
    assert check_body(b"<html>", expect="bytes") == b"<html>"


def test_plain_text_passes_and_is_decoded() -> None:
    assert check_body(b"caf\xc3\xa9\n", expect="text") == "café\n"
    latin = b"caf\xe9"
    assert (
        check_body(latin, expect="text", content_type="text/plain; charset=latin-1")
        == "café"
    )


def test_text_that_does_not_decode_is_unexpected() -> None:
    with pytest.raises(UnexpectedContent, match="utf-8"):
        check_body(b"caf\xe9", expect="text")


def test_json_served_as_text_plain_is_fine() -> None:
    assert check_body(b"[1]", expect="json", content_type="text/plain") == [1]


# --- redirect to another host ---------------------------------------------------------


@pytest.mark.parametrize(
    "final",
    [
        URL,
        "https://example.org/moved.json",
        "http://example.org/data.json",
        "https://EXAMPLE.org:443/data.json",
        None,
    ],
)
def test_same_host_passes(final: str | None) -> None:
    check_redirect(URL, final)


@pytest.mark.parametrize(
    "final",
    [
        "https://login.example.com/sso",
        "https://cdn.example.org/data.json",
        "https://evil.test/",
    ],
)
def test_another_host_is_unexpected(final: str) -> None:
    with pytest.raises(UnexpectedContent, match="redirected"):
        check_redirect(URL, final)


# --- data time and freshness ----------------------------------------------------------


def test_read_data_time() -> None:
    assert (
        read_data_time(
            {"time": T.isoformat()}, lambda d: datetime.fromisoformat(d["time"])
        )
        == T
    )


def test_a_missing_data_time_is_unexpected_content() -> None:
    with pytest.raises(UnexpectedContent, match="data time"):
        read_data_time({}, lambda d: datetime.fromisoformat(d["time"]), url=URL)


def test_a_naive_data_time_is_a_bug_of_the_extractor() -> None:
    with pytest.raises(ValueError, match="timezone"):
        read_data_time({}, lambda _d: datetime(2026, 10, 17))  # noqa: DTZ001


def test_fresh_data_passes() -> None:
    check_fresh(T - timedelta(hours=3), now=T, max_age=timedelta(hours=3))
    check_fresh(
        T + timedelta(minutes=5), now=T, max_age=timedelta(hours=3)
    )  # forecasts


def test_a_data_time_a_little_ahead_passes() -> None:
    check_not_ahead(T + timedelta(minutes=15), now=T, max_ahead=timedelta(minutes=15))


def test_a_data_time_far_ahead_is_unexpected() -> None:
    with pytest.raises(UnexpectedContent, match="ahead"):
        check_not_ahead(
            T + timedelta(minutes=16), now=T, max_ahead=timedelta(minutes=15), url=URL
        )


def test_old_data_is_stale() -> None:
    with pytest.raises(StaleData) as stale:
        check_fresh(T - timedelta(hours=5), now=T, max_age=timedelta(hours=3), url=URL)
    assert stale.value.age == timedelta(hours=5)
    assert stale.value.max_age == timedelta(hours=3)
    assert stale.value.url == URL


# --- stuck source ---------------------------------------------------------------------


def record(reads: int, since: datetime = T) -> DataTimeRecord:
    return DataTimeRecord(url=URL, latest=T, reads=reads, updated_at=T, since=since)


def test_a_source_is_stuck_after_n_reads_of_the_same_time() -> None:
    check_advancing(record(2), stuck_after=3)
    with pytest.raises(StuckSource) as stuck:
        check_advancing(record(3), stuck_after=3)
    assert stuck.value.reads == 3
    assert stuck.value.data_time == T


def test_stuck_after_needs_at_least_two_reads() -> None:
    with pytest.raises(ValueError, match="stuck_after"):
        check_advancing(record(1), stuck_after=1)


def test_a_source_is_stuck_after_a_time_without_progress() -> None:
    since = T - timedelta(hours=6)
    check_advancing(record(2, since), stuck_after=timedelta(hours=6, seconds=1), now=T)
    with pytest.raises(StuckSource) as stuck:
        check_advancing(record(2, since), stuck_after=timedelta(hours=6), now=T)
    assert (stuck.value.since, stuck.value.reads) == (since, 2)


def test_a_single_read_is_never_stuck() -> None:
    check_advancing(
        record(1, T - timedelta(days=3)), stuck_after=timedelta(hours=1), now=T
    )


def test_a_time_to_be_stuck_needs_now_and_a_length() -> None:
    with pytest.raises(ValueError, match="now"):
        check_advancing(record(2), stuck_after=timedelta(hours=1))
    with pytest.raises(ValueError, match="stuck_after"):
        check_advancing(record(2), stuck_after=timedelta(0), now=T)


# --- every content error has a shape --------------------------------------------------


def test_content_errors_name_their_shape() -> None:
    assert SourceNotReady("x").shape == "not_ready"
    assert UnexpectedContent("x").shape == "unexpected_content"
    stale = StaleData(data_time=T, max_age=timedelta(1), age=timedelta(2))
    assert stale.shape == "stale_data"
    assert StuckSource(data_time=T, reads=2).shape == "stuck_source"
