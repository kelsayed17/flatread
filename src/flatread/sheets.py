"""A workbook sheet's table read natively into its row struct's fields, by fastexcel (an Excel 3 or 4 workbook by xlrd).

:class:`SheetFile` is a clean table on one sheet of a workbook: its header in one row (row 1, or below titles), then a
row per record to the sheet's end, nothing outside the header's columns. fastexcel loads the sheet twice, as text and
typed by the struct's fields, because a typed load coerces without a word: a text cell is null as a number, ``1.5``
truncates to ``1`` as a whole number, a plain number reads as a date (a plain table whose every column holds one kind
of cell, its field's, is read from one strict load, which types a column by all its cells and refuses a mix). So a
whole number is loaded as a float and
checked whole, a date field without a ``Format`` must hold date cells and a time-of-day field time cells (one with a
``Format`` holds text, parsed as :class:`CsvFile` parses it), and any cell whose text is not a value of its column
raises, naming the sheet's own row, unless it is one of the sheet's words for none (``null_values``: ``#N/A``). An
empty row inside the table raises, unless the format's sheet leaves gaps (``_gaps``: dropped) or numbers its rows by
place (``_blank_rows``: kept, a row of no values); an empty string is no value. A typed load keeps every digit of a
number, which a text load rounds to nine decimals. A header cell may be empty over a real column (a date column under
no label): its field's ``Label`` is ``""``, and the cell must be empty, as fastexcel reports it (it names the column
itself, ``__UNNAMED__n``); columns are matched by position, so a sheet may have several, and a label written twice is
two fields (fastexcel names the second ``Rate_1``). A :class:`PaddedSheet` strips its text cells' spaces before they are
typed (SF Fed's Stata months, ``' 1988m1'``); a :class:`NumberTextSheet` reads a number written as a text cell
from its text (``' 471.21'``), composed after it.
:class:`NotedSheet` is a sheet table with notes under it (a ``Source:`` line, a ``Notes:`` heading and its lines, a
blank row and disclosures): the table ends at its first row with nothing past its first column, and below it only the
first column holds anything, the notes, which are dropped. A :class:`TitledSheet`'s header sits below title rows, each
matching its regex, and a :class:`PreambleSheet`'s below a preamble of any length: its header is the first row whose
first cell is its first label. :class:`BesideSheet` is a sheet table with more beside it (a second list, a note,
work columns): only the table's own columns are loaded, the first ones or those its ``_columns_at`` names (a table
right of another, or across a spacer). A format whose sheet has no header sets ``_header`` false, as a CSV's does:
its rows, under ``header_row``, are read by position.
:class:`DatedSheet` is a table of dated rows that runs while its first column holds date cells (a
week with no value yet is still a row), with notes below it. :class:`SheetsFile` reads every sheet of a workbook, each
by a reader of its own, a sheet a year, the year stated from the sheet's name. A sheet may head its columns from rows
right above its header row (``_above``, how many: a heading carries right over the columns with no cell of their
own), allow blank rows inside its table (``_gaps``, dropped, or ``_blank_rows``, kept as rows of no values where a
table numbers its rows by place), read a cell of only whitespace as empty (``_trim``) and a header cell stripped
(``_strip``); either of the last two reads the header from the sheet's own rows, as written, so a table below blank
rows or after a column the top rows fill is refused, and a heading written twice is twice.

:class:`SheetText` is a sheet's grid of text cells (each cell's text, a number with every digit) and :class:`BiffSheet`
an Excel 3 or 4 workbook's, cut as a :class:`~flatread.core.GridFile` cuts its table. Over a sheet read
long, a :class:`CellHeaderSheet` reads a header of date cells as their text, so a date is a key, and a
:class:`GroupedSheet` reads keys under group headings, the group and the label both keys.

fastexcel's ``dtypes="string"`` load rounds a number cell to 9 decimal places: ``13442.008862616791`` in the XML
comes back as ``13442.008862617`` and ``1e-10`` as ``0`` (measured on fastexcel 0.21.0, the latest, 2026-11-09), so a
decoder that types the text loses the rest. :func:`_text_sheet` loads the sheet a second time as floats and, for each
cell whose text reads as a different number, takes the float's shortest round-trip form instead. Text cells
(``'1.50'`` stays ``1.50``), dates and numbers the text already holds exactly stay as the text load writes them.
"""

import datetime as dt
import functools
import io
import re
import struct
import typing
from collections.abc import Callable, Mapping, Sequence
from typing import Final, Literal

import fastexcel
import msgspec
import polars as pl
import xlrd

from flatread.core import (
    _UNIT,
    File,
    _check_above,
    _Column,
    _first,
    _flagged,
    _moment,
    _none_dtype,
    _repeated,
    _stacked,
    _stated,
    _temporal,
)
from flatread.errors import LayoutError, ReadError
from flatread.found import FoundTable
from flatread.grids import GridFile
from flatread.layout import LongFile

__all__ = [
    "BesideSheet",
    "BiffSheet",
    "CellHeaderSheet",
    "DatedSheet",
    "FoundSheet",
    "GroupedSheet",
    "NotedSheet",
    "NumberTextSheet",
    "OnlySheet",
    "PaddedSheet",
    "PreambleSheet",
    "SheetFile",
    "SheetText",
    "SheetsFile",
    "TitledSheet",
    "sheet_grid",
]

#: The fastexcel type each field scalar's column is loaded as: a whole number as a float, which is then checked whole
#: (fastexcel would truncate ``1.5`` to ``1``), a date or time written as text as text (its ``Format`` parses it).
_SHEET_DTYPES: Final[Mapping[object, fastexcel.DType]] = {
    str: "string",
    int: "float",
    float: "float",
    bool: "boolean",
    dt.date: "date",
    dt.datetime: "datetime",
    dt.time: "datetime",
}


#: A date or time cell as fastexcel writes it as text: a date field's at midnight, a time of day's on Excel's day 0.
_DATE_CELL: Final = r"^\d{4}-\d{2}-\d{2} 00:00:00$"


_TIME_CELL: Final = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?$"


_CLOCK_CELL: Final = r"^1899-12-31 \d{2}:\d{2}:\d{2}(?:\.\d+)?$"


#: Each date or time scalar's cell, as its text is shaped, and what an error calls it.
_MOMENT_CELLS: Final[Mapping[object, tuple[str, str]]] = {
    dt.date: (_DATE_CELL, "date cell"),
    dt.datetime: (_TIME_CELL, "date or time cell"),
    dt.time: (_CLOCK_CELL, "time cell"),
}


#: The type a loaded column is cast to, where fastexcel's is not its field's: a whole number is loaded as a float.
_SHEET_CASTS: Final[Mapping[object, pl.DataType]] = {
    int: pl.Int64(),
    dt.datetime: pl.Datetime("us"),
    dt.time: pl.Time(),
}


