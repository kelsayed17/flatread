"""The base every reader composes on: a file read into its row struct's fields, as a DataFrame or as typed rows.

A :class:`File` is one file's reader, abstract over its format: a subclass reads a body into a DataFrame of its row
struct's fields (:meth:`~File._read`), and :class:`File` checks that frame and turns it into typed rows, by position.
The struct lays the file out: each field names its column in ``Annotated`` metadata
(:mod:`flatread.fields`: a ``Label``, a date or time's ``Format``, a boolean's ``Flag``), in the file's
order, and each column takes its field's type (``str``, ``int``, ``float``, ``bool``, a date, a time of day, a date and
time; a ``Literal`` or ``NewType`` by its base). A label the header writes twice (OCC's two ``Filler`` columns) is two
fields, matched by place. The checks: the columns are the struct's fields, in order; there is at least one row; a float
is finite (polars reads ``NaN`` and ``inf`` as floats, and no file means them as numbers); a null sits only where the
field admits ``None``. Each error names the first row it found. A provider's decoders build one per file and wrap its
two reads, ``body_reader(FILE.rows)`` and ``body_reader(FILE.frame)``.

**Modular.** Each reader is one class doing one thing, and a format is its own small subclass naming the pieces it
needs, each setting a piece's class attribute. The pieces compose, each overriding one step (``_table``) and calling the
next: FINRA's short volume is ``class _ShortVolumeCsv(TrailerCsv, HeadersCsv)``, IBKR's FTP files ``class
_FtpCsv(TitledCsv, TrailerCsv)``. A format adds its own checks the same way: ``_label`` derives a label for a field
without a ``Label`` (ECB's, upper-cased) and ``_format`` a date or time's ``Format`` (SEC's, one per data set),
``_check`` refuses what the format knows is not its file once its rows are as wide as its header (SDMX's values), and
two class switches drop a check it does not want:
``_finite`` (SDMX writes ``NaN`` and ``INF``) and ``_rows_required`` (a header alone is an empty answer). A field with
a default takes it where its file holds no value (SEC writes ``inlineXBRL`` only on an inline document: absent, false).
A field the reader states for every row, not its file (the code of the table read, Z.1's ``F1``), is *given*: a
format's constructor sets ``_given`` before :class:`File`'s, the field takes no column, and every reader fills it. A
text field's :class:`~flatread.fields.Shape` is the regex each of its cells fullmatches, checked in every
reader.

:class:`TextFile` is a table whose cells are text, a format reading a body into them (:meth:`~TextFile._cells`, a
String frame under its header's labels) and :class:`TextFile` typing each as a CSV's cell is, strictly: a whole number
or a number trimmed, a boolean ``true`` or ``false`` or its ``Flag``'s words, a date or time by its ``Format``; a cell
holding nothing or only whitespace is no value. A ``Format`` may name *alternatives*, other formats the column is
written in cell by cell (a file that pads its dates but for a few): each cell must be exactly one. A date's ``Format``
without a day writes a period, in every reader: ``%Y`` and one of ``%m``, ``%-m`` (unpadded), ``%b``, ``%B`` (a
month's name) or ``%q`` (the quarter, 1 to 4), so ``Format("%Y:Q%q")`` reads ``2022:Q4`` and ``Format("%Ym%-m")``
``2011m9``, each its first day; ``%y`` writes the year in two digits, as ``strptime`` reads them, and ``end=True`` reads
a period as its last day. In every reader, too, a date written across two columns (a month's name and its year, a year
and its quarter) is one field: its ``Label`` names the first column and a ``Joined`` marker the second, and the two
cells, joined, are read by its ``Format``.

:class:`GridFile` is a table cut from a grid of text cells, which its source reads from the body: :class:`SheetText`
a sheet's (each cell's text, a number with every digit), :class:`BiffSheet` an Excel 3 or 4 workbook's, and
:class:`CsvText` a delimited file's, as the ``csv`` module reads it (ragged rows, a line break inside a quoted cell).
Its header is one row, under the rows right above it that head its columns as a sheet's do (``_above``), and every
filled row below it is the table's; a format cuts another shape itself (``_table``).

:class:`Thousands` cleans a text reader's numbers grouped in thousands (``1,782,319``), composed before it
(``class _Volumes(Thousands[RowT], ReportFile[RowT])``), and :class:`Carried` fills a group's label, printed on its
first row only, down to the rows below, for any reader. :func:`scalar_type` is a field's scalar, as every reader types
it, and :func:`check_ending` refuses a body cut at a row boundary.
"""

import abc
import codecs
import datetime as dt
import re
import types
import typing
from collections.abc import Collection, Mapping, Sequence
from typing import Final, Literal

import msgspec
import polars as pl

from flatread.errors import EmptyBodyError, LayoutError, ReadError
from flatread.fields import Cell, Flag, Format, Joined, Label, Shape, Span

__all__ = ["Carried", "File", "GridFile", "Reshaped", "TextFile", "Thousands", "check_ending", "scalar_type"]

#: The NUL character, which no line of text holds: the separator polars splits a body's lines on.
_NUL: Final = chr(0)
#: The unit separator (U+001F), which no heading holds: what joins a stacked header's cells, top to bottom, and a
#: GroupedSheet's group and label, and what it calls a spacer column's header.
_UNIT: Final = chr(0x1F)


#: The polars type each field scalar is read as; a date or time is read as text, then parsed with its column's format.
_DTYPES: Final[Mapping[object, type[pl.DataType]]] = {
    str: pl.String,
    int: pl.Int64,
    float: pl.Float64,
    bool: pl.Boolean,
}


