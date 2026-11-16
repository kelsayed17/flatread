"""A reader: a row struct and the stages that read a body into it, typed one way for every format."""

import datetime as dt
import typing
from collections.abc import Collection, Sequence

import msgspec
import polars as pl

from flatread.core import Column, TextFile
from flatread.fields import Format as Written
from flatread.next._model import Cleaner, Format, ReadContext, Shape, Source, Table, TableFinder

__all__ = ["Reader"]


class Reader[RowT: msgspec.Struct]:
    """Reads a body into rows of *row*: opened by *source*, read by *format*, its table found, shaped, cleaned, typed.

    *row* is the schema: each field's column is its ``Label``, or its own name where it has none. *format* reads the
    file into a grid of text cells (``Csv(separator="|")``); *table* finds the table in it (the first row its header,
    unless another finder is given: ``Trailer(...)``, ``Titled(...)``); *shape* reshapes it (``Long(keys=...)``);
    *cells* cleans each column's text, in order (``[Padded()]``); *source* opens the body first (``Zip(member=...)``).
    Every cell is then typed by its field as :class:`~flatread.core.TextFile` types text, strictly: *null_values* are
    the file's words for no value, and a field of *as_written* keeps its text as the file writes it. *empty* is whether
    a table of no rows is an answer; ``None`` leaves it to the table stage (a trailer vouches for one, a header
    does not).

    A reader is immutable and holds no read's state: one serves every body and every thread.
    """

    def __init__(  # noqa: PLR0913 (a reader's stages)
        self,
        row: type[RowT],
        format: Format,  # noqa: A002 (the stage's name)
        *,
        table: TableFinder | None = None,
        shape: Shape | None = None,
        cells: Sequence[Cleaner] = (),
        source: Source | None = None,
        null_values: Collection[str] = (),
        as_written: Collection[str] = (),
        empty: bool | None = None,
    ) -> None:
        from flatread.next._stages import Header  # noqa: PLC0415 (the default finder, built on this module)

        self.row = row
        self.format = format
        self.table = Header() if table is None else table
        self.shape = shape
        self.cells = tuple(cells)
        self.source = source
        self.empty = table.vouches if empty is None and table is not None else bool(empty)
        self._engine = _Engine(self, null_values=null_values, as_written=as_written)

    def frame(self, body: bytes) -> pl.DataFrame:
        """Read *body* into a DataFrame of the row's fields, typed, or raise ``ReadError``."""
        return self._engine.frame(body)

    def rows(self, body: bytes) -> list[RowT]:
        """Read *body* into typed rows, one struct a row, or raise ``ReadError``."""
        return self._engine.rows(body)

    def read(self, body: bytes) -> tuple[pl.DataFrame, ReadContext]:
        """Read *body* as :meth:`frame` does, and return what the read learned with it (a trailer's match)."""
        return self._engine.read(body)


