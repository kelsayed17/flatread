"""The values a read passes from stage to stage, and the protocols a stage implements.

A body goes through a reader's stages in one order: its *source* opens it as a stream (a zip's member, read as it
inflates), its *format* reads the stream into a grid, its *table* stage finds the table in the grid (a :class:`Table`:
the header's labels over the rows), its *shape* reshapes the table (a wide table read long), and its *cells* cleaners
clean each column's text. The reader's engine then types the table by its row struct, one way for every format, and
checks it. Each stage is a value: a frozen object a reader is built from, sharing no state with a read. What one read
learns as it goes (the trailer's match, the member it opened) it notes on its :class:`ReadContext`.

A grid is of one of two kinds. A :class:`Lines` grid is a text file's: each row a line of its own width, every cell
text. A :class:`Cells` grid is a sheet's or a document's: rectangular, each row as wide as the grid, and each cell of a
kind (text, a number, a boolean, a moment) its format knows, with its native value beside its text.
"""

import contextlib
import dataclasses
import re
import typing
from collections.abc import Collection, Mapping, Sequence

import polars as pl

from flatread.core import Column

if typing.TYPE_CHECKING:
    from flatread.next._stages import Layout
    from flatread.pdf import PdfLine

__all__ = [
    "Cells",
    "Cleaner",
    "Format",
    "Grid",
    "HeaderRule",
    "Kind",
    "Lines",
    "ReadContext",
    "Records",
    "Shape",
    "Source",
    "Stream",
    "Table",
    "TableFinder",
]

#: A cell's kind in a :class:`Cells` grid: what its format stored, not what its text looks like.
type Kind = typing.Literal["text", "number", "bool", "moment"]


class Stream(typing.Protocol):
    """Bytes read in pieces: a file, a ``BytesIO``, a zip member inflating."""

    def read(self, size: int = -1, /) -> bytes:
        """Return up to *size* more bytes (all of the rest for ``-1``); ``b""`` at the end."""
        ...


@dataclasses.dataclass(frozen=True)
class Lines:
    """A text file's rows, as its format reads them: each line's cells, of the line's own width, every one text.

    *cells* is a String frame, its columns ``0``, ``1``, ... and a row narrower than the widest padded with nulls;
    *widths* is each row's own number of cells, *raw* each row's text as the file writes it (a CSV's line), and
    *numbers* each row's number in the file, which an error names it by. A blank line is a row of one empty cell.
    """

    cells: pl.DataFrame
    widths: pl.Series
    raw: pl.Series
    numbers: pl.Series

    @property
    def height(self) -> int:
        """The number of rows."""
        return self.cells.height

    def slice(self, start: int, end: int | None = None) -> Lines:
        """Return the rows from *start* up to *end* (Python's slice, the last row ``-1``)."""
        at, length = _span(self.height, start, end)
        return Lines(
            cells=self.cells.slice(at, length),
            widths=self.widths.slice(at, length),
            raw=self.raw.slice(at, length),
            numbers=self.numbers.slice(at, length),
        )


@dataclasses.dataclass(frozen=True)
class Cells:
    """A sheet's or a document's rows: a rectangle of cells, each of a kind its format stored, beside its text.

    *text* is a String frame of every cell's text (a number with every digit it holds), its columns ``0``, ``1``, ...;
    *kinds* the same shape, each cell's :data:`Kind` (null where empty), or ``None`` where every cell is text (a
    document's records); *floats* a number cell's value and *moments* a moment cell's, null elsewhere. *raw* is each
    row's text, its cells joined by tabs up to its last filled one (what a title matches), *numbers* each row's number
    in the file. A format that knows its table's labels (a document's records, an SDMX message's attributes) names
    them as *header*: then every row is a row.
    """

    text: pl.DataFrame
    raw: pl.Series
    numbers: pl.Series
    kinds: pl.DataFrame | None = None
    floats: pl.DataFrame | None = None
    moments: pl.DataFrame | None = None
    header: Sequence[str] | None = None

    @property
    def height(self) -> int:
        """The number of rows."""
        return self.text.height

    def slice(self, start: int, end: int | None = None) -> Cells:
        """Return the rows from *start* up to *end* (Python's slice, the last row ``-1``)."""
        at, length = _span(self.height, start, end)
        return dataclasses.replace(
            self,
            text=self.text.slice(at, length),
            raw=self.raw.slice(at, length),
            numbers=self.numbers.slice(at, length),
            kinds=None if self.kinds is None else self.kinds.slice(at, length),
            floats=None if self.floats is None else self.floats.slice(at, length),
            moments=None if self.moments is None else self.moments.slice(at, length),
        )


type Grid = Lines | Cells


def _span(height: int, start: int, end: int | None) -> tuple[int, int]:
    stop = height if end is None else end if end >= 0 else height + end
    return start, max(stop - start, 0)