#: A boolean cell as fastexcel writes it as text.
_BOOLEAN_CELLS: Final = ("true", "false")


#: The methods a format reads its sheet otherwise by: a format that overrides any reads its sheet by two loads.
_ONE_LOAD_HOOKS: Final = (
    "_read", "_column", "_layout", "_label", "_value", "_end", "_outside", "_span", "_header_cells", "_headings",
    "_used_columns", "_typed", "_load", "_text_grid", "_completed", "_gapless", "_joined_header", "_stated",
    "_sheet_in", "_header_in", "_one_cell",
)  # fmt: skip


def _workbook(body: bytes) -> fastexcel.ExcelReader:
    """Return the workbook *body*, open, or raise ValueError for a body that is no workbook."""
    try:
        return fastexcel.read_excel(body)
    except fastexcel.FastExcelError as e:
        msg = f"the body is not a workbook: {e}"
        raise ReadError(msg) from e


def _of_kind(column: _Column, cells: pl.Series) -> bool:
    """Return whether each of a strict load's *cells* is one *column* holds, as the two loads read its cells."""
    dtype, scalar = cells.dtype, column.scalar
    if column.written is not None or column.flag is not None or scalar is str:
        return dtype == pl.String
    if scalar in {int, float}:
        return dtype == pl.Float64 and (scalar is float or bool((cells == cells.floor()).all()))
    if scalar in _MOMENT_CELLS:
        shape = _MOMENT_CELLS[scalar][0]
        return isinstance(dtype, pl.Datetime) and bool(
            cells.dt.strftime("%Y-%m-%d %H:%M:%S").drop_nulls().str.contains(shape).all()
        )
    return scalar is bool and dtype == pl.Boolean


def _cell_at(frame: pl.DataFrame, row: int, at: int) -> pl.Series:
    """Return *frame*'s cell in its row *row* (0-based) and sheet column *at*: a series of one, null where none."""
    name = str(at)
    return frame[name].slice(row, 1) if name in frame.columns and 0 <= row < frame.height else pl.Series([None])