class _Engine[RowT: msgspec.Struct](TextFile[RowT]):
    """The typing and checks every reader shares: a TextFile whose cells its reader's stages read.

    shortcut: the prototype types and checks through 7.x's TextFile, so a ported reader reads values exactly as a 7.x
    text reader does; 8.0 proper moves that engine here and drops the per-read copy below for the ReadContext alone.
    """

    _context: ReadContext | None = None
    _meta_checked = True

    def __init__(self, reader: Reader[RowT], *, null_values: Collection[str], as_written: Collection[str]) -> None:
        self._reader = reader
        # shortcut: 7.x's TextFile takes these as class settings; 8.0 proper has no class settings to set.
        self._as_written = tuple(as_written)  # ty: ignore[invalid-attribute-access]
        self._rows_required = not reader.empty  # ty: ignore[invalid-attribute-access]
        super().__init__(reader.row, null_values=null_values)

    @typing.override
    def _label(self, name: str) -> str | None:
        return name  # 8.0: a field's label is its own name, where it names none

    @typing.override
    def _column(self, field: msgspec.structs.FieldInfo) -> Column:
        column = super()._column(field)
        if column.scalar is dt.date and column.written is None:
            column = column._replace(written=Written("%Y-%m-%d"))  # 8.0: a bare date is ISO 8601's, exactly
        return column

    def read(self, body: bytes) -> tuple[pl.DataFrame, ReadContext]:
        """Read *body* on a copy of this engine, returning the frame and the copy's context."""
        reading = object.__new__(type(self))
        vars(reading).update(vars(self), _reading=True)
        frame = super(TextFile, reading).frame(body)
        _check_meta(self._reader.row, frame, reading._row_numbers)
        return frame, typing.cast("ReadContext", reading._context)

    @typing.override
    def frame(self, body: bytes) -> pl.DataFrame:
        return self.read(body)[0]

    @typing.override
    def _clean(self, cells: pl.Series, column: Column) -> pl.Series:
        for cleaner in self._reader.cells:
            cells = cleaner.clean(cells, column)
        return cells

    @typing.override
    def _cells(self, body: bytes) -> pl.DataFrame:
        reader = self._reader
        read = self._context = ReadContext(
            body, columns={column.field: column for column in self._columns}, row=reader.row
        )
        opened = body if reader.source is None else reader.source.open(body, read)
        table = reader.table.find(reader.format.grid(opened, read), read)
        if reader.shape is not None:
            table = reader.shape.shape(table, self._columns, read)
        self._number_rows(table.numbers)
        return self._stating(table, read)

    def _stating(self, table: Table, read: ReadContext) -> pl.DataFrame:
        """Return *table*'s cells with each stated field's text on every row, the columns in the struct's order."""
        cells = table.cells
        if stated := self._reader.table.stated:
            cells = cells.with_columns(
                pl.lit(read.stated.get(field), pl.String).alias(field) for field in sorted(stated)
            )
            order = [column.label for column in self._columns if column.label in cells.columns]
            cells = cells.select(*order, *(name for name in cells.columns if name not in order))
        return cells


#: Each msgspec ``Meta`` bound, the polars test a value passes, and how a failure reads.
_BOUNDS: dict[str, tuple[typing.Callable[[pl.Expr, object], pl.Expr], str]] = {
    "ge": (lambda value, bound: value >= bound, "at least"),
    "gt": (lambda value, bound: value > bound, "more than"),
    "le": (lambda value, bound: value <= bound, "at most"),
    "lt": (lambda value, bound: value < bound, "less than"),
    "multiple_of": (lambda value, bound: value % bound == 0, "a multiple of"),
    "min_length": (lambda value, bound: value.str.len_chars() >= bound, "at least this long:"),
    "max_length": (lambda value, bound: value.str.len_chars() <= bound, "at most this long:"),
    "pattern": (lambda value, bound: value.str.contains(str(bound)), "matching"),
}


def _metas(annotation: object) -> list[msgspec.Meta]:
    """Return every msgspec ``Meta`` in *annotation*, through ``Annotated`` and unions."""
    found: list[msgspec.Meta] = []
    stack = [annotation]
    while stack:
        current = stack.pop()
        if typing.get_origin(current) is typing.Annotated:
            found += [meta for meta in typing.get_args(current)[1:] if isinstance(meta, msgspec.Meta)]
        stack.extend(typing.get_args(current))
    return found


def _check_meta(row: type[msgspec.Struct], frame: pl.DataFrame, numbers: pl.Series | None) -> None:
    """Raise ``ReadError`` naming the first value of *frame* outside its field's msgspec ``Meta``, by its row."""
    from flatread.errors import ReadError  # noqa: PLC0415

    for field in msgspec.structs.fields(row):
        for meta in _metas(field.type):
            for name, (passes, words) in _BOUNDS.items():
                if (bound := getattr(meta, name)) is None:
                    continue
                value = pl.col(field.name)
                failed = frame.select((value.is_not_null() & ~passes(value, bound)).arg_true()).to_series()
                if failed.len():
                    at = int(failed[0])
                    where = f"row {numbers[at]}" if numbers is not None and numbers.len() > at else f"row {at + 1}"
                    msg = f"{where}: {field.name} is {frame[field.name][at]!r}, not {words} {bound!r}"
                    raise ReadError(msg)
