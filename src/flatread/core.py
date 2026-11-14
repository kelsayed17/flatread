"""The base every reader composes on: a body read into its row struct's fields, as a DataFrame or as typed rows.

**The row struct.** A reader is built from a msgspec struct whose fields, in the file's order, name their columns in
``Annotated`` metadata (:mod:`flatread.fields`): a ``Label`` (the header's text), and for a date or time its
``Format``, for a boolean its ``Flag``. Each column takes its field's type: ``str``, ``int``, ``float``, ``bool``, a
date, a time of day, a date and time; a ``Literal`` or ``NewType`` reads as its base. A field that admits ``None``
may be empty; a field with a default takes it where its file holds no value; any other empty cell raises. A label the
header writes twice is two fields, matched by place.

**What every reader promises.** :class:`File` checks the frame a format reads: its columns are the struct's fields, in
order, each typed as its field; there is at least one row (unless ``_rows_required`` is false); a float is finite
(unless it is one of ``_non_finite``); a null sits only where its field admits one; a ``Shape``'s text fullmatches. A
body that fails raises :class:`~flatread.errors.ReadError` naming the first row; a struct or switch that describes no
readable file raises :class:`~flatread.errors.LayoutError`, most when the reader is built. :meth:`File.rows` builds the
structs from the checked frame by position, without a second check.

**Composition.** A reader is one class doing one thing, and a format is a subclass naming the pieces it needs, each
overriding one step and calling the next: ``class _Volumes(TrailerCsv[Row], HeadersCsv[Row])``. The switches are
underscored class attributes a subclass sets (``_header = False``); the hooks are underscored methods it overrides
(:meth:`File._label` derives a label for a field without one, :meth:`File._format` a ``Format``). Two pieces here
compose with any reader of text cells: :class:`Thousands` reads ``1,782,319`` as its number, and :class:`Carried` fills
a group's label, printed on its first row only, down to the rows below. :class:`Reshaped` reshapes the frame another
reader made, and :class:`TextFile` is the base of every reader whose cells are text.

**Text cells** are typed strictly, as a CSV's are: a whole number or a number (trimmed), ``true`` or ``false`` or a
``Flag``'s words, a date or time exactly as its ``Format`` writes it. A cell holding nothing or only whitespace is no
value. A ``Format`` may name *alternatives*, each cell exactly one of them. A date ``Format`` without a day writes a
period: ``%Y`` (or ``%y``) and one of ``%m``, ``%-m``, ``%b``, ``%B`` or ``%q`` (a quarter, 1 to 4), so ``%Y:Q%q``
reads ``2022:Q4``, the quarter's first day, or its last with ``end=True``. A date written across two columns is one
field, its second column named by :class:`~flatread.fields.Joined`.
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

__all__ = ["Carried", "File", "Reshaped", "TextFile", "Thousands"]

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


def _scalar_type(annotation: object) -> object:
    """Return a field's scalar: ``float`` for ``float | None``, a ``NewType``'s base, ``str`` for a ``Literal``."""
    annotation = _bare(annotation)
    if isinstance(annotation, types.UnionType) or typing.get_origin(annotation) is typing.Union:
        (annotation,) = (arm for arm in typing.get_args(annotation) if arm is not types.NoneType)
    if isinstance(annotation, typing.NewType):
        return _scalar_type(annotation.__supertype__)
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
    """A file's reader, read two ways: :meth:`frame`, a DataFrame of its row struct's fields, and :meth:`rows`.

    Build one per file shape, at import, and share it: a reader holds no state between reads, so one serves every
    body and every thread. A format subclasses a reader of this class's family and implements :meth:`_read`.
    """

    #: The float fields that may hold ``NaN`` and infinities as values (SDMX's ``obs_value``); every other float field
    #: is finite, and polars' reading of ``nan``, ``inf`` or an overflowing ``1e400`` as a float is refused there.
    _non_finite: typing.ClassVar[Collection[str]] = ()
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
        Raises ``LayoutError`` for a field without a ``Label`` (unless the format derives one), a ``Format`` on a field
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
        scalars = {field.name: _scalar_type(field.type) for field in fields if field.name in self._given}
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
        scalar = _scalar_type(field.type)
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

    def _stated_fields(self) -> frozenset[str]:
        """Return the fields a piece states for every row, not read from the table's columns: none, unless one does.

        A trailer's or a title's group, a zip member's or a sheet's name: each piece that states fields adds its own to
        ``super()._stated_fields()``, and every piece that lays out the table's columns (a header's check, a long
        table's record) leaves them out.
        """
        return frozenset()

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
        """Read *body* into a frame of the row struct's fields, in order, typed as each field, or raise ReadError."""

    def frame(self, body: bytes) -> pl.DataFrame:
        """Read *body* into a DataFrame of the row struct's fields, one row per row of the file, in order.

        Raises :class:`~flatread.errors.ReadError` for a body the format refuses, naming where, and
        :class:`~flatread.errors.LayoutError` for a reader whose columns are not the fields or not typed as them
        (``'2'`` in an ``int`` field), which :meth:`rows` would build structs of unchecked. Both are ``ValueError``.
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
        One pass says whether any column fails; only then is each failing column's first row found.
        """
        checked = pl.lit(value=True) if among is None else pl.lit(among)
        floats = [name for name, dtype in frame.schema.items() if dtype == pl.Float64 and name not in self._non_finite]
        tests = [
            *(("infinite", name, ~pl.col(name).is_finite() & checked) for name in floats),
            *(("empty", name, pl.col(name).is_null() & checked) for name in required),
        ]
        if not tests:
            return
        anywhere = frame.select(test.any().alias(str(at)) for at, (_, _, test) in enumerate(tests)).row(0)
        failed: dict[str, dict[str, int]] = {"infinite": {}, "empty": {}}
        for (kind, name, test), failing in zip(tests, anywhere, strict=True):
            if failing and (at := _first(frame.select(test).to_series(), self._row_one)) is not None:
                failed[kind][name] = at
        if infinite := failed["infinite"]:
            row = min(infinite.values())
            msg = f"the {list(infinite)} columns hold NaN or an infinity, which is no number here: row {row}"
            raise ReadError(msg)
        if empty := failed["empty"]:
            msg = (
                f"the {list(empty)} columns have an empty cell, a short row or no column, and every cell holds a value:"
                f" row {min(empty.values())}"
            )
            raise ReadError(msg)

    def rows(self, body: bytes) -> list[RowT]:
        """Read *body* into typed rows: :meth:`frame`'s rows, one struct a row, raising as :meth:`frame` does."""
        # By position: frame() checked its columns are the struct's fields, in order, already typed. A column a list
        # each, zipped by map in C: about twice as fast as polars' iter_rows.
        return list(map(self._row, *(column.to_list() for column in self.frame(body).iter_columns()), strict=True))


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

        Raises ``LayoutError`` for a date or time field without the :class:`~flatread.fields.Format` its
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
        """Read *body* into a String frame of its cells, one column per header label, in order, or raise ReadError."""

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
    """Return *body* as text in the first of *encodings* that decodes it, or raise ``ReadError``.

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
    """Return the pattern a period written *written_as* fullmatches, or raise LayoutError for a format it is not.

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


def _check_ending(body: bytes, *, final_line_ending: bool) -> None:
    """Refuse a body whose end is not the file's: one cut at a row boundary reads whole otherwise."""
    if body.endswith((b"\n", b"\r")) != final_line_ending:
        written = "always ends its last line" if final_line_ending else "never ends its last line"
        msg = f"the body's end is not the file's, which {written} with a line ending: it was cut"
        raise ReadError(msg)