class SheetFile[RowT: msgspec.Struct](File[RowT]):
    """A clean table on one sheet of a workbook, read by fastexcel with the types its row struct's fields lay out."""

    _by_cell = True

    #: Whether the table opens with a header row: a format whose sheet writes none (GDPNow's ticker map) sets it false,
    #: and its rows, from the one under ``header_row`` (0: the sheet's first), are read by position, its fields' names
    #: their labels.
    _header: typing.ClassVar[bool] = True
    #: Whether an empty row inside the table is a gap, dropped: a format whose sheet leaves one (Cboe's VXO archive,
    #: between two days) sets it; otherwise an empty row raises.
    _gaps: typing.ClassVar[bool] = False
    #: Whether an empty row inside the table is a row of no values, kept: a format numbering its rows by place, blank
    #: ones too (CFTC's Swaps Report tables of 2012 to 2014), sets it.
    _blank_rows: typing.ClassVar[bool] = False
    #: Whether a cell of only whitespace is empty (EIA leaves blank-looking cells), as fastexcel's
    #: ``whitespace_as_null`` reads it. The header is then read as the sheet writes it, so a heading written twice is
    #: twice (fastexcel would name the second ``East_1``).
    _trim: typing.ClassVar[bool] = False
    #: Whether a header cell is read stripped of its whitespace (EIA pads some headings), read as :attr:`_trim`'s is.
    _strip: typing.ClassVar[bool] = False
    #: How many rows right over the header row head its columns too (EIA's ``Sourcekey`` row over its descriptions): a
    #: cell heads its own column and every column to its right with no cell of its own. Each column's header is then
    #: its cells top to bottom, the unit separator (U+001F) between, read as written (stripped; a blank cell is empty),
    #: and a reader that lays out the key columns (:class:`LongFile`) takes them from it.
    _above: typing.ClassVar[int] = 0

    def __init__(self, row: type[RowT], *, sheet: str, header_row: int = 1, null_values: Sequence[str] = ()) -> None:
        """Lay out a table of *row* on the sheet named *sheet*, under its header in sheet row *header_row* (Excel's).

        Each field names its column with a :class:`~flatread.fields.Label`. A date or time field without a
        :class:`~flatread.fields.Format` holds the sheet's date cells; with one, text written in that
        format. *null_values* are the sheet's text for no value (``#N/A``, ``NA``). Errors name the sheet's own rows.
        Raises ``ValueError`` where the :attr:`_above` rows do not fit over *header_row*.
        """
        super().__init__(row, row_one=header_row + 1)
        _check_above(self._above, header_row)
        self._sheet = sheet
        self._header_row = header_row
        self._null_values = list(null_values)

    @typing.override
    def _joined_header(self) -> bool:
        return bool(self._above)

    #: The workbook open for one body and its sheet's text grid, on a reader made for that body (:meth:`_placed`);
    #: ``None`` on a shared reader, whose :meth:`_read` opens the body itself.
    _opened: fastexcel.ExcelReader | None = None
    _opened_grid: pl.DataFrame | None = None

    @typing.override
    def _read(self, body: bytes) -> pl.DataFrame:
        reader = self._opened or _workbook(body)
        if self._opened_grid is None and (frame := self._one_load(reader)) is not None:
            return frame
        # Two loads: the sheet's every cell as text (the grid), which says what a cell holds and serves all above and
        # around the table, and the table's cells typed, which fastexcel coerces without a word (a text cell is null as
        # a number). shortcut: a text field's number cell reads as fastexcel's text of it, rounded to 9 decimals; no
        # clean table keeps such numbers as text, a layout that does needs SheetText.
        grid = self._opened_grid if self._opened_grid is not None else self._text_grid(reader, self._sheet)
        if self._header and grid.height < self._header_row:
            msg = f"the {self._sheet!r} sheet ends at row {grid.height}, before its header row {self._header_row}"
            raise ReadError(msg)
        span = self._span(grid)
        used = self._used_columns()
        table = span if used is None else [str(at) for at in used]
        if any(name not in span for name in table):
            msg = f"the {self._sheet!r} sheet has no column {used!r} of its table"
            raise ReadError(msg)
        written = bool(self._above) or self._trim or self._strip  # the header as the sheet's own row writes it
        if written and used is None and span and span[0] != grid.columns[0]:
            # Headings read from the sheet's first column: a table that starts after cells of its top rows is refused.
            msg = (
                f"the {self._sheet!r} sheet's header row holds {grid.columns.index(span[-1]) + 1} columns,"
                f" not the {len(span)} loaded"
            )
            raise ReadError(msg)
        cells = grid.select(table)
        if self._trim:  # as fastexcel's whitespace_as_null: a cell of only whitespace is empty
            cells = cells.with_columns(pl.when(pl.col(pl.String).str.strip_chars() != "").then(pl.col(pl.String)))
        # A sheet without a header: no row is one, and its rows begin under header_row.
        named = (
            (self._headings(cells) if written else self._header_cells(cells)) if self._header else [""] * cells.width
        )
        kept = [at for at, label in enumerate(named) if label not in self._unread]
        header = [named[at] for at in kept]
        columns = self._columns if (layout := self._layout(header)) is None else layout
        # The numbered field holds the sheet's row, under no column.
        columns = [column for column in columns if column.field != self._numbered and column.cell is None]
        # The header's names number a label's repeats by their place (``Rate``, ``Rate_1``), as fastexcel names them.
        labels = (
            [column.label for column in columns]
            if written
            else _repeated([column.label for column in columns], "{label}_{n}", 1)
        )
        if not self._header and len(header) > len(columns):
            msg = f"the {self._sheet!r} sheet's table is {len(header)} columns wide, not {len(columns)}"
            raise ReadError(msg)
        # A header-less sheet's last columns, empty all down, are no columns of its (calamine trims them): no values.
        columns, absent = columns[: len(header)], columns[len(header) :]
        if self._header and header != labels:
            msg = f"the {self._sheet!r} sheet's header (row {self._header_row}) {header} is not {labels}"
            raise ReadError(msg)
        text = cells.slice(self._header_row).select(pl.nth(kept))
        text.columns = [column.field for column in columns]
        # By position, not label: an empty header cell is no name. A position is the sheet's column, which fastexcel
        # keys a dtype by: a table may open past column A.
        placed = dict(zip(kept, columns, strict=True))
        typed, stated = self._typed(reader, grid, {int(name): placed.get(at) for at, name in enumerate(table)}, used)
        typed = typed.select(table).select(pl.nth(kept))
        typed.columns = text.columns
        # A cell holds no value when it is empty, an empty string or one of the sheet's words for none.
        text = text.with_columns(pl.when(~pl.col(pl.String).is_in(["", *self._null_values])).then(pl.col(pl.String)))
        end = self._end(text)
        self._outside(grid, end)
        text, typed = text.head(end), typed.head(end)
        empty = text.select(pl.all_horizontal(pl.all().is_null())).to_series()
        if not (self._gaps or self._blank_rows) and (at := _first(empty, self._row_one)) is not None:
            msg = f"the {self._sheet!r} sheet's row {at} is empty, and a table's rows run to the sheet's end"
            raise ReadError(msg)
        # Each column's cells under its label, which errors name, or its field's name where its header cell is empty.
        frame = pl.DataFrame(
            [
                self._value(column, text[column.field].alias(column.label or column.field), typed[column.field])
                for column in columns
            ]
        )
        frame = self._completed(frame, absent, stated)
        # Dropped once typed and numbered, so an error and a numbered row still name the sheet's own row.
        return self._gapless(frame, empty) if self._gaps else frame

    def _one_loadable(self) -> bool:
        """Return whether a table may be read from one strict load: a plain table, by none of the hooks or switches."""
        labels = [column.label for column in self._columns if column.field != self._numbered]
        return (
            self._header
            and not (self._above or self._trim or self._strip or self._gaps or self._blank_rows)
            and not (self._null_values or self._unread or self._blank_columns)
            and all(column.cell is None for column in self._columns)
            and "" not in labels
            and len(set(labels)) == len(labels)
            and all(getattr(type(self), hook) is getattr(SheetFile, hook) for hook in _ONE_LOAD_HOOKS)
        )

    def _one_load(self, reader: fastexcel.ExcelReader) -> pl.DataFrame | None:
        """Return the table read from one load, or ``None`` where the two loads must read it: a read's fast path.

        fastexcel's strict load types each column by every one of its cells and raises for a column that mixes kinds
        (a ``.`` among numbers), so a column of one kind, its field's, needs no text load to say what each cell holds.
        Anything else (a mixed column, a header or a cell the two loads refuse) is ``None``, and the two loads raise
        their own error. A sheet that holds tokens for none (``null_values``) mixes kinds and goes to them directly.
        """
        if not self._one_loadable() or self._sheet not in reader.sheet_names:
            return None
        columns = [column for column in self._columns if column.field != self._numbered]
        try:
            table = reader.load_sheet(
                self._sheet, header_row=self._header_row - 1, dtype_coercion="strict", schema_sample_rows=None
            ).to_polars()
        except fastexcel.FastExcelError:
            return None
        # fastexcel names a column by its header cell, a repeat by its place (Rate_1) and an empty or date cell
        # __UNNAMED__n, so a stray cell beside the table or a header that is not the labels is a mismatch.
        if table.columns != [column.label for column in columns]:
            return None
        values = [self._one_kind(column, table[column.label]) for column in columns]
        if any(value is None for value in values):
            return None
        frame = pl.DataFrame([value for value in values if value is not None])
        if frame.select(pl.all_horizontal(pl.all().is_null()).any()).item():  # an empty row, which the loads refuse
            return None
        return self._completed(frame, [], [])

    def _one_kind(self, column: _Column, cells: pl.Series) -> pl.Series | None:
        """Return *column*'s values from its strictly loaded *cells*, or ``None`` where they are not all its kind."""
        if cells.null_count() == cells.len():
            return pl.Series(column.field, [None] * cells.len(), _none_dtype(column.scalar))
        if not _of_kind(column, cells):
            return None
        if column.written is not None or column.flag is not None or column.scalar is str:
            text = pl.select(pl.when(cells != "").then(cells)).to_series().alias(column.label)
            try:
                return self._value(column, text, text)
            except ValueError:  # a cell its Format or Flag refuses: the two loads name it
                return None
        dtype = pl.Date() if column.scalar is dt.date else _SHEET_CASTS.get(column.scalar, cells.dtype)
        return cells.cast(dtype).alias(column.field)

    def _span(self, grid: pl.DataFrame) -> list[str]:
        """Return the *grid*'s columns the table spans, as fastexcel loads a table under its header.

        They run from the first to the last column holding a cell from the header row down (a title's column left of
        the table is none of them); a sheet without a header spans every column.
        """
        held = grid.slice(self._header_row - 1).select(pl.all().is_not_null().any()).row(0) if grid.width else ()
        if not self._header or not (filled := [at for at, cell in enumerate(held) if cell]):
            return grid.columns
        return grid.columns[filled[0] : filled[-1] + 1]

    def _typed(
        self,
        reader: fastexcel.ExcelReader,
        grid: pl.DataFrame,
        read: Mapping[int, _Column | None],
        used: list[int] | None,
    ) -> tuple[pl.DataFrame, list[pl.Expr]]:
        """Load the table's cells typed, the rows under its header, and read its ``Cell`` fields: a read's second load.

        *read* maps each of the table's sheet columns to the column it reads there (``None``: none); the frame holds
        every column loaded, named by place. A Cell typed by its cell (a date's) is loaded with the table, from its row,
        where the table types its column alike or reads none there. Those over columns the table types otherwise (a
        date over numbers) share one load more, each of its column, and a second of them over one column of another
        type a load of its own. Any other Cell is its text, the *grid*'s.
        """
        dtypes: fastexcel.DTypeMap = {
            at: "string" if column is None or column.written is not None else _SHEET_DTYPES[column.scalar]
            for at, column in read.items()
        }
        claimed: dict[int, fastexcel.DType] = {}
        aside: dict[int, fastexcel.DType] = {}
        where: dict[str, Literal["table", "aside", "own"]] = {}  # the load each typed Cell's field is read from
        for column in self._columns:
            if column.cell is None or column.written is not None or column.scalar is str:
                continue
            at, dtype = column.cell[1], _SHEET_DTYPES[column.scalar]
            if str(at) not in grid.columns:  # a cell past the sheet's columns is empty: its text says so
                continue
            held = dtypes[at] if read.get(at) is not None else claimed.setdefault(at, dtype)
            where[column.field] = (
                "table" if held == dtype else "aside" if aside.setdefault(at, dtype) == dtype else "own"
            )

        def first(load: str) -> int:
            return min(
                column.cell[0] - 1 for column in self._columns if column.cell and where.get(column.field) == load
            )

        start = min(self._header_row, first("table")) if "table" in where.values() else self._header_row
        loaded = None if used is None else [*used, *sorted(claimed.keys() - set(used))]
        typed = self._load(reader, self._sheet, skip_rows=start, dtypes=dtypes | claimed, use_columns=loaded)
        beside, top = pl.DataFrame(), first("aside") if aside else 0
        if aside:
            beside = self._load(
                reader, self._sheet, skip_rows=top, dtypes=dict(aside.items()), use_columns=sorted(aside)
            )
        stated = []
        for column in self._columns:
            if column.cell is None:
                continue
            row, at = column.cell
            text = _cell_at(grid, row - 1, at)
            match where.get(column.field):
                case "table":
                    value = _cell_at(typed, row - 1 - start, at)
                case "aside":
                    value = _cell_at(beside, row - 1 - top, at)
                case "own":
                    value = self._one_cell(reader, row, at, {at: _SHEET_DTYPES[column.scalar]})
                case _:
                    value = text
            stated.append(self._stated(column, text, value))
        return typed.slice(self._header_row - start), stated

    def _frame_on(self, reader: fastexcel.ExcelReader, body: bytes) -> pl.DataFrame:
        """Read *body*'s table as :meth:`frame` does, from *reader*, the workbook open for it: one open, many sheets."""
        return self._placed(reader).frame(body)

    def _placed(self, reader: fastexcel.ExcelReader) -> typing.Self:
        """Return a reader of the body open in *reader*: its sheet :meth:`_sheet_in`'s, its header :meth:`_header_in`'s.

        The shared reader is left as it is, read by any thread; the one returned reads the open workbook and its grid.
        """
        sheet = self._sheet_in(reader)
        grid = self._text_grid(reader, sheet)
        row = self._header_in(grid)
        placed = object.__new__(type(self))
        vars(placed).update(
            vars(self), _opened=reader, _opened_grid=grid, _sheet=sheet, _header_row=row, _row_one=row + 1
        )
        return placed

    def _sheet_in(self, reader: fastexcel.ExcelReader) -> str:
        """Return the name of the table's sheet in the workbook *reader*: the format's."""
        del reader
        return self._sheet

    def _header_in(self, grid: pl.DataFrame) -> int:
        """Return the sheet row of the table's header in the sheet's *grid*: the format's."""
        del grid
        return self._header_row

    def _text_grid(self, reader: fastexcel.ExcelReader, sheet: str) -> pl.DataFrame:
        """Return *sheet*'s every cell as text, a row per sheet row from row 1, a column per sheet column by place."""
        if sheet not in reader.sheet_names:
            msg = f"the workbook has no sheet {sheet!r}; its sheets are {reader.sheet_names!r}"
            raise ReadError(msg)
        return self._load(reader, sheet, skip_rows=0, dtypes="string", use_columns=None)

    def _completed(self, frame: pl.DataFrame, absent: list[_Column], stated: list[pl.Expr]) -> pl.DataFrame:
        """Return the table's typed *frame* with the fields no column holds: its row, the *absent*, the *stated*."""
        if self._numbered is not None:
            numbers = pl.int_range(self._row_one, self._row_one + frame.height, dtype=pl.Int64, eager=True)
            frame = frame.insert_column(0, numbers.alias(self._numbered))
        frame = frame.with_columns(pl.lit(None, _none_dtype(column.scalar)).alias(column.field) for column in absent)
        frame = frame.with_columns(stated)
        # A wide table: its fields in the struct's order.
        return frame.select(self._fields) if set(frame.columns) == set(self._fields) else frame

    def _gapless(self, frame: pl.DataFrame, empty: pl.Series) -> pl.DataFrame:
        """Return *frame* without the rows *empty* marks: a format that needs the sheet's row numbers keeps them."""
        return frame.filter(~empty)

    def _load(
        self,
        reader: fastexcel.ExcelReader,
        sheet: str,
        *,
        skip_rows: int,
        dtypes: fastexcel.DType | fastexcel.DTypeMap,
        use_columns: list[int] | None,
    ) -> pl.DataFrame:
        """Load *sheet* by fastexcel from sheet row *skip_rows* + 1, its columns named by place (``"0"`` is ``A``).

        fastexcel's own failure is a ValueError.
        """
        try:
            loaded = reader.load_sheet(
                sheet, header_row=None, skip_rows=skip_rows, dtypes=dtypes, use_columns=use_columns
            )
            frame = loaded.to_polars()  # fastexcel loads lazily
        except fastexcel.FastExcelError as e:  # a sheet fastexcel cannot read (a cut one)
            msg = f"the {sheet!r} sheet cannot be loaded as the table: {e}"
            raise ReadError(msg) from e
        frame.columns = [str(column.absolute_index) for column in loaded.selected_columns]
        return frame

    @typing.override
    def _label(self, name: str) -> str | None:
        return name if not self._header or name == self._numbered else super()._label(name)

    def _header_cells(self, cells: pl.DataFrame) -> list[str]:
        """Return the header's names, from the table's *cells*: its row's cells as fastexcel names its columns.

        A date, time or boolean cell names no column, as an empty one does (``""``), and a label's repeats are numbered
        by their place (``Rate``, ``Rate_1``). A date or time cell's text is its shape (``2026-10-29 00:00:00``), which
        text written so shares: it names nothing too.
        """
        return _repeated(
            [
                "" if cell is None or cell in _BOOLEAN_CELLS or re.fullmatch(_TIME_CELL, cell) else cell
                for cell in cells.row(self._header_row - 1)
            ],
            "{label}_{n}",
            1,
        )

    def _stated(self, column: _Column, text: pl.Series, typed: pl.Series) -> pl.Expr:
        """Return the value *column*'s cell states, its *text* and its *typed* cell, read as a cell of its column is."""
        text = text.cast(pl.String).replace(["", *self._null_values], None).alias(column.label)
        try:
            value = self._value(column, text, typed)
        except ValueError as e:
            msg = f"the {self._sheet!r} sheet's cell {column.label!r} is not its {column.field}: {e}"
            raise ReadError(msg) from e
        # An empty cell (none past the sheet's columns) loads untyped: no value of the field's type.
        dtype = _none_dtype(column.scalar) if value.dtype == pl.Null else value.dtype
        return pl.lit(value[0], dtype).alias(column.field)

    def _one_cell(
        self, reader: fastexcel.ExcelReader, row: int, at: int, dtypes: fastexcel.DType | fastexcel.DTypeMap
    ) -> pl.Series:
        """Return the cell at sheet *row* and column *at*, loaded with *dtypes*: a series of one, null where empty."""
        loaded = reader.load_sheet(
            self._sheet, header_row=None, skip_rows=row - 1, n_rows=1, use_columns=[at], dtypes=dtypes
        ).to_polars()
        return loaded.to_series(0) if loaded.height else pl.Series([None], dtype=pl.String)

    def _used_columns(self) -> list[int] | None:
        """Return the sheet's columns the table is read from, 0-based (``A`` is 0): ``None``, all."""
        return None

    def _end(self, text: pl.DataFrame) -> int:
        """Return how many of *text*'s rows, the sheet's under its header, are the table's: all of them."""
        return text.height

    def _outside(self, grid: pl.DataFrame, end: int) -> None:
        """Refuse what the sheet's *grid* holds outside the table, above its header or past its first *end* rows: none.

        The grid is every cell as text, a row per sheet row from row 1, its columns named by place (``"0"`` is ``A``).
        """

    def _headings(self, cells: pl.DataFrame) -> list[str]:
        """Return each column's header from the table's *cells* over it and in its header row, U+001F between them."""
        # A cell is as written, or stripped (always where rows head the columns, which join as a stack of cells).
        strip = self._strip or bool(self._above)
        return _stacked(
            [
                [None if cell is None else (cell.strip() if strip else cell) or None for cell in cells.row(row - 1)]
                for row in range(self._header_row - self._above, self._header_row + 1)
            ]
        )

    def _value(self, column: _Column, text: pl.Series, typed: pl.Series) -> pl.Series:
        """Return *column*'s values, refusing a cell that is not one of its type, its row the sheet's own."""
        if column.written is not None:
            return _moment(text, _temporal(column.scalar, column.written), column.written, self._row_one).alias(
                column.field
            )
        if column.scalar is str:
            return text.alias(column.field)
        if column.scalar in _MOMENT_CELLS:
            # A text cell written like a date cell has the shape, and fastexcel reads it as no date.
            shape, what = _MOMENT_CELLS[column.scalar]
            wrong = ~text.str.contains(shape) | typed.is_null()
        elif column.flag is not None:
            return _flagged(text, column.flag, self._row_one).alias(column.field)
        elif column.scalar is bool:
            # fastexcel reads a number cell as a boolean (1 is true); a boolean cell's text is its word.
            wrong, what = ~text.is_in(_BOOLEAN_CELLS), "boolean cell"
        else:
            # ... and a boolean cell as a number (true is 1.0).
            wrong = typed.is_null() | text.is_in(_BOOLEAN_CELLS)
            if column.scalar is int:
                wrong |= typed != typed.floor()
            what = "whole number" if column.scalar is int else "number"
        if (at := _first((text.is_not_null() & wrong).fill_null(value=False), self._row_one)) is not None:
            msg = f"the {text.name!r} cell {text[at - self._row_one]!r} is not a {what}: row {at}"
            raise ReadError(msg)
        values = pl.select(pl.when(text.is_not_null()).then(typed)).to_series()
        if (cast := _SHEET_CASTS.get(column.scalar)) is not None:
            values = values.cast(cast)
        return values.alias(column.field)