def _bare(annotation: object) -> object:
    """Return *annotation* without its ``Annotated`` metadata: ``float | None`` for ``Annotated[float | None, ...]``."""
    return typing.get_args(annotation)[0] if typing.get_origin(annotation) is typing.Annotated else annotation


def scalar_type(annotation: object) -> object:
    """Return a field's scalar: ``float`` for ``float | None``, a ``NewType``'s base, ``str`` for a ``Literal``."""
    annotation = _bare(annotation)
    if isinstance(annotation, types.UnionType) or typing.get_origin(annotation) is typing.Union:
        (annotation,) = (arm for arm in typing.get_args(annotation) if arm is not types.NoneType)
    if isinstance(annotation, typing.NewType):
        return scalar_type(annotation.__supertype__)
    if typing.get_origin(annotation) is Literal:
        return str
    return annotation


def _none_dtype(scalar: object) -> pl.DataType | type[pl.DataType]:
    """Return the polars type a column of *scalar* holds, for one a table lacks: all of it no value."""
    return {**_DTYPES, dt.date: pl.Date, dt.datetime: pl.Datetime("us"), dt.time: pl.Time}[scalar]


def _address(address: str) -> tuple[int, int]:
    """Return the sheet row (1-based) and column (``A`` is 0) of the cell Excel names *address* (``"I1"``)."""
    if (match := re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]*)", address)) is None:
        msg = f"{address!r} is no cell, as Excel names one (``I1``)"
        raise LayoutError(msg)
    column = 0
    for letter in match[1]:
        column = column * 26 + ord(letter) - ord("A") + 1
    return int(match[2]), column - 1


def _check_above(above: int, header_row: int) -> None:
    """Refuse *above* rows heading a table's columns that do not fit over its header in row *header_row*."""
    if above and not 0 < above < header_row:
        msg = f"the {above} rows over the header do not fit above its row {header_row}"
        raise LayoutError(msg)


def _stacked(rows: Sequence[Sequence[str | None]]) -> list[str]:
    """Return each column's header from *rows*, those over the header then its own, U+001F between a column's cells.

    A cell over the header heads its own column and every column to its right with no cell of its own; an empty cell
    is ``None``.
    """
    stacked = []
    for row in rows[:-1]:
        filled = None
        stacked.append([filled := cell if cell is not None else filled for cell in row])
    return [_UNIT.join(cell or "" for cell in cells) for cells in zip(*stacked, rows[-1], strict=True)]


def _first(mask: pl.Series, row_one: int = 1) -> int | None:
    """Return the row number where *mask* is first true, its first row numbered *row_one*, or ``None``."""
    at = mask.arg_true()
    return int(at[0]) + row_one if at.len() else None


#: The scalars a column reads: a field of any other type has no column.
_SCALARS: Final = frozenset({str, int, float, bool, dt.date, dt.datetime, dt.time})


#: The scalars read by a ``Format`` (or a sheet's date or time cells): a date, a date and time, a time of day.
_MOMENTS: Final = frozenset({dt.date, dt.datetime, dt.time})


class _Column(typing.NamedTuple):
    """A field's column: the field, its label, its scalar, how a date or time reads, a boolean's words, its Span.

    *aliases* are its label's alternatives, the other words a header read by name writes it in; *joined* the second
    column of a field written across two (:class:`~flatread.fields.Joined`).
    """

    field: str
    label: str
    scalar: object
    written: Format | None
    flag: Flag | None
    span: Span | None
    aliases: tuple[str, ...] = ()
    joined: Joined | None = None
    #: A :class:`~flatread.fields.Cell` field's cell: its sheet row (1-based) and column (``A`` is 0).
    cell: tuple[int, int] | None = None
    shape: Shape | None = None