@dataclasses.dataclass(frozen=True)
class Table:
    """A file's table: its cells' text under its header's labels, each row's number, and its cells' kinds if known.

    *kinds*, *floats* and *moments* are a :class:`Cells` grid's, their columns named as *cells*' are: ``None`` for a
    table of text alone.
    """

    cells: pl.DataFrame
    numbers: pl.Series
    kinds: pl.DataFrame | None = None
    floats: pl.DataFrame | None = None
    moments: pl.DataFrame | None = None

    def frames(self) -> list[pl.DataFrame | None]:
        """Return the table's frames, all of one shape: its text, kinds, floats and moments."""
        return [self.cells, self.kinds, self.floats, self.moments]

    def with_frames(self, frames: Sequence[pl.DataFrame | None], numbers: pl.Series) -> Table:
        """Return a table of *frames*, as :meth:`frames` orders them, and *numbers*."""
        cells, kinds, floats, moments = frames
        assert cells is not None  # noqa: S101 (a table always has its text)
        return Table(cells, numbers, kinds, floats, moments)


@dataclasses.dataclass
class ReadContext:
    """What one read of one body has learned so far, for the stages after the one that learned it.

    A reader makes one per read, so a stage never keeps a read's state on itself and one reader serves every thread.
    *stated* holds the text of the fields a stage reads once for every row (a trailer's stamp), by field; *matches* the
    regex matches a stage found (``matches["trailer"]``); *member* the name of the zip member opened, if one was.
    *columns* are the row struct's columns, by field, and *row* the struct: a format with no header (a document's
    records, an SDMX message's attributes) lays its cells out by them. *required* are the fields that hold a value on
    every row (none admits ``None`` or has a default), whose column every header names.
    """

    body: bytes
    stated: dict[str, str | None] = dataclasses.field(default_factory=dict)
    matches: dict[str, re.Match[str]] = dataclasses.field(default_factory=dict)
    member: str | None = None
    columns: Mapping[str, Column] = dataclasses.field(default_factory=dict)
    row: type | None = None
    required: frozenset[str] = frozenset()


class Source(typing.Protocol):
    """Opens a body as the stream of the file its format reads: a zip's member, read as it inflates."""

    def open(self, body: bytes, read: ReadContext) -> contextlib.AbstractContextManager[Stream]:
        """Return the file in *body*, open as a stream, noting on *read* what it opened; or raise ``ReadError``."""
        ...


class Format(typing.Protocol):
    """Reads a file into its grid."""

    def grid(self, stream: Stream, read: ReadContext) -> Grid:
        """Return the file's rows, read from *stream*, or raise ``ReadError``."""
        ...


class Records(typing.Protocol):
    """Reads a document's records out of its lines of text (a PDF's), each record its cells by field."""

    def records(
        self, lines: Sequence[PdfLine], layout: Layout | None, read: ReadContext
    ) -> Sequence[Mapping[str, str | None]]:
        """Return each record's text cells by field name (a field it lacks is no value), or raise ``ReadError``."""
        ...


class TableFinder(typing.Protocol):
    """Finds a file's table in its grid: where its header is, where its rows start and end, what it states."""

    @property
    def stated(self) -> frozenset[str]:
        """The fields the finder states for every row, from the file around the table (a trailer's groups)."""
        ...

    @property
    def vouches(self) -> bool:
        """Whether the file vouches for a table of no rows (a trailer that closes it): a header alone is an answer."""
        ...

    def find(self, grid: Grid, read: ReadContext) -> Table:
        """Return the table in *grid*, noting what it states on *read*, or raise ``ReadError``."""
        ...


class HeaderRule(typing.Protocol):
    """How a header's labels name the struct's columns: which headers a file is published under, what they drop.

    A table finder finds the header row and hands its table here, each column under its header cell (a repeated
    cell's later columns apart, by a NUL and their count); the rule returns it under the struct's labels, in the
    struct's order, a column the header lacks null, or raises ``UnknownLayoutError`` for a header it is not.
    """

    def check(self, columns: Sequence[Column], required: Collection[str]) -> None:
        """Raise ``LayoutError`` if the rule could never read the struct of *columns*, *required* its fields'."""
        ...

    def match(self, table: Table, read: ReadContext) -> Table:
        """Return *table* under the struct's labels, in order, a column its header lacks null; or raise."""
        ...


class Shape(typing.Protocol):
    """Reshapes a table into its row struct's columns (a wide table read long)."""

    def shape(self, table: Table, columns: Sequence[Column], read: ReadContext) -> Table:
        """Return *table* as the struct's *columns*' labels, or raise ``ReadError``."""
        ...


class Cleaner(typing.Protocol):
    """Cleans a column's text cells before they are typed (padded with spaces, grouped in thousands)."""

    def clean(self, cells: pl.Series, column: Column) -> pl.Series:
        """Return *column*'s *cells* cleaned, or raise ``ReadError`` naming the cell."""
        ...
