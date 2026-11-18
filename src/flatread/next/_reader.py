"""A reader: a row struct and the stages that read a body into it, typed by the one engine for every format."""

import contextlib
import io
from collections.abc import Collection, Generator, Sequence

import msgspec
import polars as pl

from flatread.core import Column, _panicked
from flatread.errors import LayoutError, ReadError
from flatread.next._engine import Engine
from flatread.next._model import Cleaner, Format, ReadContext, Shape, Source, Stream, TableFinder

__all__ = ["Reader"]


class Reader[RowT: msgspec.Struct]:
    """Reads a body into rows of *row*: opened by *source*, read by *format*, its table found, shaped, cleaned, typed.

    *row* is the schema: each field's column is its ``Label``, or its own name where it has none. *source* opens the
    body as a stream (``Zip(member=...)``; by default the body as it is); *format* reads it into a grid
    (``Csv(separator="|")``, ``Sheet(sheet="DATA")``); *table* finds the table in it (the first row its header, unless
    another finder is given: ``Trailer(...)``, ``Titled(...)``); *shape* reshapes it (``Long(keys=...)``); *cells*
    cleans each column's text, in order (``[Padded()]``). The engine then types every cell by its field, strictly:
    *null_values* are the file's words for no value, and a field of *as_written* keeps its text exactly as written.
    *empty* is whether a table of no rows is an answer; ``None`` leaves it to the table stage (a trailer vouches for
    one, a header does not).

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
        from flatread.next._stages import Csv, Header, Sheet  # noqa: PLC0415 (the default finder, built on this module)

        self.row = row
        self.format = format
        self.table: TableFinder = Header() if table is None else table
        self.shape = shape
        self.cells = tuple(cells)
        self.source = source
        self.empty = self.table.vouches if empty is None else empty
        self.engine = Engine(
            row,
            null_values=null_values,
            as_written=as_written,
            rows_required=not self.empty,
            exact=getattr(format, "exact", False),
        )
        self._null_values = list(null_values)
        header = getattr(
            self.table, "header", None
        )  # a finder's header rule, which a finder of a format's own may lack
        if header is not None:
            header.check(self.engine.columns, self.engine.required)
            if shape is not None:
                msg = "a header rule reads the struct's columns and a shape the table as written: a reader takes one"
                raise LayoutError(msg)
        # A finder or rule that finds a column by its label declares it (by_name), so a Label may name alternatives.
        aliased = [column.field for column in self.engine.columns if column.aliases]
        if aliased and not (getattr(header, "by_name", False) or getattr(self.table, "by_name", False)):
            msg = f"the fields {aliased} name Label alternatives, which only a header read by name takes"
            raise LayoutError(msg)
        # A CSV or a sheet that is a header and its rows, read with nothing between, has a typed read of its own
        # (Csv.typed, Sheet.typed): its numbers by polars' parser or fastexcel's strict load, in one pass, and the
        # grid's reading only where that read is unsure or refused. A sheet's every field is a plain one, as a
        # column's kind then says all its cells are: a text column of a date's or a flag's is the grid's to read.
        typed = {column.label: _parsed_as(column) for column in self.engine.columns}
        direct = (
            (
                (isinstance(self.format, Csv) and not self.format.misquoted)
                or (isinstance(self.format, Sheet) and all(_plain(column) for column in self.engine.columns))
            )
            and isinstance(self.table, Header)
            and self.table.header is None
        )
        self._typed = typed if direct and shape is None and not cells and not as_written else None

    def frame(self, body: bytes) -> pl.DataFrame:
        """Read *body* into a DataFrame of the row's fields, typed, or raise ``ReadError``."""
        return self.read(body)[0]

    def rows(self, body: bytes) -> list[RowT]:
        """Read *body* into typed rows, one struct a row, or raise ``ReadError``."""
        return self.engine.rows(self.frame(body))

    def read(self, body: bytes) -> tuple[pl.DataFrame, ReadContext]:
        """Read *body* as :meth:`frame` does, and return what the read learned with it (a trailer's match)."""
        read = ReadContext(
            body,
            columns={column.field: column for column in self.engine.columns},
            row=self.row,
            required=self.engine.required,
            null_values=frozenset(self._null_values),
            empty=self.empty,
        )
        try:
            with self._opened(body, read) as stream:
                if self._typed is None:
                    grid = self.format.grid(stream, read)
                else:
                    data = stream.read()
                    if (typed := self._direct(data, read)) is not None:
                        return typed, read
                    grid = self.format.grid(io.BytesIO(data), read)
            table = self.table.find(grid, read)
            if self.shape is not None:
                table = self.shape.shape(table, self.engine.columns, read)
            return self.engine.frame(table, read, self.cells), read
        except ReadError as e:
            e.member = e.member or read.member  # where in the zip it went wrong
            raise
        except BaseException as e:
            if not _panicked(e):
                raise
            msg = f"the body made the reader's Rust code panic, as no file of the format does: {e}"
            raise ReadError(msg, member=read.member) from e

    def _direct(self, data: bytes, read: ReadContext) -> pl.DataFrame | None:
        """Return *data* read by the format's typed read, where it reads it and the engine takes its table."""
        assert self._typed is not None  # noqa: S101 (a reader that reads directly)
        table = self.format.typed(data, self._typed, self._null_values)  # ty: ignore[unresolved-attribute] (a Csv or a Sheet)
        if table is None:
            return None
        try:
            return self.engine.frame(table, read)
        except ReadError:  # the grid's reading raises it, naming the cell by its line
            return None

    @contextlib.contextmanager
    def _opened(self, body: bytes, read: ReadContext) -> Generator[Stream]:
        if self.source is None:
            yield io.BytesIO(body)
        else:
            with self.source.open(body, read) as stream:
                yield stream


#: The dtype a typed read reads a plain field of each scalar as; a text's is text.
_PARSED: dict[object, type[pl.DataType]] = {int: pl.Int64, float: pl.Float64, bool: pl.Boolean}


def _plain(column: Column) -> bool:
    """Return whether *column* is a plain one: text, a number or a boolean, read with no format and no flag."""
    return column.written is None and column.flag is None and column.scalar in (str, int, float, bool)


def _parsed_as(column: Column) -> type[pl.DataType]:
    """Return the dtype a typed read reads *column* as: a plain number's or boolean's own, any other text.

    A CSV's parser reads a boolean as text, which the engine types; a sheet's load holds it as a boolean cell.
    """
    return _PARSED.get(column.scalar, pl.String) if _plain(column) else pl.String