class File[RowT: msgspec.Struct](abc.ABC):
    """A file's reader, read two ways: :meth:`frame`, a DataFrame of its row struct's fields, and :meth:`rows`."""

    #: Whether every float is finite: a format that writes ``NaN`` and infinities as values (SDMX) sets it false.
    _finite: typing.ClassVar[bool] = True
    #: Whether a header alone is no file: a format whose header with no rows is an empty answer (SDMX) sets it false,
    #: as does a file that states its row count (a trailer's), which then vouches for a table of none.
    _rows_required: typing.ClassVar[bool] = True
    #: Whether a long table's heading may repeat (a CSV's ``Difference`` under each week): a CSV then reads it by its
    #: heading, a NUL and its number, and a key may name the column's place (:class:`LongFile`'s ``_key_count``), an
    #: ``int`` counted from 1 among the keys. Without it, a repeat keeps polars' name (``Difference_duplicated_0``).
    _repeats: typing.ClassVar[bool] = False
    #: Whether the format finds a column by its label in the header, so a ``Label`` may name alternatives.
    _by_name: typing.ClassVar[bool] = False
    #: Whether the format reads a field from one cell its file states (a sheet's), so a field may be a ``Cell``'s.
    _by_cell: typing.ClassVar[bool] = False
    #: The field holding each row's own number in the file, as its errors name it, where a format names one: a table
    #: whose rows no label tells apart (GDPNow's rows without a ticker). A reader that knows its rows' numbers (a
    #: sheet's) fills it; the field is a whole number and names no column.
    _numbered: typing.ClassVar[str | None] = None
    #: Fields every row holds as its reader states them, not as its file writes them (the code of the table read):
    #: a format's constructor sets them before :class:`File`'s, and they take no column.
    _given: Mapping[str, object] = types.MappingProxyType[str, object]({})
    #: The labels of the header's columns no field reads, dropped unread wherever they stand: a format whose file writes
    #: a column of prose notes beside its table (``notes``), its own row numbers under an empty header cell (``""``) or
    #: a heading over a group of columns sets them. A sheet's, a delimited file's and a text table's readers drop them.
    _unread: typing.ClassVar[Collection[str]] = ()
    #: The labels of the header's columns no field reads that never hold a value, each repeat listed: a cell holding
    #: one raises, and each is then dropped. A delimited file's and a text table's readers check them (Nasdaq's
    #: ``Filler`` columns, Yahoo's ``Follow`` buttons); a column :attr:`_unread` drops may hold anything.
    _blank_columns: typing.ClassVar[Collection[str]] = ()

    def __init__(self, row: type[RowT], *, row_one: int = 1) -> None:
        """Lay out a file of *row*: each field's column, from its ``Annotated`` metadata (see the module).

        *row_one* is the number the file's first row under its header takes in errors: 1, or a sheet's own row.
        Raises ``ValueError`` for a field without a ``Label`` (unless the format derives one), a ``Format`` on a field
        that is no date or time or with a zone on a date, or a field of a type no column reads.
        """
        self._row = row
        self._fields = list(row.__struct_fields__)
        # A field's default is its value where its file holds none (an attribute SEC writes only when true).
        self._defaults = {
            field.name: field.default
            for field in msgspec.structs.fields(row)
            if field.default is not msgspec.NODEFAULT and field.default is not None
        }
        self._required = [
            field.name
            for field in msgspec.structs.fields(row)
            if types.NoneType not in typing.get_args(_bare(field.type)) and field.name not in self._defaults
        ]
        self._row_one = row_one
        if stray := self._given.keys() - set(self._fields):
            msg = f"the format gives {sorted(stray)}, which are no fields of {row.__name__}"
            raise LayoutError(msg)
        fields = msgspec.structs.fields(row)
        columns = [self._column(field) for field in fields if field.name not in self._given]
        # A field written across two columns is read as two text columns, joined into its value by frame().
        self._joined = {
            column.field: (column, joined, written)
            for column in columns
            if (joined := column.joined) is not None and (written := column.written) is not None
        }
        self._columns = [part for column in columns for part in _parts(column)]
        # Each given value typed as its field reads, so ``None`` is not a Null column, nor ``3`` an Int32 one.
        scalars = {field.name: scalar_type(field.type) for field in fields if field.name in self._given}
        self._given_types = {
            name: _DTYPES[scalar] if scalar in _DTYPES else _temporal(scalar, None) for name, scalar in scalars.items()
        }
        # The type each field's column holds: rows() builds its structs from them unchecked.
        self._dtypes = {
            column.field: _DTYPES.get(column.scalar) or _temporal(column.scalar, column.written) for column in columns
        } | self._given_types

    def _column(self, field: msgspec.structs.FieldInfo) -> _Column:
        metadata = typing.get_args(field.type)[1:] if typing.get_origin(field.type) is typing.Annotated else ()
        named = next((meta for meta in metadata if isinstance(meta, Label)), None)
        written = next((meta for meta in metadata if isinstance(meta, Format)), None)
        flag = next((meta for meta in metadata if isinstance(meta, Flag)), None)
        span = next((meta for meta in metadata if isinstance(meta, Span)), None)
        joined = next((meta for meta in metadata if isinstance(meta, Joined)), None)
        shape = next((meta for meta in metadata if isinstance(meta, Shape)), None)
        cell = self._cell_of(field.name, next((meta for meta in metadata if isinstance(meta, Cell)), None))
        # A Cell field takes no column, so its name stands for its label.
        label = named.text if named is not None else field.name if cell is not None else self._label(field.name)
        aliases = named.alternatives if named is not None else ()
        if aliases and not self._by_name:
            msg = f"the field {field.name!r} names Label alternatives, which only a header read by name takes"
            raise LayoutError(msg)
        if label is None:
            msg = f"the field {self._row.__name__}.{field.name} names no column: annotate it with a Label"
            raise LayoutError(msg)
        scalar = scalar_type(field.type)
        written = self._format(scalar) if written is None else written
        if scalar not in _SCALARS:
            msg = f"the field {field.name!r} is a {getattr(scalar, '__name__', scalar)}, which no column reads"
            raise LayoutError(msg)
        if written is not None and scalar not in _MOMENTS:
            msg = f"the field {field.name!r} is no date or time, so it takes no Format"
            raise LayoutError(msg)
        if written is not None and written.time_zone is not None and scalar is not dt.datetime:
            msg = (
                f"the date field {field.name!r} has no time, so its Format takes no zone"
                if scalar is dt.date
                else f"the time-of-day field {field.name!r} has no date, so its Format takes no zone"
            )
            raise LayoutError(msg)
        for written_as in () if written is None else (written.strptime, *written.alternatives):
            if scalar is dt.date and not _DAY.search(written_as):
                _period(written_as)  # refuses a directive a period is not written with
            elif written is not None and written.end:
                msg = f"the field {field.name!r} is no period, so its Format {written_as!r} has no end"
                raise LayoutError(msg)
            elif "%q" in written_as:
                msg = f"the field {field.name!r} is no date, so its Format {written_as!r} writes no quarter"
                raise LayoutError(msg)
        if flag is not None and scalar is not bool:
            msg = f"the field {field.name!r} is no boolean, so it takes no Flag"
            raise LayoutError(msg)
        if joined is not None and (written is None or span is not None):
            msg = f"the field {field.name!r} is Joined, so it is a date or time read by its Format, and has no Span"
            raise LayoutError(msg)
        if shape is not None and scalar is not str:
            msg = f"the field {field.name!r} is no text, so it takes no Shape"
            raise LayoutError(msg)
        return _Column(field.name, label, scalar, written, flag, span, aliases, joined=joined, shape=shape, cell=cell)

    def _cell_of(self, name: str, stated: Cell | None) -> tuple[int, int] | None:
        """Return the row and column of the cell that states field *name*, where it is a ``Cell``'s, or raise."""
        if stated is not None and not self._by_cell:
            msg = f"the field {name!r} is a Cell's, which only a sheet's reader reads"
            raise LayoutError(msg)
        return None if stated is None else _address(stated.address)

    def _label(self, name: str) -> str | None:
        """Return the label of field *name*, which names no ``Label``: none, unless a format derives its labels."""
        del name
        return None

    def _joined_header(self) -> bool:
        """Return whether a header cell joins the rows over it by U+001F: no, unless a reader stacks its header."""
        return False

    def _layout(self, header: Sequence[str]) -> Sequence[_Column] | None:
        """Return the columns a body whose header is *header* is read as: ``None``, the struct's, unless a long table.

        A :class:`LongFile` lays out a column per key its header names.
        """
        del header
        return None

    def _format(self, scalar: object) -> Format | None:
        """Return how a field of *scalar* that names no ``Format`` is written: none, unless a format derives it."""
        del scalar
        return None

    def _clean(self, cells: pl.Series, column: _Column) -> pl.Series:
        """Return *column*'s text *cells* as its type reads them: as written, unless a piece cleans them.

        Only a reader that reads its cells as text (a :class:`TextFile`, a padded or grouped CSV) calls it.
        """
        del column
        return cells

    @abc.abstractmethod
    def _read(self, body: bytes) -> pl.DataFrame:
        """Read *body* into a frame of the row struct's fields, in order, typed as each field, or raise ValueError."""

    def frame(self, body: bytes) -> pl.DataFrame:
        """Read *body* into a DataFrame of the row struct's fields, one row per row of the file, in order.

        Raises ``ValueError`` for what the format refuses, and for a reader whose columns are not the fields or not
        typed as them (``'2'`` in an ``int`` field), which :meth:`rows` would build structs of unchecked.
        """
        frame = self._join(self._read(body))
        if self._given:
            frame = frame.with_columns(
                pl.lit(value, self._given_types[name]).alias(name) for name, value in self._given.items()
            )
            frame = frame.select(name for name in self._fields if name in frame.columns)
        if frame.columns != self._fields:
            msg = f"the reader's columns {frame.columns} are not {self._row.__name__}'s fields {self._fields}"
            raise LayoutError(msg)
        # A text field's column may be an Enum of its Literal's values (Treasury's TIC tables type theirs so).
        if wrong := {
            name: str(dtype)
            for name, dtype in frame.schema.items()
            if dtype != self._dtypes[name] and not (isinstance(dtype, pl.Enum) and self._dtypes[name] == pl.String)
        }:
            typed = {name: str(self._dtypes[name]) for name in wrong}
            msg = f"the reader's {wrong} columns are not typed as {self._row.__name__}'s fields {typed}"
            raise LayoutError(msg)
        if self._defaults:
            frame = frame.with_columns(pl.col(name).fill_null(value) for name, value in self._defaults.items())
        if self._rows_required and frame.is_empty():
            msg = "the body is a header and no rows"
            raise EmptyBodyError(msg)
        self._check_cells(frame, self._required)
        for column in self._columns:
            if column.shape is None:
                continue
            cells = frame[column.field]
            wrong = ~cells.str.contains(f"^(?:{column.shape.pattern})$").fill_null(value=True)
            if (at := _first(wrong, self._row_one)) is not None:
                msg = (
                    f"the {column.label!r} cell {cells[at - self._row_one]!r} is not of its shape"
                    f" {column.shape.pattern!r}: row {at}"
                )
                raise ReadError(msg)
        return frame

    def _join(self, frame: pl.DataFrame) -> pl.DataFrame:
        """Return *frame* with each field written across two columns read from their text, joined (see ``Joined``)."""
        for field, (column, joined, written) in self._joined.items():
            second = _second(field)
            half = frame[field].is_null() != frame[second].is_null()
            if (at := _first(half, self._row_one)) is not None:
                msg = (
                    f"row {at} holds one of the {column.label!r} and {joined.label!r} cells, a {field} written in both"
                )
                raise ReadError(msg)
            text = pl.select(pl.concat_str(frame[field], frame[second], separator=joined.separator)).to_series()
            value = _moment(text.alias(column.label), _temporal(column.scalar, written), written, self._row_one)
            frame = frame.with_columns(value.alias(field)).drop(second)
        return frame

    def _check_cells(self, frame: pl.DataFrame, required: Collection[str], among: pl.Series | None = None) -> None:
        """Refuse a float in *frame* that is not finite (unless the format writes them) or a null in *required*.

        With *among*, a mask of *frame*'s rows, only those rows are checked (a long table's records that hold values).
        """
        checked = pl.repeat(value=True, n=frame.height, eager=True) if among is None else among
        floats = [name for name, dtype in frame.schema.items() if dtype == pl.Float64]
        infinite = {
            name: at
            for name in floats
            if self._finite and (at := _first(~frame[name].is_finite() & checked, self._row_one)) is not None
        }
        if infinite:
            msg = (
                f"the {list(infinite)} columns hold NaN or an infinity, which is no number here:"
                f" row {min(infinite.values())}"
            )
            raise ReadError(msg)
        if empty := {
            name: at for name in required if (at := _first(frame[name].is_null() & checked, self._row_one)) is not None
        }:
            msg = (
                f"the {list(empty)} columns have an empty cell, a short row or no column, and every cell holds a value:"
                f" row {min(empty.values())}"
            )
            raise ReadError(msg)

    def rows(self, body: bytes) -> list[RowT]:
        """Read *body* into typed rows: :meth:`frame`'s rows, one struct a row."""
        # By position: frame() checked its columns are the struct's fields, in order, already typed.
        return [self._row(*row) for row in self.frame(body).iter_rows()]