class PaddedSheet[RowT: msgspec.Struct](SheetFile[RowT]):
    """A sheet whose text cells are padded with spaces (SF Fed's Stata months, ``' 1988m1'``), stripped before typed.

    A cell stripped to one of the sheet's words for none (``'  NA'``) holds no value.
    """

    @typing.override
    def _value(self, column: _Column, text: pl.Series, typed: pl.Series) -> pl.Series:
        stripped = text.str.strip_chars(" ")
        return super()._value(column, stripped.replace(["", *self._null_values], None), typed)


class BesideSheet[RowT: msgspec.Struct](SheetFile[RowT]):
    """A sheet table with more beside it (a second list, a note, a work column): only the table's columns are read.

    They are the sheet's first columns (``A`` on), as many as the struct's fields, unless :attr:`_columns_at` names
    them: the table's own columns, 0-based (``A`` is 0), so a table may start right of others (a column offset) or skip
    a spacer. Nothing else on the sheet is loaded. Compose it with :class:`NotedSheet` for a table that
    also ends above the sheet's last row (``class _Updates(BesideSheet[R], NotedSheet[R])``).
    """

    #: The table's columns on the sheet, in its fields' order; ``None`` takes the first, one a field.
    _columns_at: typing.ClassVar[Sequence[int] | None] = None

    @typing.override
    def _used_columns(self) -> list[int] | None:
        # As many as the header's check needs: a wide table's columns are its fields, a long table's its keys too.
        read = [column for column in self._columns if column.cell is None and column.field != self._numbered]
        return list(range(len(read)) if self._columns_at is None else self._columns_at)


