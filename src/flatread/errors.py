"""The two things that go wrong: a body that is not its file, and a reader declared wrong.

* :class:`ReadError` is raised by :meth:`~flatread.core.File.frame` and :meth:`~flatread.core.File.rows` for a body
  that is not the file its reader describes: a cell that is not its column's type, a row of the wrong width, a header
  that is not the struct's labels, a cut or damaged body, and a body on which polars' or fastexcel's Rust code panics
  (pyo3's ``PanicException``, no ``Exception`` of its own). Its message names where: the column by its label, the row,
  the cell, the line or the member. :class:`EmptyBodyError` and :class:`RaggedRowError` are the two kinds worth
  telling apart.
* :class:`LayoutError` is raised for a reader declared wrong: a field with no ``Label`` where its format needs one, a
  ``Format`` on a field that is no date or that polars cannot read it by, a ``Shape`` polars cannot compile, spans that
  overlap, a required switch left unset, a switch naming a field the struct lacks. Most are raised when the reader is
  built; a few only show once a body is read (an inner reader's columns that are not the struct's).

Both are :class:`ValueError`, as :mod:`json` and msgspec raise theirs, so code that catches a decoder's
``ValueError`` catches them; catch :class:`ReadError` alone to handle bad data and let a misdeclared reader fail loudly.
"""

__all__ = ["EmptyBodyError", "LayoutError", "RaggedRowError", "ReadError"]


class ReadError(ValueError):
    """A body that is not the file its reader describes; the message names where it went wrong."""


class EmptyBodyError(ReadError):
    """A body with nothing to read: no bytes, only whitespace, or a header and no rows where rows are required."""


class RaggedRowError(ReadError):
    """A row with more or fewer cells than its header."""


class LayoutError(ValueError):
    """A reader declared wrong: its row struct's markers or its switches describe no file it can read."""