#: A number written with thousands separators: a group of one to three digits, then groups of three.
_THOUSANDS: Final = r"^[+-]?[0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?$"


class Thousands[RowT: msgspec.Struct](File[RowT]):
    """A table whose numbers are written with thousands separators (``1,782,319``), each read as its number.

    It cleans the cells of a reader that reads them as text, listed after it
    (``class _Volumes(Thousands[RowT], ReportFile[RowT])``): each number cell holding a comma must be grouped so.
    """

    @typing.override
    def _clean(self, cells: pl.Series, column: _Column) -> pl.Series:
        cells = super()._clean(cells, column)
        if column.scalar not in {int, float}:
            return cells
        grouped = cells.str.contains(",", literal=True)
        if (at := _first((grouped & ~cells.str.contains(_THOUSANDS)).fill_null(value=False))) is not None:
            msg = f"the {column.label!r} cell {cells[at - 1]!r} is not a number grouped in thousands: row {at}"
            raise ReadError(msg)
        return cells.str.replace_all(",", "", literal=True)


class Carried[RowT: msgspec.Struct](File[RowT]):
    """A table that prints a group's label on its first row only (a class over its exchanges, a sector over its rows).

    Each of :attr:`_carried`'s fields, where a row leaves it empty, is the row's above. It reads by the reader listed
    after it (``class _ByClass(Carried[RowT], ReportFile[RowT])``); a carried field still empty, on the table's first
    row, is refused as any empty cell is.
    """

    #: The fields a group's first row writes and the rows below it leave empty.
    _carried: typing.ClassVar[Collection[str]]

    @typing.override
    def _read(self, body: bytes) -> pl.DataFrame:
        frame = super()._read(body)
        if stray := set(self._carried) - set(frame.columns):
            msg = f"the format carries {sorted(stray)} down, which are no fields of {self._row.__name__}"
            raise LayoutError(msg)
        return frame.with_columns(pl.col(*self._carried).forward_fill())


