"""A table cut from a grid of text cells: a sheet's, a delimited file's, read cell by cell as text.

:class:`GridFile` is the base: a source reads the body into a grid (:meth:`~GridFile._body_grid`), a String frame of a
row per row of the file and a column per column, and :class:`GridFile` cuts its table from it: the header in one row,
under the rows right above it that head its columns as a sheet's do (``_above``), then every filled row below it,
each cell typed as a :class:`~flatread.core.TextFile`'s. The sources are :class:`~flatread.delimited.CsvText` (a
delimited file as the ``csv`` module reads it), :class:`~flatread.sheets.SheetText` (a sheet's every cell as text, a
number with every digit) and :class:`~flatread.sheets.BiffSheet` (an Excel 3 or 4 workbook's). An error names a row
by its number in the grid, the file's own (a sheet's row, a delimited file's record), blank rows counted.

The pieces below change the cut, each composed before its source (``class _Fees(BesideGrid[R], CsvText[R])``):

* :class:`TitledGrid` finds the header under a title row;
* :class:`FilledHeader` finds it by its shape, the first row about as filled as the fullest;
* :class:`BlankEnded` ends the table at its first blank row;
* :class:`BesideGrid` reads a band of the grid's columns.
"""

import abc
import typing
from collections.abc import Sequence

import msgspec
import polars as pl

from flatread.core import TextFile, _check_above, _first, _repeated, _stacked
from flatread.errors import EmptyBodyError, LayoutError, ReadError

__all__ = ["BesideGrid", "BlankEnded", "FilledHeader", "GridFile", "TitledGrid"]


class GridFile[RowT: msgspec.Struct](TextFile[RowT]):
    """A table cut from a grid of text cells, which a source reads from the body: a sheet's, a delimited file's.

    A source (:class:`~flatread.sheets.SheetText`, :class:`~flatread.delimited.CsvText`) reads the body into the grid
    (:meth:`_body_grid`), a String frame: a row per row of the file from its first, a column per column (``0``,
    ``1``, ...), a cell of nothing but whitespace and each of the file's words for none (*null_values*) no value. A
    format cuts its table from it (:meth:`_table`: by default its header in row *header_row*, under the
    :attr:`_above` rows right over it, and every filled row below, a column labeled one of :attr:`_unread` dropped, a
    cell past the header refused) and :class:`~flatread.core.TextFile` types its cells, an error naming the grid row
    a cell stands in. A piece changes the cut: :class:`TitledGrid` and :class:`FilledHeader` find the header
    (:meth:`_header_at`), :class:`BlankEnded` ends the rows (:meth:`_rows_below`), :class:`BesideGrid` reads a band
    of the columns.
    """

    #: How many rows right over the header row head its columns too (a group over its labels): a cell heads its own
    #: column and every column to its right with no cell of its own, to the header's last filled column. Each
    #: column's header is then its cells top to bottom, stripped, the unit separator (U+001F) between, which a reader
    #: that lays out the key columns (:class:`LongFile`) takes them from.
    _above: typing.ClassVar[int] = 0

    def __init__(self, row: type[RowT], *, header_row: int = 1, null_values: Sequence[str] = ()) -> None:
        """Lay out a table of *row* under its header in row *header_row* of the grid (the file's own, from 1).

        *null_values* are the file's words for no value. Raises ``LayoutError`` for a *header_row* below 1, where the
        :attr:`_above` rows do not fit over it, or what :class:`~flatread.core.TextFile` refuses.
        """
        super().__init__(row, null_values=null_values)
        if header_row < 1:
            msg = f"the header row {header_row} is no row of a grid, whose rows count from 1"
            raise LayoutError(msg)
        _check_above(self._above, header_row)
        self._header_row = header_row

    @typing.override
    def _joined_header(self) -> bool:
        return bool(self._above)

    @abc.abstractmethod
    def _body_grid(self, body: bytes) -> pl.DataFrame:
        """Return *body*'s grid: its every cell as text, a row per row from its first, a column per column."""

    @typing.override
    def _cells(self, body: bytes) -> pl.DataFrame:
        return self._table(self._body_grid(body))

    def _header_at(self, grid: pl.DataFrame) -> int:
        """Return the grid row (from 1) of *grid*'s header: *header_row*, unless a piece finds it."""
        del grid
        return self._header_row

    def _rows_below(self, grid: pl.DataFrame, at: int) -> pl.DataFrame:
        """Return the grid rows under the header in row *at*: every one to the grid's end, unless a piece ends them.

        A piece returns the rows from the one right under the header, ended sooner (an error numbers them so).
        """
        return grid.slice(at)

    def _header_of(self, grid: pl.DataFrame, at: int) -> list[str]:
        """Return the labels *grid*'s header row *at* writes, to its last filled cell, each under the rows over it."""
        if grid.height < at:
            return []
        if at - 1 - self._above < 0:
            msg = f"the header in row {at} has not the {self._above} rows over it that head its columns too"
            raise ReadError(msg)
        rows = [grid.row(row) for row in range(at - 1 - self._above, at)]
        if not self._above:
            header: list[str] = [cell or "" for cell in rows[-1]]
            while header and not header[-1]:
                header.pop()
            return header
        cells = [[None if cell is None else cell.strip() or None for cell in row] for row in rows]
        width = max((at + 1 for row in cells for at, cell in enumerate(row) if cell is not None), default=0)
        return _stacked([row[:width] for row in cells])

    def _table(self, grid: pl.DataFrame) -> pl.DataFrame:
        """Return the table in *grid*: its header row's labels over every filled row below it, to the grid's end."""
        at = self._header_at(grid)
        header = self._header_of(grid, at)
        # A stacked header names each column by its heading and label: two alike are no columns a format tells apart.
        if self._above and len(set(header)) != len(header):
            msg = f"the header {header} heads two columns alike, which no format of the grid tells apart"
            raise ReadError(msg)
        below = self._rows_below(grid, at)
        if (past := below.select(grid.columns[len(header) :])).width and past.null_count().sum_horizontal()[0] != (
            past.width * past.height
        ):
            msg = f"a row below the header {header} holds a cell past its last label, which no column of the table is"
            raise ReadError(msg)
        rows = below.select(grid.columns[: len(header)])
        # A label written twice is two columns, named by their place as fastexcel names them (``Rate``, ``Rate_1``).
        if len(set(header := _repeated(header, "{label}_{n}", 1))) != len(header):
            msg = f"the header {header} leaves two cells empty, which no format of the sheet reads"
            raise ReadError(msg)
        rows.columns = header
        if self._unread:
            rows = rows.select(label for label in header if label not in self._unread)
        # Each row's number in the grid, the file's own: the one under the header is row at + 1.
        numbers = pl.int_range(at + 1, at + 1 + rows.height, dtype=pl.Int64, eager=True)
        if rows.width:
            filled = rows.select(~pl.all_horizontal(pl.all().is_null())).to_series()
            rows, numbers = rows.filter(filled), numbers.filter(filled)
        self._number_rows(numbers)
        return rows


