"""The body as the source meant it: Content-Encoding undone in one place, with
the standard library, for every client.

Transports hand back the body as transferred; this module decodes gzip and
deflate (the encodings the fetchers ask for in `Accept-Encoding`) and checks
that a compressed stream reaches its end. A client that decodes on its own
may hand back a stream cut short as if it were whole.
"""

import zlib

from politefetch.errors import BodyTooLarge, SourceNotReady, UnexpectedContent

__all__ = ["ACCEPT_ENCODING", "decode_body"]

ACCEPT_ENCODING = "gzip, deflate"
"""What the fetchers ask for: the encodings `decode_body` can undo."""

_GZIP = 16 + zlib.MAX_WBITS


def decode_body(
    body: bytes,
    encoding: str | None,
    *,
    max_bytes: int | None = None,
    url: str | None = None,
) -> bytes:
    """Undo `encoding` (the Content-Encoding header) on `body`.

    Raises `SourceNotReady` for a compressed stream that stops before its
    end, `UnexpectedContent` for one that is not valid or an encoding it
    cannot decode, and `BodyTooLarge` when the decoded body is longer than
    `max_bytes` (it stops decoding there).
    """
    name = (encoding or "").strip().lower()
    if not body or name in ("", "identity"):
        data = body
    elif name in ("gzip", "x-gzip"):
        data = _inflate(body, _GZIP, name, max_bytes, url)
    elif name == "deflate":
        wbits = zlib.MAX_WBITS if _zlib_header(body) else -zlib.MAX_WBITS
        data = _inflate(body, wbits, name, max_bytes, url)
    else:
        raise UnexpectedContent(
            f"content encoding {encoding!r} cannot be decoded", url=url
        )
    if max_bytes is not None and len(data) > max_bytes:
        raise _too_large(max_bytes, url)
    return data


def _zlib_header(body: bytes) -> bool:
    """Whether `body` starts with a zlib header. "deflate" should mean zlib,
    but some servers send a raw deflate stream."""
    return len(body) >= 2 and body[0] & 0x0F == 8 and (body[0] << 8 | body[1]) % 31 == 0


def _too_large(max_bytes: int, url: str | None) -> BodyTooLarge:
    return BodyTooLarge(f"decoded body larger than max_bytes {max_bytes}", url=url)


def _inflate(
    body: bytes, wbits: int, name: str, max_bytes: int | None, url: str | None
) -> bytes:
    out: list[bytes] = []
    size = 0
    rest = body
    while True:  # gzip may come in several members, one after the other
        inflater = zlib.decompressobj(wbits)
        room = 0 if max_bytes is None else max_bytes - size + 1  # 0: no limit
        try:
            piece = inflater.decompress(rest, room)
        except zlib.error as error:
            raise UnexpectedContent(
                f"body is not valid {name}: {error}", url=url
            ) from None
        size += len(piece)
        out.append(piece)
        if max_bytes is not None and size > max_bytes:
            raise _too_large(max_bytes, url)
        if not inflater.eof:
            raise SourceNotReady(
                f"compressed body ({name}) stops before its end", url=url
            )
        rest = inflater.unused_data
        if wbits != _GZIP or not rest.strip(b"\0"):
            return b"".join(out)
