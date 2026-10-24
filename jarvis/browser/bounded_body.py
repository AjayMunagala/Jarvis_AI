"""Read an HTTP response body under a hard size cap, safe against decompression bombs (v7.3 review).

``httpx`` decodes ``Content-Encoding`` itself (including stacked ``gzip, gzip``) before a caller can count bytes,
so a tiny compressed body can expand to hundreds of megabytes in memory. Callers therefore ask for
``Accept-Encoding: identity`` and read ``response.iter_raw()`` through ``read_bounded_body``:

- no encoding (or ``identity``): raw bytes, capped;
- exactly one layer of ``gzip``/``x-gzip`` or ``deflate``: decoded incrementally with ``zlib.decompressobj``,
  never producing more than the remaining budget per call (``max_length`` plus the ``unconsumed_tail`` loop);
- anything else (``br``, ``zstd``, ``compress``, unknown or stacked encodings): refused.

Both the compressed input and the decoded output are capped at ``max_bytes``.
"""

from typing import Callable, Iterable
import zlib

IDENTITY_HEADERS = {"Accept-Encoding": "identity"}


class BodyTooLarge(ValueError):
    pass


class UnsupportedEncoding(ValueError):
    pass


def _decoder(encoding_header: str | None):
    layers = [token.strip().lower() for token in (encoding_header or "").split(",")]
    layers = [token for token in layers if token and token != "identity"]
    if not layers:
        return None
    if len(layers) > 1:
        raise UnsupportedEncoding("Stacked content encodings are refused.")
    if layers[0] in ("gzip", "x-gzip"):
        return zlib.decompressobj(16 + zlib.MAX_WBITS)
    if layers[0] == "deflate":
        return "deflate"  # zlib-wrapped or raw deflate: decided on the first bytes
    raise UnsupportedEncoding(f"The content encoding '{layers[0][:20]}' is not supported.")


def read_response_body(response, max_bytes: int, check: Callable[[], None] | None = None) -> bytes:
    """``read_bounded_body`` over ``response.iter_raw()`` of an ``httpx`` streaming response.

    A response whose content was already read in memory (only test transports build those; the guarded network
    transport always streams) is capped as it is, since httpx already decoded it."""
    import httpx

    try:
        return read_bounded_body(response.iter_raw(), response.headers.get("content-encoding"), max_bytes, check)
    except httpx.StreamConsumed:
        return read_bounded_body([response.content], None, max_bytes, check)


def read_bounded_body(chunks: Iterable[bytes], content_encoding: str | None, max_bytes: int,
                      check: Callable[[], None] | None = None) -> bytes:
    """Return the decoded body, raising ``BodyTooLarge`` or ``UnsupportedEncoding``. ``check`` runs per chunk
    (for example a deadline check that raises)."""
    decoder = _decoder(content_encoding)
    out = bytearray()
    raw_total = 0
    for chunk in chunks:
        if check is not None:
            check()
        if not chunk:
            continue
        raw_total += len(chunk)
        if raw_total > max_bytes:
            raise BodyTooLarge("The response exceeds the size limit.")
        if decoder is None:
            out.extend(chunk)
            if len(out) > max_bytes:
                raise BodyTooLarge("The response exceeds the size limit.")
            continue
        if decoder == "deflate":
            decoder = zlib.decompressobj(zlib.MAX_WBITS if (chunk[0] & 0x0F) == 8 and len(chunk) > 1
                                         and ((chunk[0] << 8) | chunk[1]) % 31 == 0 else -zlib.MAX_WBITS)
        data = chunk
        try:
            while data and not decoder.eof:
                out.extend(decoder.decompress(data, max_bytes - len(out) + 1))
                if len(out) > max_bytes:
                    raise BodyTooLarge("The decoded response exceeds the size limit.")
                data = decoder.unconsumed_tail
        except zlib.error:
            raise UnsupportedEncoding("The compressed response is corrupt.") from None
        if decoder.eof:
            break  # trailing data after the compressed stream is ignored, never decoded
    if decoder is not None and not isinstance(decoder, str) and not decoder.eof:
        raise UnsupportedEncoding("The compressed response is incomplete.")
    return bytes(out)