class BlankEnded[RowT: msgspec.Struct](GridFile[RowT]):
    """A grid table that ends at its first blank row: what stands below it (notes, another table) is not read.

    Example: ``docs/examples/blank_ended.csv``, a seasonal factor file whose months stop at a blank row over
    ``Last Updated: 9-30-2026``. :class:`GridFile`'s own table runs to the grid's end, skipping blank rows;
    :class:`~flatread.sheets.NotedSheet`'s ends at its first row with nothing past column A, this one at a row with
    nothing at all.
    """

    @typing.override
    def _rows_below(self, grid: pl.DataFrame, at: int) -> pl.DataFrame:
        below = super()._rows_below(grid, at)
        end = _first(below.select(pl.all_horizontal(pl.all().is_null())).to_series(), 0)
        return below if end is None else below.head(end)


class TitledGrid[RowT: msgspec.Struct](GridFile[RowT]):
    """A grid table under its title row: its header is the first filled row below the title's.

    The title's row is the first whose first cell (the grid's first column, column A of a sheet read from it) is
    :attr:`_title`, as written, and whose every other cell is empty: a title written in another column is not found.
    Example: ``docs/examples/titled_grid.csv``, a file of prose and titled tables (``I. Specifications for
    individual IP indexes``). :class:`~flatread.delimited.PreambleCsv` finds its header by the struct's first label and
    :class:`~flatread.delimited.TitledCsv` matches every line above it; this one finds one table of several by the
    title over it, so each of a file's tables is read by its title. Compose it with :class:`BlankEnded` for a table
    that ends at the next blank row. Raises ``LayoutError``, when built, for :attr:`_title` unset.
    """

    #: The title row's one cell, as written: a format's subclass sets it.
    _title: typing.ClassVar[str]

    @typing.override
    def _check_switches(self) -> None:
        super()._check_switches()
        if not isinstance(getattr(self, "_title", None), str):
            msg = f"{type(self).__name__} is a TitledGrid, so it names _title, the text of its title's cell"
            raise LayoutError(msg)

    @typing.override
    def _header_at(self, grid: pl.DataFrame) -> int:
        first, rest = pl.col(grid.columns[0]), pl.exclude(grid.columns[0])
        alone = (first == self._title) & pl.all_horizontal(rest.is_null()) if grid.width > 1 else first == self._title
        if (title := _first(grid.select(alone.fill_null(value=False)).to_series(), 1)) is None:
            msg = f"no row is the title {self._title!r} alone"
            raise ReadError(msg)
        filled = grid.slice(title).select(~pl.all_horizontal(pl.all().is_null())).to_series()
        if (header := _first(filled, title + 1)) is None:
            msg = f"the title {self._title!r} has no table below it"
            raise ReadError(msg)
        return header


