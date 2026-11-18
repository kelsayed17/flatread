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

To tell a download to fetch again from a file the publisher changed: a text body cut short (CSV, XML, a trailer missing)
is :class:`TruncatedFileError`, and a zip cut short or damaged, or a body that is no zip where one was expected (an
HTML error page), is :class:`CorruptArchiveError`, since a zip's directory sits at its end; both call for a fresh
download. A body laid out as some other file (a root, sheet, header or member that is not the reader's) is
:class:`UnknownLayoutError`, which calls for a look at what the publisher changed.

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
    """A body that is not the file its reader describes; the message names where it went wrong.

    Where a reader knows them, the error carries where as values too, each ``None`` where it does not: :attr:`row`,
    the row its message names; :attr:`label`, the column's label; and :attr:`member`, the zip member read. A
    :mod:`flatread.next` reader numbers a row as its file does (a text file's line, a sheet's row) and sets all three
    wherever its message names them; the other readers set ``row`` and ``label`` on a cell's error, numbering rows
    as their messages do.
    """

    def __init__(
        self, *args: object, row: int | None = None, label: str | None = None, member: str | None = None
    ) -> None:
        """Raise with the message *args*, and *row*, *label* and *member* where they are known."""
        super().__init__(*args)
        #: The row as the file numbers it, where the error is one row's.
        self.row = row
        #: The label of the column, where the error is one column's.
        self.label = label
        #: The name of the zip member read, where the body was a zip's.
        self.member = member


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
