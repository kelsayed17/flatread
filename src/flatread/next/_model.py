"""The values a read passes from stage to stage, and the protocols a stage implements.

A body goes through a reader's stages in one order: its *source* opens the bytes (a zip's member), its *format* reads
them into a :class:`Grid` of text cells, its *table* stage finds the table in the grid (a :class:`Table`: the header's
labels over the rows), its *shape* reshapes the table (a wide table read long), and its *cells* cleaners clean each
column's text. The reader then types the table by its row struct, one way for every format, and checks it. Each stage
is a value: a frozen object a reader is built from, sharing no state with a read. What one read learns as it goes (the
trailer's match, the member it opened) it notes on its :class:`ReadContext`, which a later stage may read.
"""

import dataclasses
import re
import typing
from collections.abc import Mapping, Sequence

import polars as pl

from flatread.core import Column

if typing.TYPE_CHECKING:
    from flatread.next._stages import Layout
    from flatread.pdf import PdfLine

__all__ = ["Cleaner", "Format", "Grid", "ReadContext", "Records", "Shape", "Source", "Table", "TableFinder"]


@dataclasses.dataclass(frozen=True)
class Grid:
    """A file's rows of text cells, as its format reads them, before its table is found.

    *cells* is a String frame, its columns ``0``, ``1``, ... and a row narrower than the widest padded with nulls;
    *widths* is each row's own number of cells, *raw* each row's text as the file writes it (a CSV's line), and
    *numbers* each row's number in the file, which an error names it by. A format that knows its table's header
    already (an XML record's elements, an SDMX message's dimensions) states it as *header*, and its rows are the cells.
    """

    cells: pl.DataFrame
    widths: pl.Series
    raw: pl.Series
    numbers: pl.Series
    header: Sequence[str] | None = None
    #: Whether every row is the grid's width (a sheet's), so a row narrower than its header is no fault.
    rectangular: bool = False

    @property
    def height(self) -> int:
        """The number of rows."""
        return self.cells.height

    def slice(self, start: int, end: int | None = None) -> Grid:
        """Return the rows from *start* up to *end* (Python's slice, the last row ``-1``)."""
        stop = self.height if end is None else end if end >= 0 else self.height + end
        length = max(stop - start, 0)
        return dataclasses.replace(
            self,
            cells=self.cells.slice(start, length),
            widths=self.widths.slice(start, length),
            raw=self.raw.slice(start, length),
            numbers=self.numbers.slice(start, length),
        )


@dataclasses.dataclass(frozen=True)
class Table:
    """A file's table: a String frame of its cells, each column named by its header's label, and each row's number."""

    cells: pl.DataFrame
    numbers: pl.Series


@dataclasses.dataclass
class ReadContext:
    """What one read of one body has learned so far, for the stages after the one that learned it.

    A reader makes one per read, so a stage never keeps a read's state on itself and one reader serves every thread.
    *stated* holds the text of the fields a stage reads once for every row (a trailer's stamp), by field; *matches* the
    regex matches a stage found (``matches["trailer"]``); *member* the name of the zip member opened, if one was.
    """

    body: bytes
    stated: dict[str, str | None] = dataclasses.field(default_factory=dict)
    matches: dict[str, re.Match[str]] = dataclasses.field(default_factory=dict)
    member: str | None = None
    #: The row struct's columns, by field: a format with no header (a PDF's records) lays its cells out by them.
    columns: Mapping[str, Column] = dataclasses.field(default_factory=dict)
    #: The row struct itself.
    row: type | None = None


class Source(typing.Protocol):
    """Opens a body into the bytes of the file the format reads (a zip's member, a transcoded text)."""

    def open(self, body: bytes, read: ReadContext) -> bytes:
        """Return the file's bytes in *body*, or raise ``ReadError``."""
        ...


class Records(typing.Protocol):
    """Reads a document's records out of its lines of text (a PDF's), each record its cells by field."""

    def records(
        self, lines: Sequence[PdfLine], layout: Layout | None, read: ReadContext
    ) -> Sequence[Mapping[str, str | None]]:
        """Return each record's text cells by field name (a field it lacks is no value), or raise ``ReadError``."""
        ...


class Format(typing.Protocol):
    """Reads a file into its grid of text cells."""

    def grid(self, body: bytes, read: ReadContext) -> Grid:
        """Return *body*'s rows of cells, or raise ``ReadError``."""
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