class CellHeaderSheet[RowT: msgspec.Struct](SheetFile[RowT]):
    """A sheet whose header writes date cells (a column a day), each read as its text (``2026-10-29 00:00:00``).

    :class:`SheetFile` names a header's columns as fastexcel does, a date, time or boolean cell naming none; here
    every header cell is its text, as written. Read long, a date header is its key
    (``class _Dated(LongFile[R], CellHeaderSheet[R])``, ``_keys`` matching the date cell's text).
    """

    @typing.override
    def _header_cells(self, cells: pl.DataFrame) -> list[str]:
        return ["" if cell is None else cell for cell in cells.row(self._header_row - 1)]


class NumberTextSheet[RowT: msgspec.Struct](SheetFile[RowT]):
    """A sheet whose numbers are written as text as well as in number cells (SF Fed's ``' 1.03'`` beside ``0.60``).

    A number field's text cell, which fastexcel's typed load reads as no number, is read as its text's number
    of the field's own type, strictly (an int field takes no ``1e3`` or ``1.0``); a number cell keeps every digit it
    holds. Composed after a piece that cleans the text
    (``class _Sheet(PaddedSheet[RowT], NumberTextSheet[RowT])``).
    """

    @typing.override
    def _value(self, column: _Column, text: pl.Series, typed: pl.Series) -> pl.Series:
        if column.scalar in {int, float} and column.flag is None and column.written is None:
            # Read as the field's own type, as a CSV's text is: an int field's text that is no whole number
            # (``1e3``, ``1.0``, ``inf``, past 2^63) stays no value, and is refused as such; a float's NaN and
            # infinities are refused downstream as any float that is not finite is.
            if column.scalar is float:
                return super()._value(column, text, typed.fill_null(text.cast(pl.Float64, strict=False)))
            # An int is loaded as a float, which holds 2^53 exactly; the text's own digits are kept beside it.
            whole = text.cast(pl.Int64, strict=False)
            checked = super()._value(column, text, typed.fill_null(whole.cast(pl.Float64)))
            return pl.select(pl.when(typed.is_null()).then(whole).otherwise(checked)).to_series().alias(column.field)
        return super()._value(column, text, typed)


