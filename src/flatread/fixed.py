"""A fixed-width text table read natively into its row struct's fields.

:class:`FixedWidthFile` is a clean fixed-width text table: a header line, then a line per row, every line as long as
the last column's end, each field's cell at its :class:`~flatread.fields.Span` (characters *start* to
*end*, as Python slices count), only spaces between columns. Columns may touch (OCC's volumes write
``000000006000000000``), so their places are the struct's, not found in the header; the header must be the labels, each
in its column's place. polars reads the lines (a String column), slices each column and strips its spaces; a cell of
spaces is no value, as is a cell that is one of *null_values* once stripped, and the cells are cleaned and typed as a
:class:`~flatread.core.TextFile`'s are (so :class:`~flatread.core.Thousands` composes with it). A line of another
length, a character between columns or another header raises, naming the row. A format subclasses it for what sits
around the table: ``_header`` false for a file that writes none (its fields need no ``Label``), and
:meth:`~FixedWidthFile._table` to read lines above or below it, returning the text of the ``_stated`` fields, which a
title writes once for every row (OCC's report date). A field another piece states (a zip member's name,
:meth:`~flatread.core.File._stated_fields`) takes no ``Span`` either, and that piece adds it.
"""

import typing
from collections.abc import Mapping, Sequence

import msgspec
import polars as pl

from flatread.core import _MOMENTS, File, _check_ending, _first, _lines, _typed
from flatread.errors import EmptyBodyError, LayoutError, ReadError

__all__ = ["FixedWidthFile"]


class FixedWidthFile[RowT: msgspec.Struct](File[RowT]):
    """A clean fixed-width text table: each line its cells at the places its row struct's ``Span`` fields lay out."""

    #: Whether the table opens with a header line, its labels in their columns' places: a file that writes none (OCC's
    #: volume files) sets it false, and its fields need no ``Label``.
    _header: typing.ClassVar[bool] = True
    #: The fields a format reads from above the table, once for every row (a title's date), not from a column: they
    #: take no ``Span``, and the format's :meth:`_table` returns their text.
    _stated: typing.ClassVar[frozenset[str]] = frozenset[str]()

    def __init__(
        self, row: type[RowT], *, null_values: Sequence[str] = (), final_line_ending: bool | None = None
    ) -> None:
        """Lay out a file of *row*: each field's :class:`~.fields.Span`, in order, none overlapping.

        Every line of the table is as long as the last span's end, and a character outside every span is a space. A date
        or time field gives the :class:`~flatread.fields.Format` it is written in. *null_values* are the file's tokens
        for no value, each matched to a cell stripped of its spaces. *final_line_ending* is
        :class:`~flatread.delimited.CsvFile`'s. Raises ``LayoutError`` for a field without a ``Span`` (but a stated
        one), a stated field that is none of *row*'s, no span, spans out of order or overlapping, a date or time field
        without a ``Format``, a piece listed after this reader (its ``_read`` would never run), or what
        :class:`~flatread.core.File` refuses.
        """
        mro = [cls for cls in type(self).__mro__ if cls is not File]
        if after := [cls.__name__ for cls in mro[mro.index(FixedWidthFile) + 1 :] if "_read" in vars(cls)]:
            msg = f"{type(self).__name__} lists {after} after its fixed-width reader, whose read never reaches them"
            raise LayoutError(msg)
        super().__init__(row)
        if stray := self._stated - set(self._fields):
            msg = f"the format states {sorted(stray)}, which are no fields of {row.__name__}"
            raise LayoutError(msg)
        stated = self._stated_fields()
        spanned = [column for column in self._columns if column.field not in stated]
        # The fields this reader types: its columns and its own stated ones, not another piece's (a member's name).
        self._typed = [column for column in self._columns if column.field not in stated - self._stated]
        for column in self._columns:
            if (column.span is None) != (column.field in stated):
                msg = f"the field {column.field!r} needs a Span, its column's place, and a stated field takes none"
                raise LayoutError(msg)
            if column.scalar in _MOMENTS and column.written is None:
                msg = f"the field {column.field!r} needs the Format its file writes it in"
                raise LayoutError(msg)
        self._spans = [column.span for column in spanned if column.span is not None]
        ends = [0, *(span.end for span in self._spans)]
        if not self._spans or any(
            span.start < end or span.end <= span.start for span, end in zip(self._spans, ends[:-1], strict=True)
        ):
            msg = f"the spans {self._spans} are not one or more, in order, each after the last, none empty"
            raise LayoutError(msg)
        self._spanned = spanned
        self._width = self._spans[-1].end
        # The places between columns, which hold only spaces.
        self._between = [
            (end, span.start) for span, end in zip(self._spans, ends[:-1], strict=True) if span.start > end
        ]
        self._final_line_ending = final_line_ending
        self._null_values = list(null_values)

    @typing.override
    def _stated_fields(self) -> frozenset[str]:
        return super()._stated_fields() | self._stated

    @typing.override
    def _label(self, name: str) -> str | None:
        return name if not self._header or name in self._stated_fields() else super()._label(name)

    def _table(self, lines: pl.Series) -> tuple[pl.Series, Mapping[str, str]]:
        """Return the table's lines, its header first if it has one, and the text of each stated field.

        The body's lines, unless a format reads lines above the table (a title) or below it (a trailer).
        """
        return lines, dict[str, str]()

    @typing.override
    def _read(self, body: bytes) -> pl.DataFrame:
        if not body or body.isspace():
            msg = "the body is empty"
            raise EmptyBodyError(msg)
        if self._final_line_ending is not None:
            _check_ending(body, final_line_ending=self._final_line_ending)
        lines, stated = self._table(_lines(body))
        if stated.keys() != self._stated:
            msg = f"the format states {sorted(stated)}, not the fields {sorted(self._stated)}"
            raise LayoutError(msg)
        header = int(self._header)
        line = pl.col("line")
        bad = pl.any_horizontal(
            line.str.len_chars() != self._width,
            *(line.str.slice(start, end - start).str.strip_chars(" ") != "" for start, end in self._between),
        )
        # A row is numbered from 1 under the header, which is row 0.
        if (at := _first(lines.to_frame("line").select(bad).to_series(), 1 - header)) is not None:
            where = "the header" if at < 1 else f"row {at}"
            msg = f"{where} {lines[at - 1 + header]!r} is not {self._width} characters, spaces between its columns"
            raise ReadError(msg)
        labels = [column.label for column in self._spanned]
        cells = lines.to_frame("line").select(
            line.str.slice(span.start, span.end - span.start).str.strip_chars(" ").alias(label)
            for label, span in zip(labels, self._spans, strict=True)
        )
        if header:
            if cells.is_empty() or list(cells.row(0)) != labels:
                msg = f"the header {lines[0] if len(lines) else None!r} is not the labels {labels} in their places"
                raise ReadError(msg)
            cells = cells.slice(1)
        named = {column.field: column.label for column in self._columns}
        cells = cells.with_columns(pl.lit(text, pl.String).alias(named[field]) for field, text in stated.items())
        # A cell of spaces holds no value, and so does an empty stated one, or one of the file's tokens.
        cells = cells.with_columns(pl.col(pl.String).replace(["", *self._null_values], None))
        return pl.DataFrame(
            [
                _typed(column, self._clean(cells[column.label], column), f"{column.label!r} cell")
                for column in self._typed
            ]
        )
