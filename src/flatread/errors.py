"""The errors a read raises, one class for each thing it can find wrong with a body.

Every one is a :class:`ReadError`, which is a :class:`ValueError`: a body of the wrong shape is a bad value, as
:mod:`json` and msgspec raise theirs. headgate's ``body_reader`` already raises a ``ValueError`` from its decoder as
``ResponseParseError`` on a 2xx, with the read's error as its cause.
"""

__all__ = [
    "CellError",
    "CheckError",
    "ControlTotalError",
    "CorruptArchiveError",
    "EmptyBodyError",
    "EncodingError",
    "ErratumGoneError",
    "NotPublishedError",
    "RaggedRowError",
    "ReadError",
    "TruncatedFileError",
    "UnknownLayoutError",
]


class ReadError(ValueError):
    """A body that is not the file it should be: the root of every error flatread raises for one."""


class EmptyBodyError(ReadError):
    """The body has nothing to read: no bytes, only whitespace, or a header and too few rows."""


class UnknownLayoutError(ReadError):
    """The body is not laid out as the layout says: another header, another record shape, or no file at all."""


class CellError(UnknownLayoutError):
    """One cell is not its field's type: it names where the cell is, its label and its text."""

    def __init__(self, where: str, label: str, text: str, expected: str) -> None:
        """Name the cell at *where* under *label*, written *text*, which is not *expected*."""
        super().__init__(f"{where}: the {label!r} cell {text!r} is not {expected}")
        self.where = where
        self.label = label
        self.text = text


class RaggedRowError(ReadError):
    """A row has more or fewer fields than its header."""


class TruncatedFileError(ReadError):
    """The file does not end the way every real one does (its line ending, its trailer), so it may have been cut off."""


class ControlTotalError(ReadError):
    """A total the file declares for itself, such as its trailer's row count, does not match what was read."""


class CheckError(ReadError):
    """A layout's own ``check`` found a row that breaks a rule of the provider's."""


class ErratumGoneError(ReadError):
    """A provider mistake the layout fixes is gone from the body: the provider fixed it, so drop the erratum."""


class NotPublishedError(ReadError):
    """The body is the publisher's own answer that it has no file to give, such as Nasdaq's "Page Not Available"."""


class CorruptArchiveError(ReadError):
    """The body is an archive that cannot be read: damaged, cut off, encrypted, or compressed by a method not taken."""


class EncodingError(ReadError):
    """The body is not in the layout's text encoding."""