class TextFile[RowT: msgspec.Struct](File[RowT]):
    """A table whose cells are text, typed strictly by its row struct's fields: an XML table's, an HTML table's.

    A format reads a body into its text cells (:meth:`_cells`), and each is typed as a CSV's cell is: a whole number
    or a number trimmed, a boolean ``true`` or ``false`` or its ``Flag``'s words, a date or time by its ``Format``. A
    cell holding nothing or only whitespace is no value, except in a field the format names in :attr:`_as_written`,
    whose cells are kept as the format read them (an empty name SEC's index writes is ``""``).
    """

    #: The fields whose text is kept as written, whitespace and empty text included; a format's subclass sets them.
    _as_written: typing.ClassVar[Collection[str]] = ()

    def __init__(self, row: type[RowT]) -> None:
        """Lay out a table of *row*: each field names its column with a :class:`~flatread.fields.Label`.

        Raises ``ValueError`` for a date or time field without the :class:`~flatread.fields.Format` its
        text is written in, or what :class:`File` refuses.
        """
        super().__init__(row)
        for column in self._columns:
            if column.scalar in _MOMENTS and column.written is None:
                msg = f"the field {column.field!r} needs the Format its text is written in"
                raise LayoutError(msg)
        self._labels = [column.label for column in self._columns]

    @abc.abstractmethod
    def _cells(self, body: bytes) -> pl.DataFrame:
        """Read *body* into a String frame of its cells, one column per header label, in order, or raise ValueError."""

    @typing.override
    def _read(self, body: bytes) -> pl.DataFrame:
        text = self._cells(body)
        unread = {*self._unread, *self._blank_columns} - set(self._labels)
        dropped = [label for label in text.columns if label in unread]
        for label in (label for label in dropped if label in self._blank_columns):
            if (at := _first(text[label].str.strip_chars().fill_null("") != "")) is not None:
                msg = f"the {label!r} column holds {text[label][at - 1]!r}, and it never holds a value: row {at}"
                raise ReadError(msg)
        text = text.drop(dropped)
        columns = self._columns if (layout := self._layout(text.columns)) is None else layout
        if text.columns != (labels := [column.label for column in columns]):
            msg = f"the header {text.columns} is not {labels}"
            raise ReadError(msg)
        # A cell holding nothing, or only whitespace, holds no value, but for a field the format keeps as written.
        written = [column.label for column in columns if column.field in self._as_written]
        blank = pl.col(pl.String).exclude(*written) if written else pl.col(pl.String)
        text = text.with_columns(pl.when(blank.str.strip_chars() != "").then(blank))
        typed = []
        for column in columns:
            cells = self._clean(text[column.label], column)
            cells = cells if column.field in self._as_written else cells.replace("", None)
            typed.append(_typed(column, cells, f"{column.label!r} cell"))
        return pl.DataFrame(typed)


