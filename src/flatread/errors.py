"""The errors a read raises for a body of the wrong shape.

Every one is a :class:`ReadError`, which is a :class:`ValueError`: a body of the wrong shape is a bad value, as
:mod:`json` and msgspec raise theirs. headgate's ``body_reader`` already raises a ``ValueError`` from its decoder as
``ResponseParseError`` on a 2xx, with the read's error as its cause.
"""

__all__ = ["EmptyBodyError", "RaggedRowError", "ReadError", "UnknownLayoutError"]


class ReadError(ValueError):
    """A body that is not the file it should be: the root of the errors flatread names."""


class EmptyBodyError(ReadError):
    """The body has nothing to read: no bytes, only whitespace, or a header and too few rows."""


class UnknownLayoutError(ReadError):
    """The body is not laid out as the reader says: another header, another record shape, or no file at all."""


class RaggedRowError(ReadError):
    """A row has more or fewer fields than its header."""
