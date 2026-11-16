"""The two things that go wrong: a body that is not its file, and a reader declared wrong.

* :class:`ReadError` is raised by :meth:`~flatread.core.File.frame` and :meth:`~flatread.core.File.rows` for a body
  that is not the file its reader describes: a cell that is not its column's type, a row of the wrong width, a header
  that is not the struct's labels, a cut or damaged body, and a body on which polars' or fastexcel's Rust code panics
  (pyo3's ``PanicException``, no ``Exception`` of its own). Its message names where: the column by its label, the row,
  the cell, the line or the member. Its subclasses name the kinds worth telling apart: :class:`EmptyBodyError`,
  :class:`RaggedRowError`, :class:`TruncatedFileError`, :class:`ControlTotalError`, :class:`UnknownLayoutError`,
  :class:`CorruptArchiveError` and :class:`EncodingError`. A cell that is not its type, or a body past a reader's
  cap, is a plain :class:`ReadError`.
* :class:`LayoutError` is raised for a reader declared wrong: a field with no ``Label`` where its format needs one, a
  ``Format`` on a field that is no date or that polars cannot read it by, a ``Shape`` polars cannot compile, spans that
  overlap, a required switch left unset, a switch naming a field the struct lacks. Most are raised when the reader is
  built; a few only show once a body is read (an inner reader's columns that are not the struct's).

Both are :class:`ValueError`, as :mod:`json` and msgspec raise theirs, so code that catches a decoder's
``ValueError`` catches them; catch :class:`ReadError` alone to handle bad data and let a misdeclared reader fail loudly.
"""

__all__ = [
    "ControlTotalError",
    "CorruptArchiveError",
    "EmptyBodyError",
    "EncodingError",
    "LayoutError",
    "RaggedRowError",
    "ReadError",
    "TruncatedFileError",
    "UnknownLayoutError",
]


class ReadError(ValueError):
    """A body that is not the file its reader describes; the message names where it went wrong."""


class EmptyBodyError(ReadError):
    """A body with nothing to read: no bytes, only whitespace, or a header and no rows where rows are required."""


class RaggedRowError(ReadError):
    """A row with more or fewer cells than its header."""


class TruncatedFileError(ReadError):
    """A body cut short: it ends inside a member, before a title or trailer the file writes, or not as the file ends."""


class ControlTotalError(ReadError):
    """A count the file states, in a trailer or a title, that is not the number of rows it holds."""


class UnknownLayoutError(ReadError):
    """A body laid out as some other file: a header, root, sheet, title or member that is not the reader's."""


class CorruptArchiveError(ReadError):
    """A zip that will not open, or a member that will not inflate or whose CRC-32 and size are not its own."""


class EncodingError(ReadError):
    """Bytes that are not the text encoding the file is written in, or a charset no codec reads."""


class LayoutError(ValueError):
    """A reader declared wrong: its row struct's markers or its switches describe no file it can read."""