class NotedSheet[RowT: msgspec.Struct](SheetFile[RowT]):
    """A sheet table with notes under it: it ends at its first row with nothing past its first column.

    That row and those below it are the notes (a ``Source:`` line, a ``Notes:`` heading, a blank row and disclosures),
    dropped; a cell past the first column among them is a row the table lost, and raises, naming the sheet's row.
    """

    @typing.override
    def _end(self, text: pl.DataFrame) -> int:
        bare = text.select(pl.all_horizontal(pl.col(text.columns[1:]).is_null())).to_series()
        end = int(bare.arg_true()[0]) if bare.any() else text.height
        if (at := _first(~bare.slice(end), self._row_one + end)) is not None:
            msg = (
                f"the {self._sheet!r} sheet's row {at} holds a cell past its first column under its table, which ends"
                f" at row {self._row_one + end}: only notes go there"
            )
            raise ReadError(msg)
        return end


class DatedSheet[RowT: msgspec.Struct](SheetFile[RowT]):
    """A sheet table of dated rows, with notes under it: it runs while its first column holds date cells.

    A row whose values are all empty is still a row. Below the table only the first column holds anything, the notes,
    which are dropped; a cell past it among them is a row the table lost, and raises, naming the sheet's row.
    """

    @typing.override
    def _end(self, text: pl.DataFrame) -> int:
        dated = text.select(pl.col(text.columns[0]).str.contains(_DATE_CELL).fill_null(value=False)).to_series()
        end = text.height if dated.all() else int(dated.arg_min() or 0)
        below = text.slice(end).select(pl.any_horizontal(pl.col(text.columns[1:]).is_not_null())).to_series()
        if (at := _first(below, self._row_one + end)) is not None:
            msg = (
                f"the {self._sheet!r} sheet's row {at} holds a cell under its table,"
                " where only notes in its first column go"
            )
            raise ReadError(msg)
        return end


class SheetsFile[RowT: msgspec.Struct](File[RowT]):
    r"""Every sheet of a workbook, each read as its table by a reader of its own, one after another: a sheet a year.

    A format gives the regex every sheet's name matches (a sheet that does not raises, and a workbook needs one) and
    *each*, which builds the reader of one sheet from its name. That reader reads the row struct's fields but the ones
    the regex's groups name: a group named for a field states it on that sheet's rows (``ngsstats (?P<year>\d{4}) .*``),
    a field no column holds, which takes no ``Label``.
    """

    def __init__(
        self,
        row: type[RowT],
        *,
        sheets: re.Pattern[str],
        each: Callable[[str], SheetFile[typing.Any] | SheetText[typing.Any]],
    ) -> None:
        """Lay out the tables of *row*, one sheet each (see the class)."""
        self._sheets = sheets
        self._each = each
        super().__init__(row)
        self._stated = [column for column in self._columns if column.field in sheets.groupindex]

    @typing.override
    def _label(self, name: str) -> str | None:
        return name

    @typing.override
    def _read(self, body: bytes) -> pl.DataFrame:
        reader = _workbook(body)
        names = reader.sheet_names
        groups = []
        for name in names:
            if (match := self._sheets.fullmatch(name)) is None:
                msg = f"the workbook's sheet {name!r} is not one of {self._sheets.pattern!r}"
                raise ReadError(msg)
            groups.append(match.groupdict())
        frames = []
        for name, group in zip(names, groups, strict=True):
            try:
                frame = self._each(name)._frame_on(reader, body)  # noqa: SLF001 (a sheet reader of its own)
            except ValueError as e:
                msg = f"the {name!r} sheet: {e}"
                # A sheet's reader is built here too: a misdeclared one stays a LayoutError, an empty sheet empty.
                raise (type(e) if isinstance(e, ReadError | LayoutError) else ReadError)(msg) from e
            states = [
                _stated(column, group[column.field], frame.height, f"{name!r} sheet's name") for column in self._stated
            ]
            frames.append(frame.with_columns(states).select(self._fields))
        return pl.concat(frames)


class TitledSheet[RowT: msgspec.Struct](SheetFile[RowT]):
    """A sheet table below title rows (a title, a note, a blank row): each row above its header is one of its titles.

    A format subclasses it and names them: :attr:`_titles`, a regex per row above the header, in order, each matching
    the row's text, its cells joined by tabs up to its last filled one (an empty row is ``re.compile("")``).
    """

    #: The regexes the rows above the header match, one a row; each format's subclass (or its constructor) sets them.
    _titles: Sequence[re.Pattern[str]]

    @typing.override
    def _outside(self, grid: pl.DataFrame, end: int) -> None:
        super()._outside(grid, end)
        rows = [
            "\t".join(cell or "" for cell in row).rstrip("\t") for row in grid.slice(0, self._header_row - 1).rows()
        ]
        if len(rows) != len(self._titles):
            msg = f"the {self._sheet!r} sheet has {len(rows)} rows above its header, not its {len(self._titles)} titles"
            raise ReadError(msg)
        for number, (row, title) in enumerate(zip(rows, self._titles, strict=True), 1):
            if not title.fullmatch(row):
                msg = f"the {self._sheet!r} sheet's row {number} {row[:200]!r} is not its title {title.pattern!r}"
                raise ReadError(msg)


