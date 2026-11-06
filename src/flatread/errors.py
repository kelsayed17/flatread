"""The errors a read raises, one class for each thing it can find wrong with a body.

Every one is a :class:`ReadError`, which is a :class:`ValueError`: a body of the wrong shape is a bad value, as
:mod:`json` and msgspec raise theirs. So code that already treats a ``ValueError`` from a decoder as a malformed body
handles these too. An HTTP client that reads a body through a function raising them (headgate's ``body_reader``, for
one) reports a parse error with the read's error as its cause; called on bytes the caller already holds, the read raises
it directly.

The subclass names what was found: no body at all (:class:`EmptyBodyError`), an archive that cannot be read
(:class:`CorruptArchiveError`), text in the wrong encoding (:class:`EncodingError`), a layout the read does not know
(:class:`UnknownLayoutError`, and :class:`~flatread.cells.CellError` for one cell), a row of the wrong width
(:class:`RaggedRowError`), a file that ends early (:class:`TruncatedFileError`), a total that does not match
(:class:`ControlTotalError`), and the publisher's own answer that it has no file (:class:`NotPublishedError`).
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "ControlTotalError",
    "CorruptArchiveError",
    "EmptyBodyError",
    "EncodingError",
    "NotPublishedError",
    "RaggedRowError",
    "ReadError",
    "TruncatedFileError",
    "UnknownLayoutError",
    "or_not_published",
]


class ReadError(ValueError):
    """A body that is not the file it should be: the root of every error flatread raises for one."""


class EmptyBodyError(ReadError):
    """The body has nothing to read: no bytes, or only blank lines."""


class CorruptArchiveError(ReadError):
    """The body is an archive that cannot be read.

    It is damaged or was cut off, or its file is encrypted, or compressed with a method no real archive uses: any but
    stored and deflated.
    """


class UnknownLayoutError(ReadError):
    """The header is not a layout the decoder knows.

    Renamed, reordered, added and dropped columns all land here, and so does a body that is not the wire at all, such
    as an error page, whose first line is no header the decoder has seen.
    """


class RaggedRowError(ReadError):
    """A row has more or fewer fields than its header."""


class TruncatedFileError(ReadError):
    """The file does not end the way every real one does, such as with its trailer, so it may have been cut off."""


class ControlTotalError(ReadError):
    """A total the file declares for itself does not match what was read: rows were lost or added, or values changed.

    A record count is one such total. A column's sum, such as the total quantity of shares SEC's fails-to-deliver file
    declares, is another.
    """


class EncodingError(ReadError):
    """The body is not in the wire's text encoding: a byte that is not UTF-8, in a file every copy of which was."""


class NotPublishedError(ReadError):
    """The body is the publisher's own answer that it has no file to give, such as Nasdaq's "Page Not Available" page.

    OCC's one sentence for a missing day is another. A caller walking dates reads it as a day with no file. Only the
    answers a decoder knows by sight raise this; any other body that is not the file is an
    :class:`UnknownLayoutError`, so drift cannot pass for a missing day.
    """


def or_not_published[T, M](marker: Callable[[], M], decode: Callable[[bytes], T], body: bytes, /) -> T | M:
    """Decode *body* with *decode*, or return ``marker()`` when *decode* raises :class:`NotPublishedError`.

    How a reader answers a publisher's "no file for that date" with a value rather than an error:
    ``partial(or_not_published, NotPublished, read_x)``, ``NotPublished`` being the caller's own marker. Every other
    error *decode* raises still propagates, so drift still reads as drift.
    """
    try:
        return decode(body)
    except NotPublishedError:
        return marker()