class GridFile[RowT: msgspec.Struct](TextFile[RowT]):
    """A table cut from a grid of text cells, which a source reads from the body: a sheet's, a delimited file's.

    A source (:class:`SheetText`, :class:`CsvText`) reads the body into the grid (:meth:`_body_grid`), a String frame: a
    row per row of the file from its first, a column per column (``0``, ``1``, ...), a cell of nothing but spaces and
    each of the file's words for none (*null_values*) no value. A format cuts its table from it (:meth:`_table`: by
    default its header in row *header_row*, under the :attr:`_above` rows right over it, and every filled row below, a
    column labeled one of :attr:`_unread` dropped, a cell past the header refused) and :class:`TextFile` types its
    cells. A piece changes the cut: :class:`TitledGrid` finds the header (:meth:`_header_at`), :class:`BlankEnded`
    ends the rows (:meth:`_rows_below`), :class:`BesideGrid` reads a band of the columns.
    """

    #: How many rows right over the header row head its columns too (a group over its labels): a cell heads its own
    #: column and every column to its right with no cell of its own, to the header's last filled column. Each
    #: column's header is then its cells top to bottom, stripped, the unit separator (U+001F) between, which a reader
    #: that lays out the key columns (:class:`LongFile`) takes them from.
    _above: typing.ClassVar[int] = 0

    def __init__(self, row: type[RowT], *, header_row: int = 1, null_values: Sequence[str] = ()) -> None:
        """Lay out a table of *row* under its header in row *header_row* of the grid (the file's own, from 1).

        Raises ``ValueError`` where the :attr:`_above` rows do not fit over *header_row*, or what :class:`TextFile`
        refuses.
        """
        super().__init__(row)
        _check_above(self._above, header_row)
        self._header_row = header_row
        self._null_values = list(null_values)

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
        """Return the grid rows under the header in row *at*: every one to the grid's end, unless a piece ends them."""
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
        return rows.filter(~pl.all_horizontal(pl.all().is_null())) if rows.width else rows


class BlankEnded[RowT: msgspec.Struct](GridFile[RowT]):
    """A grid table that ends at its first blank row: what stands below it (notes, another table) is not read.

    Example: ``docs/examples/blank_ended.csv``, a seasonal factor file whose months stop at a blank row over
    ``Last Updated: 9-30-2026``. :class:`GridFile`'s own table runs to the grid's end, skipping blank rows;
    :class:`NotedSheet`'s ends at its first row with nothing past column A, this one at a row with nothing at all.
    """

    @typing.override
    def _rows_below(self, grid: pl.DataFrame, at: int) -> pl.DataFrame:
        below = super()._rows_below(grid, at)
        end = _first(below.select(pl.all_horizontal(pl.all().is_null())).to_series(), 0)
        return below if end is None else below.head(end)


class TitledGrid[RowT: msgspec.Struct](GridFile[RowT]):
    """A grid table under its title row: its header is the first filled row below the row that is :attr:`_title` alone.

    Example: ``docs/examples/titled_grid.csv``, a file of prose and titled tables (``I. Specifications for
    individual IP indexes``). :class:`PreambleCsv` finds its header by the struct's first label and :class:`TitledCsv`
    matches every line above it; this one finds one table of several by the title over it, so each of a file's tables
    is read by its title. Compose it with :class:`BlankEnded` for a table that ends at the next blank row.
    """

    #: The title row's one cell, as written: a format's subclass sets it.
    _title: typing.ClassVar[str]

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
    :attr:`_short_by` fewer than the fullest row; a grid whose fullest row fills fewer than :attr:`_fewest` holds no
    table (a note, ``No ETF trades``). :class:`TitledGrid` finds its header under a known title and
    :class:`PreambleCsv` by its first label; this one by the header's shape alone, for a sheet whose titles and
    labels change from release to release.
    """

    #: The fewest cells a header fills: a grid whose fullest row fills fewer holds no table.
    _fewest: typing.ClassVar[int] = 2
    #: How many fewer cells than the fullest row the header may fill (a row of values may fill more than its header).
    _short_by: typing.ClassVar[int] = 3

    @typing.override
    def _header_at(self, grid: pl.DataFrame) -> int:
        filled = grid.select(pl.sum_horizontal(pl.all().is_not_null())).to_series()
        fullest = filled.max() if grid.height else 0
        if not isinstance(fullest, int) or fullest < self._fewest:
            return grid.height + 1  # no table: a header below the grid's end is no header
        # The fullest row is one such row, so there is a first.
        return int((filled >= max(self._fewest, fullest - self._short_by)).arg_true()[0]) + 1


class BesideGrid[RowT: msgspec.Struct](GridFile[RowT]):
    """A grid table with more beside it (a second table, a spacer): only the grid's columns in :attr:`_band` are read.

    Example: ``docs/examples/beside_grid.csv``, two fee reviews side by side, B to D and F on.
    :class:`BesideSheet`'s twin: that one names each column it loads (``_columns_at``); this one cuts a band, from its
    first column to before its last (``None``: the grid's end), so a table that grows to the right keeps every column.
    """

    #: The table's first grid column (``0`` is the file's first) and the column past its last, ``None`` the grid's end.
    _band: typing.ClassVar[tuple[int, int | None]] = (0, None)

    @typing.override
    def _table(self, grid: pl.DataFrame) -> pl.DataFrame:
        first, last = self._band
        return super()._table(grid.select(grid.columns[first:last]))


#: A boolean as XML Schema writes it, and a fixed-width file without a ``Flag``.
_BOOLEANS: Final = {"true": True, "false": False}


def _typed(column: _Column, text: pl.Series, what_cell: str) -> pl.Series:
    """Return *column*'s values from its cells' *text*, strictly, refusing one that is not its type, naming its row."""
    if column.written is not None:
        return _moment(text, _temporal(column.scalar, column.written), column.written).alias(column.field)
    if column.scalar is str:
        return text.alias(column.field)
    if column.flag is not None:
        return _flagged(text, column.flag).alias(column.field)
    if column.scalar is bool:
        values = text.replace_strict(_BOOLEANS, default=None, return_dtype=pl.Boolean)
        what = "true or false"
    else:
        values = text.str.strip_chars().cast(_DTYPES[column.scalar], strict=False)
        what = "whole number" if column.scalar is int else "number"
    if (at := _first(text.is_not_null() & values.is_null())) is not None:
        msg = f"the {what_cell} {text[at - 1]!r} is not a {what}: row {at}"
        raise ReadError(msg)
    return values.alias(column.field)


def _stated(column: _Column, text: str | None, rows: int, what_cell: str) -> pl.Series:
    """Return *column*'s value on each of *rows* rows, from the *text* its file states once, typed as :func:`_typed`.

    The one step for every value a file states for many rows: a trailer's or a title's group, a member's or a sheet's
    name.
    """
    return _typed(column, pl.Series(column.field, [text], pl.String), what_cell).new_from_index(0, rows)


def _second(field: str) -> str:
    """Return the name a reader gives the second column of *field*, a field written across two (``Joined``)."""
    return f"{field}+"


def _parts(column: _Column) -> list[_Column]:
    """Return the columns *column*'s field is read from: itself, or a ``Joined`` field's two, each read as text."""
    if column.joined is None:
        return [column]
    first = column._replace(scalar=str, written=None, joined=None)
    return [first, _Column(_second(column.field), column.joined.label, str, None, None, None)]


def _repeated(labels: Sequence[str], name: str, first: int, *, empties: bool = False) -> list[str]:
    """Return the names a reader gives a header of *labels*: each repeat of a label *name*, numbered from *first*.

    *name* is formatted with the label and the repeat's number; an empty label (a sheet's empty header cell) stays,
    unless the reader numbers those too (*empties*, polars').
    """
    seen: dict[str, int] = {}
    named = []
    for label in labels:
        repeats = seen[label] = seen.get(label, -1) + 1
        named.append(name.format(label=label, n=repeats - 1 + first) if repeats and (label or empties) else label)
    return named


class Reshaped[RowT: msgspec.Struct](File[RowT]):
    """A table its inner reader reads (a struct of the file's own columns), made the row's fields by :meth:`_shape`.

    A format reads a file whose rows are not yet its struct's (read long, then each column's month named; a block of
    outcome columns unpivoted) with an inner reader of the file's shape and a :meth:`_shape` that reshapes its frame.
    The inner reader checks the file; the shape checks what only the whole table shows. :class:`File` then checks the
    shaped frame is the row's fields, in order, typed. Unlike :class:`LongFile`, which reads a wide table long itself,
    this reshapes a frame another reader made.
    """

    def __init__(self, row: type[RowT], inner: File[typing.Any]) -> None:
        """Read a file of *row* as *inner* reads it, then shaped; *row*'s fields name no columns of their own."""
        super().__init__(row)
        self._inner = inner

    @typing.override
    def _label(self, name: str) -> str | None:
        return name  # the inner reader lays out the columns: these are its frame's

    def _shape(self, frame: pl.DataFrame) -> pl.DataFrame:
        """Return the inner reader's *frame* as the row's fields: as read, unless a format reshapes it, or raise."""
        return frame

    @typing.override
    def _read(self, body: bytes) -> pl.DataFrame:
        return self._shape(self._inner.frame(body))


def _decoded(body: bytes, encodings: Sequence[str], *, lossy: bool = False) -> str:
    """Return *body* as text in the first of *encodings* that decodes it, or raise ``ValueError``.

    ``utf-16`` decodes only a body opening with its byte-order mark. With *lossy*, a byte the last encoding does not
    decode is U+FFFD.
    """
    tried = [
        encoding
        for encoding in encodings
        if encoding != "utf-16" or body.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))
    ]
    error = None
    for encoding in tried:
        try:
            return body.decode(encoding)
        except UnicodeDecodeError as e:
            error = e
    if lossy and tried:
        return body.decode(tried[-1], errors="replace")
    msg = f"the body is not {' or '.join(encodings)}: {error}"
    raise ReadError(msg)