class PreambleSheet[RowT: msgspec.Struct](SheetFile[RowT]):
    """A sheet table below a preamble of any length: its header is the first row whose first cell is its first label.

    The rows above it (a banner, a link, prose) are not read. The header row is found in each body (two workbooks of
    one layout open it on different rows), and errors name the sheet's own rows.
    """

    @typing.override
    def frame(self, body: bytes) -> pl.DataFrame:
        return super(PreambleSheet, self._placed(self._opened or _workbook(body))).frame(body)

    @typing.override
    def _header_in(self, grid: pl.DataFrame) -> int:
        """Return the sheet row of the header: the first whose first cell is the first column's label."""
        label = self._columns[0].label
        # The sheet's rows from its first, and its first filled column: a table may open past column A.
        if not grid.width or (at := _first(grid.to_series(0) == label)) is None:
            msg = f"the {self._sheet!r} sheet has no row whose first cell is {label!r}, its header's"
            raise ReadError(msg)
        return at


class OnlySheet[RowT: msgspec.Struct](SheetFile[RowT]):
    """A workbook of one sheet whose name varies (a workbook per variable, the sheet named for it): that sheet."""

    def __init__(self, row: type[RowT], *, header_row: int = 1, null_values: Sequence[str] = ()) -> None:
        """Lay out a table of *row* on the workbook's one sheet, under its header in sheet row *header_row*."""
        super().__init__(row, sheet="", header_row=header_row, null_values=null_values)

    @typing.override
    def frame(self, body: bytes) -> pl.DataFrame:
        return super(OnlySheet, self._placed(self._opened or _workbook(body))).frame(body)

    @typing.override
    def _sheet_in(self, reader: fastexcel.ExcelReader) -> str:
        if len(names := reader.sheet_names) != 1:
            msg = f"the workbook holds the sheets {names!r}, not one"
            raise ReadError(msg)
        return names[0]


def sheet_grid(body: bytes, sheet: str | None = None, *, null_values: Sequence[str] = ()) -> pl.DataFrame:
    """Return sheet *sheet* of the workbook *body* (``None``: its one sheet, whatever its name) as text cells.

    The grid :class:`SheetText` cuts: a row per sheet row from row 1, a column per sheet column from the first filled
    one, named ``"0"``, ``"1"``, ...; a number with every digit it holds (fastexcel's text load rounds to nine
    decimals), a blank cell or one of *null_values* no value. A body that is no workbook, a workbook without *sheet*,
    or one of several sheets when *sheet* is ``None``, raises ValueError.

    Example: ``docs/examples/sheet_text.txt``.
    """
    reader = _workbook(body)
    return _text_grid(reader, _sheet_named(reader, sheet), null_values)[1]


def _sheet_named(reader: fastexcel.ExcelReader, sheet: str | None) -> str:
    """Return *sheet*, or for ``None`` the name of *reader*'s one sheet; raise ValueError for a workbook of several."""
    if sheet is None:
        if len(reader.sheet_names) != 1:
            msg = f"the workbook holds the sheets {reader.sheet_names!r}, not one"
            raise ReadError(msg)
        return reader.sheet_names[0]
    return sheet


def _text_grid(reader: fastexcel.ExcelReader, sheet: str, null_values: Sequence[str]) -> tuple[int, pl.DataFrame]:
    """Return *sheet* of *reader* as :func:`sheet_grid` lays it out, and the sheet column (``A`` is 0) of its first."""
    if sheet not in reader.sheet_names:
        msg = f"the workbook has no sheet {sheet!r}; its sheets are {reader.sheet_names!r}"
        raise ReadError(msg)
    try:
        text = _text_sheet(reader, sheet)
    except fastexcel.FastExcelError as e:
        msg = f"the workbook's {sheet!r} sheet will not load: {e}"
        raise ReadError(msg) from e
    first = int(text.columns[0].removeprefix("__UNNAMED__")) if text.width else 0
    text.columns = [str(at) for at in range(text.width)]
    cell = pl.col(pl.String)
    return first, text.with_columns(pl.when((cell.str.strip_chars() != "") & ~cell.is_in(null_values)).then(cell))


class SheetText[RowT: msgspec.Struct](GridFile[RowT]):
    """A sheet too irregular for :class:`SheetFile` (blocks, summary rows, spacer columns): its cells as text, cut.

    Its grid is :func:`sheet_grid`'s: every cell of the sheet as text, a number with every digit it holds, from sheet
    row 1 and the sheet's first filled column, a column per sheet column; :class:`GridFile` cuts it. *sheet* ``None``
    reads the workbook's one sheet, whatever its name.
    """

    #: The workbook :meth:`_frame_on` was handed, open: the grid is read from it, not from the body.
    _opened: fastexcel.ExcelReader | None = None

    def __init__(
        self, row: type[RowT], *, sheet: str | None = None, header_row: int = 1, null_values: Sequence[str] = ()
    ) -> None:
        """Lay out a table of *row* on *sheet* (``None``: the workbook's only sheet), under its header *header_row*."""
        super().__init__(row, header_row=header_row, null_values=null_values)
        self._sheet = sheet

    def _frame_on(self, reader: fastexcel.ExcelReader, body: bytes) -> pl.DataFrame:
        """Read *body*'s table as :meth:`frame` does, from *reader*, the workbook open for it: one open, many sheets."""
        placed = object.__new__(type(self))
        vars(placed).update(vars(self), _opened=reader)
        return placed.frame(body)

    @typing.override
    def _body_grid(self, body: bytes) -> pl.DataFrame:
        reader = self._opened or _workbook(body)
        return self._grid(reader, _sheet_named(reader, self._sheet))

    _workbook = staticmethod(_workbook)

    def _grid(self, reader: fastexcel.ExcelReader, sheet: str) -> pl.DataFrame:
        """Return *sheet*'s every cell as text, a row per sheet row from row 1, a column per sheet column."""
        return self._sheet_grid(reader, sheet)[1]

    def _sheet_grid(self, reader: fastexcel.ExcelReader, sheet: str) -> tuple[int, pl.DataFrame]:
        """Return *sheet*'s grid (:meth:`_grid`'s) and the sheet column (``A`` is 0) of its column ``0``, its first."""
        return _text_grid(reader, sheet, self._null_values)


#: The BOF record opening a BIFF3 and a BIFF4 worksheet (Excel 3 and 4), which calamine cannot open.
_BIFF_BOFS: Final = (0x0209, 0x0409)


