"""A reader: a row struct and the stages that read a body into it, typed by the one engine for every format."""

import contextlib
import io
from collections.abc import Collection, Generator, Sequence

import msgspec
import polars as pl

from flatread.core import Column
from flatread.errors import ReadError
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
        from flatread.next._stages import Csv, Header  # noqa: PLC0415 (the default finder, built on this module)

        self.row = row
        self.format = format
        self.table: TableFinder = Header() if table is None else table
        self.shape = shape
        self.cells = tuple(cells)
        self.source = source
        self.empty = self.table.vouches if empty is None else empty
        self.engine = Engine(row, null_values=null_values, as_written=as_written, rows_required=not self.empty)
        self._null_values = list(null_values)
        # A CSV that is a header and its rows, read with nothing between, has a typed read of its own (Csv.typed): its
        # numbers by polars' parser, in one pass, and the grid's reading only where that read is unsure or refused.
        direct = isinstance(self.format, Csv) and isinstance(self.table, Header) and shape is None and not cells
        self._typed = (
            {column.label: _parsed_as(column) for column in self.engine.columns} if direct and not as_written else None
        )

    def frame(self, body: bytes) -> pl.DataFrame:
        """Read *body* into a DataFrame of the row's fields, typed, or raise ``ReadError``."""
        return self.read(body)[0]

    def rows(self, body: bytes) -> list[RowT]:
        """Read *body* into typed rows, one struct a row, or raise ``ReadError``."""
        return self.engine.rows(self.frame(body))

    def read(self, body: bytes) -> tuple[pl.DataFrame, ReadContext]:
        """Read *body* as :meth:`frame` does, and return what the read learned with it (a trailer's match)."""
        read = ReadContext(body, columns={column.field: column for column in self.engine.columns}, row=self.row)
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
        except BaseException as e:
            # pyo3's PanicException is a BaseException no module of polars' or fastexcel's exports: named alone.
            if type(e).__name__ != "PanicException":
                raise
            msg = f"the body made the reader's Rust code panic, as no file of the format does: {e}"
            raise ReadError(msg) from e

    def _direct(self, data: bytes, read: ReadContext) -> pl.DataFrame | None:
        """Return *data* read by the format's typed read, where it reads it and the engine takes its table."""
        assert self._typed is not None  # noqa: S101 (a reader that reads directly)
        table = self.format.typed(data, self._typed, self._null_values)  # ty: ignore[unresolved-attribute] (a Csv)
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


def _parsed_as(column: Column) -> type[pl.DataType]:
    """Return the dtype polars' parser reads *column* as in a typed read: a plain number's own, any other text."""
    if column.written is None and column.flag is None and column.scalar in (int, float):
        return pl.Int64 if column.scalar is int else pl.Float64
    return pl.String
