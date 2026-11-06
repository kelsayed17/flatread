"""A file's text and its lines: decoded from a body held whole, or read as its chunks arrive.

:func:`decode_text` and :func:`decode_utf8` turn a body into text, :func:`lines` into its numbered non-empty lines, and
:func:`check_header` and :func:`check_line_ending` check the first and the last. :func:`stream_lines` reads the lines of
a file too large to hold as its chunks arrive, and :func:`batched` groups them for a frame read, each holding no more
than a chunk, a line or a batch. A stream's error is raised when it is met, so the lines before it have been yielded
already: a caller that needs the whole file or nothing keeps what it read only once the iteration ends without one.
"""

import codecs
from typing import TYPE_CHECKING, Final, NamedTuple

from flatread.errors import EmptyBodyError, EncodingError, ReadError, TruncatedFileError, UnknownLayoutError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

__all__ = [
    "Line",
    "batched",
    "check_header",
    "check_line_ending",
    "decode_text",
    "decode_utf8",
    "lines",
    "stream_lines",
]

_BOM: Final = codecs.BOM_UTF8


def decode_text(file: bytes) -> str:
    """Return the file's text: UTF-8 when it is UTF-8, else latin-1, which gives every byte a character.

    A byte-order mark that opens the file is not part of it.
    """
    file = file.removeprefix(_BOM)
    try:
        return file.decode()
    except UnicodeDecodeError:
        return file.decode("latin-1")


class Line(NamedTuple):
    """One non-empty line of a file: its number in the file, counting blank lines, and its text."""

    number: int
    text: str


def lines(text: str) -> list[Line]:
    """Return the file's non-empty lines.

    A line ends at an LF, and a CR just before it is dropped, as is one that is the file's last character; any other CR
    is part of its line, so a file whose lines end with CR alone reads as one line. An empty line is framing, wherever
    it falls. A file with nothing in it, or only blank lines, raises :class:`EmptyBodyError`.
    """
    if not text:
        msg = "the file is empty"
        raise EmptyBodyError(msg)
    found = [
        Line(number, line.removesuffix("\r"))
        for number, line in enumerate(text.split("\n"), start=1)
        if line.removesuffix("\r")
    ]
    if not found:
        msg = "the file holds only blank lines"
        raise EmptyBodyError(msg)
    return found


def decode_utf8(file: bytes) -> str:
    """Return the file's text, for a wire that is UTF-8 throughout; one that is not raises :class:`EncodingError`.

    :func:`decode_text` is the lenient twin, for a wire that has been seen in another encoding. A byte-order mark that
    opens the file is not part of it.
    """
    try:
        return file.removeprefix(_BOM).decode()
    except UnicodeDecodeError as exc:
        msg = f"the file is not UTF-8: {exc}"
        raise EncodingError(msg) from exc


def check_line_ending(text: str, last: Line) -> None:
    """Check that the file ends with a line ending, for a wire whose every file does; *last* is its last line.

    A download cut mid-row loses it, and a line reads the same whether it is ended or not, so the text is what can tell.
    """
    if not text.endswith("\n"):
        msg = f"the file ends mid-line, with {last.text!r} on line {last.number}, so it may have been cut off"
        raise TruncatedFileError(msg)


def check_header(first: Line, header: str) -> None:
    """Check that *first*, a file's first line, is *header* exactly; any other line raises :class:`UnknownLayoutError`.

    Renamed, reordered, added and dropped columns all fail it, and so does a body that is not the wire at all.
    """
    if first.text != header:
        msg = f"line {first.number} is not the header {header!r}: {first.text[:200]!r}"
        raise UnknownLayoutError(msg)


async def stream_lines(chunks: AsyncIterator[bytes]) -> AsyncIterator[Line]:
    """Yield a UTF-8 file's non-empty lines as its chunks arrive, for a wire whose every file ends with a line ending.

    :func:`lines` read incrementally, with :func:`decode_utf8`'s and :func:`check_line_ending`'s checks: a line ends at
    an LF, a CR just before it is dropped, an empty line is framing, and a byte-order mark that opens the file is not
    part of it. A byte that is not UTF-8 raises :class:`EncodingError` where it falls; a file with nothing in it, or
    only blank lines, raises :class:`EmptyBodyError`; and a file that ends mid-line raises
    :class:`TruncatedFileError` instead of yielding that line. Only the line being read is held.
    """
    decoder = codecs.getincrementaldecoder("utf-8")()
    number, rest, opened, last = 0, "", False, None
    async for chunk in chunks:
        text = _decode_piece(decoder, chunk)
        if text and not opened:
            text, opened = text.removeprefix(_BOM.decode()), True
        *ended, rest = (rest + text).split("\n")
        for written in ended:
            number += 1
            if line := written.removesuffix("\r"):
                last = Line(number, line)
                yield last
    rest += _decode_piece(decoder, b"", final=True)
    if not number and not rest:
        msg = "the file is empty"
        raise EmptyBodyError(msg)
    if unended := rest.removesuffix("\r"):
        last = Line(number + 1, unended)
    if last is None:
        msg = "the file holds only blank lines"
        raise EmptyBodyError(msg)
    if rest:
        check_line_ending(rest, last)


def _decode_piece(decoder: codecs.IncrementalDecoder, piece: bytes, *, final: bool = False) -> str:
    """Decode the next *piece* of a UTF-8 file, and with *final* whatever the decoder still holds.

    A byte that is not UTF-8 raises :class:`EncodingError`.
    """
    try:
        return decoder.decode(piece, final=final)
    except UnicodeDecodeError as exc:
        msg = f"the file is not UTF-8: {exc}"
        raise EncodingError(msg) from exc


async def batched[T](items: AsyncIterator[T], size: int) -> AsyncIterator[list[T]]:
    """Yield *items* in lists of *size*, the last one shorter when they run out: a frame decoder's batches.

    When *items* raises a :class:`~flatread.errors.ReadError`, the items read before it are yielded first, as a last
    batch, and the error is raised on the next request. So a decoder that checks each batch meets an error inside the
    batch before the one that ended the stream, as a decoder reading one item at a time does. A *size* below one raises
    :class:`ValueError`.
    """
    if size < 1:
        msg = f"a batch holds at least one item, not {size}"
        raise ValueError(msg)
    batch: list[T] = []
    try:
        async for item in items:
            batch.append(item)
            if len(batch) == size:
                yield batch
                batch = []
    except ReadError:
        if batch:
            yield batch
        raise
    if batch:
        yield batch