def _lines(body: bytes) -> pl.Series:
    """Return *body*'s lines as text, line endings and a byte order mark off: split by polars, on a NUL no line holds.

    No copy of the body is made in Python, as ``decode`` and ``split`` would make. A blank line is an empty line.
    """
    try:
        lines = pl.read_csv(body, has_header=False, separator=_NUL, quote_char=None, schema={"line": pl.String})
    except pl.exceptions.PolarsError as e:
        msg = f"the body is not UTF-8: {e}"
        raise ReadError(msg) from e
    return lines["line"].fill_null("")


def _flagged(cells: pl.Series, flag: Flag, row_one: int = 1) -> pl.Series:
    """Read *cells* as booleans by *flag*'s words, refusing a cell that is neither."""
    values = cells.replace_strict({flag.true: True, flag.false: False}, default=None, return_dtype=pl.Boolean)
    if (at := _first(cells.is_not_null() & values.is_null(), row_one)) is not None:
        msg = f"the {cells.name!r} cell {cells[at - row_one]!r} is not {flag.true!r} or {flag.false!r}: row {at}"
        raise ReadError(msg)
    return values


def _temporal(scalar: object, written: Format | None) -> pl.Date | pl.Datetime | pl.Time:
    """Return the polars type a date or time field reads as: a date, a time of day, or a time, aware in its zone."""
    if scalar is dt.date:
        return pl.Date()
    if scalar is dt.time:
        return pl.Time()
    return pl.Datetime("us", written.time_zone if written is not None else None)


def _strptime(cells: pl.Series, dtype: pl.Date | pl.Datetime | pl.Time, written_as: str) -> pl.Series:
    """Parse *cells* as *dtype* written *written_as*, a cell written otherwise null."""
    try:
        return cells.str.strptime(dtype, written_as, strict=False)
    except pl.exceptions.PolarsError as e:  # a local time a zone's clock change skips or repeats
        msg = f"the {cells.name!r} column holds a time that is no one moment in its zone: {e}"
        raise ReadError(msg) from e


#: A directive writing a day: a date format without one writes a period (a year, a month, a quarter).
_DAY: Final = re.compile(r"%-?[dej]")