class BiffSheet[RowT: msgspec.Struct](SheetText[RowT]):
    """A :class:`SheetText` on a BIFF3 or BIFF4 workbook, which calamine cannot open: xlrd reads its grid.

    The grid is the one :class:`SheetText` cuts, each cell as fastexcel writes it as text: a number its every digit,
    a date ``YYYY-MM-DD HH:MM:SS``, a boolean ``true`` or ``false``, an error its code (``#N/A``, one of the sheet's
    *null_values* where it means none). A body that is neither, or one xlrd cannot read (cut, corrupt), raises.
    """

    @typing.override
    def _body_grid(self, body: bytes) -> pl.DataFrame:
        return self._biff_grid(body)

    def _biff_grid(self, body: bytes) -> pl.DataFrame:
        """Return the sheet's grid as :meth:`SheetText._grid` does: a column per sheet column from its first filled one.

        Raises ValueError for a body that is no BIFF3 or BIFF4 workbook, one xlrd cannot read, or no such sheet.
        """
        if int.from_bytes(body[:2], "little") not in _BIFF_BOFS:
            msg = f"the body is not a BIFF3 or BIFF4 workbook: it opens {body[:4]!r}"
            raise ReadError(msg)
        try:
            # A workbook with no CODEPAGE record reads its text as latin-1, as Excel did; xlrd's note of it is dropped.
            book = xlrd.open_workbook(file_contents=body, logfile=io.StringIO())
        # xlrd's checks of a corrupt record are its own error, struct's, a lookup's, a decode's or an assert.
        except (xlrd.XLRDError, struct.error, LookupError, ValueError, AssertionError) as e:
            msg = f"the body is not a readable BIFF3 or BIFF4 workbook: {e!r}"
            raise ReadError(msg) from e
        names = book.sheet_names()
        name = self._sheet if self._sheet is not None else names[0] if len(names) == 1 else None
        if name not in names:
            msg = f"the workbook holds the sheets {names!r}, not {'one' if self._sheet is None else repr(self._sheet)}"
            raise ReadError(msg)
        sheet = book.sheet_by_name(name)
        empty = {xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK}
        first = next((at for at in range(sheet.ncols) if set(sheet.col_types(at)) - empty), sheet.ncols)
        text = functools.partial(_biff_text, book.datemode)
        columns = {
            str(at - first): [
                None if cell is None or not cell.strip() or cell in self._null_values else cell
                for cell in map(text, sheet.col_types(at), sheet.col_values(at), strict=True)
            ]
            for at in range(first, sheet.ncols)
        }
        return pl.DataFrame(columns, schema=dict.fromkeys(columns, pl.String))


def _biff_text(datemode: int, kind: int, value: object) -> str | None:
    """Return the text of an xlrd cell of *kind* holding *value*, as fastexcel writes it; an empty cell none.

    A date's serial counts from the workbook's *datemode* (1900 or 1904).
    """
    match kind, value:
        case xlrd.XL_CELL_TEXT, str():
            return value
        case xlrd.XL_CELL_NUMBER, float():
            return str(int(value)) if value.is_integer() else repr(value)
        case xlrd.XL_CELL_DATE, float():
            return f"{xlrd.xldate_as_datetime(value, datemode):%Y-%m-%d %H:%M:%S}"
        case xlrd.XL_CELL_BOOLEAN, int():
            return _BOOLEAN_CELLS[not value]
        case xlrd.XL_CELL_ERROR, int():
            return xlrd.error_text_from_code[value]
        case _:
            return None


class GroupedSheet[RowT: msgspec.Struct](LongFile[RowT], CellHeaderSheet[RowT]):
    """A sheet read long whose key columns sit under group headings, in the row :attr:`_groups_row` above its header.

    The row struct's last three fields are the group, the label and the value, after the record's (a question, an
    answer and its share). A heading names its column and each to its right until the next; a key column with an empty
    header cell is a spacer, not read, and one left of every heading raises, as a key that is no group's.
    """

    #: The sheet row of the group headings, 1-based (Excel's).
    _groups_row: typing.ClassVar[int]
    _key_count = 2
    _unread = (_UNIT,)
    _split_keys = True

    def __init__(self, row: type[RowT], *, sheet: str, header_row: int = 1, null_values: Sequence[str] = ()) -> None:
        """Lay out a grouped table of *row* (see :class:`SheetFile`): its group and label fields before its value."""
        super().__init__(row, sheet=sheet, header_row=header_row, null_values=null_values)
        group, label = self._fields[-3:-1]
        self._keys = re.compile(f"(?P<{group}>[^{_UNIT}]+){_UNIT}(?P<{label}>[^{_UNIT}]+)")

    @typing.override
    def _header_cells(self, cells: pl.DataFrame) -> list[str]:
        labels = super()._header_cells(cells)
        headings = cells.row(self._groups_row - 1) if self._groups_row <= cells.height else (None,) * cells.width
        # The headings over the record's columns are not groups (a link, a title), and are not read.
        lead = len(self._record())
        named, group = labels[:lead], None
        for heading, label in zip(headings[lead:], labels[lead:], strict=True):
            group = heading or group
            named.append(_UNIT if not label else label if group is None else f"{group}{_UNIT}{label}")
        return named


def _text_sheet(reader: fastexcel.ExcelReader, name: str) -> pl.DataFrame:
    """Load sheet *name* of *reader* with no header row, every cell as text, each decimal number to its last digit.

    Row *i* of the frame is the sheet's row *i* + 1, as Excel numbers it, leading empty rows kept. Columns start at
    the first filled one, as calamine trims them, each named ``__UNNAMED__<j>`` for its sheet column *j* (``A`` is 0).
    """
    sheet = reader.load_sheet(name, header_row=None, skip_rows=0, dtypes="string")
    names = {f"__UNNAMED__{c.index}": f"__UNNAMED__{c.absolute_index}" for c in sheet.available_columns()}
    text = sheet.to_polars()
    # fastexcel keys a dtype by the sheet's column (``A`` is 0), so a sheet that opens past column A keys it there.
    floats: fastexcel.DTypeMap = {column.absolute_index: "float" for column in sheet.available_columns()}
    numbers = reader.load_sheet(name, header_row=None, skip_rows=0, dtypes=floats).to_polars()
    # fastexcel may load a column of text as String. Side by side, by name: half the time of Series in the expressions.
    numbers = numbers.cast(pl.Float64, strict=False).rename(lambda column: f"{column}:float")
    return pl.concat([text, numbers], how="horizontal").select(
        pl.when(
            pl.col(f"{column}:float").is_not_null()
            & (pl.col(column).cast(pl.Float64, strict=False) != pl.col(f"{column}:float"))
        )
        .then(pl.col(f"{column}:float").cast(pl.String))
        .otherwise(pl.col(column))
        .alias(names[column])
        for column in text.columns
    )


class FoundSheet[RowT: msgspec.Struct](FoundTable[RowT]):
    """A :class:`FoundTable` on the first sheet of a workbook (``.xlsx``, ``.xls``), its cells as text."""

    @typing.override
    def _lines(self, body: bytes) -> list[list[str]]:
        try:
            reader = fastexcel.read_excel(body)
            sheet = _text_sheet(reader, reader.sheet_names[0])
        except fastexcel.FastExcelError as e:
            msg = f"the body is not a workbook: {e}"
            raise ReadError(msg) from e
        return [[cell or "" for cell in line] for line in sheet.iter_rows()]