class FilledHeader[RowT: msgspec.Struct](GridFile[RowT]):
    """A grid table whose header is the first row about as filled as its fullest row, wherever the titles end.

    Example: ``docs/examples/filled_header.txt``, a facility disclosure sheet whose header is row 2 under a
    one-cell title. The header is the first row filling at least :attr:`_fewest` cells and no more than
    :attr:`_short_by` fewer than the fullest row. A grid whose fullest row fills fewer than :attr:`_fewest` holds no
    table (a note, ``No ETF trades``): it is read as no rows where the format's :attr:`_rows_required` is false, and
    raises ``EmptyBodyError`` otherwise. :class:`TitledGrid` finds its header under a known title and
    :class:`~flatread.delimited.PreambleCsv` by its first label; this one by the header's shape alone, for a sheet
    whose titles and labels change from release to release.
    """

    #: The fewest cells a header fills: a grid whose fullest row fills fewer holds no table.
    _fewest: typing.ClassVar[int] = 2
    #: How many fewer cells than the fullest row the header may fill (a row of values may fill more than its header).
    _short_by: typing.ClassVar[int] = 3

    @typing.override
    def _table(self, grid: pl.DataFrame) -> pl.DataFrame:
        if self._fullest(grid) >= self._fewest:
            return super()._table(grid)
        if self._rows_required:
            msg = f"the grid holds no table: no row of it fills {self._fewest} cells, as a header does"
            raise EmptyBodyError(msg)
        # No rows under the header the reader lays out with no columns: a long table's own, else the struct's.
        laid = self._layout([])
        labels = self._labels if laid is None else [column.label for column in laid]
        return pl.DataFrame(schema=dict.fromkeys(labels, pl.String))

    @typing.override
    def _header_at(self, grid: pl.DataFrame) -> int:
        # The fullest row is one such row, so there is a first.
        floor = max(self._fewest, self._fullest(grid) - self._short_by)
        return int((self._filled(grid) >= floor).arg_true()[0]) + 1

    def _filled(self, grid: pl.DataFrame) -> pl.Series:
        """Return how many cells each row of *grid* fills."""
        return grid.select(pl.sum_horizontal(pl.all().is_not_null())).to_series()

    def _fullest(self, grid: pl.DataFrame) -> int:
        """Return how many cells the fullest row of *grid* fills: 0 for a grid of no rows or columns."""
        fullest = self._filled(grid).max() if grid.height and grid.width else 0
        return fullest if isinstance(fullest, int) else 0


class BesideGrid[RowT: msgspec.Struct](GridFile[RowT]):
    """A grid table with more beside it (a second table, a spacer): only the grid's columns in :attr:`_band` are read.

    Example: ``docs/examples/beside_grid.csv``, two fee reviews side by side, B to D and F on.
    :class:`~flatread.sheets.BesideSheet`'s twin: that one names each column it loads (``_columns_at``); this one cuts
    a band, from its first column to before its last (``None``: the grid's end), so a table that grows to the right
    keeps every column.
    """

    #: The table's first grid column (``0`` is the file's first) and the column past its last, ``None`` the grid's end.
    _band: typing.ClassVar[tuple[int, int | None]] = (0, None)

    @typing.override
    def _table(self, grid: pl.DataFrame) -> pl.DataFrame:
        first, last = self._band
        return super()._table(grid.select(grid.columns[first:last]))