#: strptime's two-digit years: from this one to 99 the 1900s, below it the 2000s.
_TWO_DIGIT_PIVOT: Final = 69


_MONTH_NAMES: Final = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


_FULL_MONTH_NAMES: Final = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip


#: Each month's number, by its name or its abbreviation.
_MONTHS: Final = {
    **{name: n for n, name in enumerate(_MONTH_NAMES, 1)},
    **{name: n for n, name in enumerate(_FULL_MONTH_NAMES, 1)},
}


#: The directives a period is written with, each a group of the pattern it reads by.
_PERIOD_PARTS: Final = {
    "%Y": "(?<year>[0-9]{4})",
    "%y": "(?<yy>[0-9]{2})",
    "%m": "(?<month>0[1-9]|1[0-2])",
    "%-m": "(?<month>1[0-2]|[1-9])",
    "%b": f"(?<name>{'|'.join(_MONTH_NAMES)})",
    "%B": f"(?<name>{'|'.join(_FULL_MONTH_NAMES)})",
    "%q": "(?<quarter>[1-4])",
}


def _period(written_as: str) -> str:
    """Return the pattern a period written *written_as* fullmatches, or raise ValueError for a format it is not.

    polars reads no month written without its day unless padded, and no quarter: a period is read by its pattern.
    """
    parts = re.split(r"(%-?.)", written_as)
    directives = parts[1::2]
    years = [directive for directive in directives if directive in {"%Y", "%y"}]
    months = [directive for directive in directives if directive not in {"%Y", "%y"}]
    if not ({*directives} <= _PERIOD_PARTS.keys() and len(years) == 1 and len(months) <= 1):
        msg = (
            f"the date format {written_as!r} writes no day, so it writes a period: %Y (or %y) and at most one of"
            " %m, %-m, %b, %B or %q, besides literal text"
        )
        raise LayoutError(msg)
    return "".join(_PERIOD_PARTS[part] if n % 2 else re.escape(part) for n, part in enumerate(parts))


def _read_period(cells: pl.Series, written_as: str, *, end: bool = False) -> pl.Series:
    """Parse *cells* as periods written *written_as*: each one's first day, or its last at its *end*; else null."""
    pattern = _period(written_as)
    parts = cells.to_frame().select(pl.first().str.extract_groups(f"^{pattern}$").struct.unnest())
    months = 1
    if "month" in parts.columns:
        month = pl.col("month").cast(pl.Int8)
    elif "name" in parts.columns:
        month = pl.col("name").replace_strict(_MONTHS, return_dtype=pl.Int8)
    elif "quarter" in parts.columns:
        month, months = (pl.col("quarter").cast(pl.Int8) - 1) * 3 + 1, 3
    else:
        month, months = pl.lit(1, pl.Int8), 12
    if "year" in parts.columns:
        year = pl.col("year").cast(pl.Int32)
    else:
        short = pl.col("yy").cast(pl.Int32)
        year = pl.when(short >= _TWO_DIGIT_PIVOT).then(short + 1900).otherwise(short + 2000)
    first = pl.date(year, month, 1)
    return parts.select(first.dt.offset_by(f"{months - 1}mo").dt.month_end() if end else first).to_series()


def _moment(cells: pl.Series, dtype: pl.Date | pl.Datetime | pl.Time, written: Format, row_one: int = 1) -> pl.Series:
    """Parse *cells* as *dtype* by *written*, refusing a cell that is not its value written in one of its formats.

    A cell must be what a format writes for its value: polars' ``%Y`` takes one to four digits (``01/08/71`` is the
    year 71) and ``%m`` one or two, and a day that does not exist (``02/30/2026``) is no date. A date's format with no
    day writes a period, read as its first day by the pattern :func:`_period` gives it (``%YQ%q`` is ``2026Q1``,
    ``%Ym%-m`` ``2011m9``). A cell takes the first of the formats it is written in. A time in a zone is read as an
    aware time in it, and written back in it.
    """
    formats = (written.strptime, *written.alternatives)
    periods = [dtype == pl.Date and not _DAY.search(written_as) for written_as in formats]
    parsed = [
        _read_period(cells, written_as, end=written.end) if period else _strptime(cells, dtype, written_as)
        for written_as, period in zip(formats, periods, strict=True)
    ]
    # A period's pattern matches only its exact text; any other cell must be what strftime writes for its value, and
    # polars reads the year 0, which no date of Python's is in.
    exact = [
        values.is_not_null()
        if period
        else (
            values.is_not_null()
            & (dtype == pl.Time or values.dt.year() >= dt.MINYEAR)
            & (values.dt.strftime(written_as) == cells)
        ).fill_null(value=False)
        for values, written_as, period in zip(parsed, formats, periods, strict=True)
    ]
    wrong = cells.is_not_null() & ~pl.select(pl.any_horizontal(exact)).to_series()
    if (at := _first(wrong, row_one)) is not None:
        what = "date" if dtype == pl.Date else "time of day" if dtype == pl.Time else "time"
        msg = (
            f"the {cells.name!r} cell {cells[at - row_one]!r} is not a {what} written {' or '.join(formats)}: row {at}"
        )
        raise ReadError(msg)
    taken = (pl.when(ok).then(values) for values, ok in zip(parsed, exact, strict=True))
    return pl.select(pl.coalesce(taken)).to_series().alias(cells.name)


def check_ending(body: bytes, *, final_line_ending: bool) -> None:
    """Refuse a body whose end is not the file's: one cut at a row boundary reads whole otherwise."""
    if body.endswith((b"\n", b"\r")) != final_line_ending:
        written = "always ends its last line" if final_line_ending else "never ends its last line"
        msg = f"the body's end is not the file's, which {written} with a line ending: it was cut"
        raise ReadError(msg)
