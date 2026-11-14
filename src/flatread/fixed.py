"""A fixed-width text table read natively into its row struct's fields.

:class:`FixedWidthFile` is a clean fixed-width text table: a header line, then a line per row, every line as long as
the last column's end, each field's cell at its :class:`~flatread.fields.Span` (characters *start* to
*end*, as Python slices count), only spaces between columns. Columns may touch (OCC's volumes write
``000000006000000000``), so their places are the struct's, not found in the header; the header must be the labels, each
in its column's place. polars reads the lines (a String column), slices each column and strips its spaces; a cell of
spaces is no value, and the cells are cleaned and typed as a :class:`TextFile`'s are (so :class:`Thousands` composes
with it). A line of another length, a character between columns or another header raises, naming the row. A format
subclasses it for what sits around the table: ``_header`` false for a file that writes none (its fields need no
``Label``), and :meth:`~FixedWidthFile._table` to read lines above or below it, returning the text of the ``_stated``
fields, which a title writes once for every row (OCC's report date).
"""

import typing
from collections.abc import Mapping

import msgspec
import polars as pl

from flatread.core import _MOMENTS, File, _first, _lines, _typed

__all__ = ["FixedWidthFile"]


class FixedWidthFile[RowT: msgspec.Struct](File[RowT]):
    """A clean fixed-width text table: each line its cells at the places its row struct's ``Span`` fields lay out."""

    #: Whether the table opens with a header line, its labels in their columns' places: a file that writes none (OCC's
    #: volume files) sets it false, and its fields need no ``Label``.
    _header: typing.ClassVar[bool] = True
    #: The fields a format reads from above the table, once for every row (a title's date), not from a column: they
    #: take no ``Span``, and the format's :meth:`_table` returns their text.
    _stated: typing.ClassVar[frozenset[str]] = frozenset[str]()

    def __init__(self, row: type[RowT], *, final_line_ending: bool | None = None) -> None:
        """Lay out a file of *row*: each field's :class:`~.fields.Span`, in order, none overlapping.

        Every line of the table is as long as the last span's end, and a character outside every span is a space. A
        date or time field gives the :class:`~flatread.fields.Format` it is written in.
        *final_line_ending* is :class:`CsvFile`'s. Raises ``ValueError`` for a field without a ``Span`` (but a stated
        one), a stated field that is none of *row*'s, no span, spans out of order or overlapping, a date or time field
        without a ``Format``, or what :class:`File` refuses.
        """
        super().__init__(row)
        if stray := self._stated - set(self._fields):
            msg = f"the format states {sorted(stray)}, which are no fields of {row.__name__}"
            raise ValueError(msg)
        spanned = [column for column in self._columns if column.field not in self._stated]
        for column in self._columns:
            if (column.span is None) != (column.field in self._stated):
                msg = f"the field {column.field!r} needs a Span, its column's place, and a stated field takes none"
                raise ValueError(msg)
            if column.scalar in _MOMENTS and column.written is None:
                msg = f"the field {column.field!r} needs the Format its file writes it in"
                raise ValueError(msg)
        self._spans = [column.span for column in spanned if column.span is not None]
        ends = [0, *(span.end for span in self._spans)]
        if not self._spans or any(
            span.start < end or span.end <= span.start for span, end in zip(self._spans, ends[:-1], strict=True)
        ):
            msg = f"the spans {self._spans} are not one or more, in order, each after the last, none empty"
            raise ValueError(msg)
        self._spanned = spanned
        self._width = self._spans[-1].end
        # The places between columns, which hold only spaces.
        self._gaps = [(end, span.start) for span, end in zip(self._spans, ends[:-1], strict=True) if span.start > end]
        self._final_line_ending = final_line_ending

    @typing.override
    def _label(self, name: str) -> str | None:
        return None if self._header else name

    def _table(self, lines: pl.Series) -> tuple[pl.Series, Mapping[str, str]]:
        """Return the table's lines, its header first if it has one, and the text of each stated field.

        The body's lines, unless a format reads lines above the table (a title) or below it (a trailer).
        """
        return lines, dict[str, str]()

    @typing.override
    def _read(self, body: bytes) -> pl.DataFrame:
        if not body or body.isspace():
            msg = "the body is empty"
            raise ValueError(msg)
        if self._final_line_ending is not None and body.endswith((b"\n", b"\r")) != self._final_line_ending:
            written = "always ends its last line" if self._final_line_ending else "never ends its last line"
            msg = f"the body's end is not the file's, which {written} with a line ending: it was cut"
            raise ValueError(msg)
        lines, stated = self._table(_lines(body))
        if stated.keys() != self._stated:
            msg = f"the format states {sorted(stated)}, not the fields {sorted(self._stated)}"
            raise ValueError(msg)
        header = int(self._header)
        line = pl.col("line")
        bad = pl.any_horizontal(
            line.str.len_chars() != self._width,
            *(line.str.slice(start, end - start).str.strip_chars(" ") != "" for start, end in self._gaps),
        )
        # A row is numbered from 1 under the header, which is row 0.
        if (at := _first(lines.to_frame("line").select(bad).to_series(), 1 - header)) is not None:
            where = "the header" if at < 1 else f"row {at}"
            msg = f"{where} {lines[at - 1 + header]!r} is not {self._width} characters, spaces between its columns"
            raise ValueError(msg)
        labels = [column.label for column in self._spanned]
        cells = lines.to_frame("line").select(
            line.str.slice(span.start, span.end - span.start).str.strip_chars(" ").alias(label)
            for label, span in zip(labels, self._spans, strict=True)
        )
        if header:
            if cells.is_empty() or list(cells.row(0)) != labels:
                msg = f"the header {lines[0] if len(lines) else None!r} is not the labels {labels} in their places"
                raise ValueError(msg)
            cells = cells.slice(1)
        named = {column.field: column.label for column in self._columns}
        cells = cells.with_columns(pl.lit(text, pl.String).alias(named[field]) for field, text in stated.items())
        # A cell of spaces holds no value, and so does an empty stated one.
        cells = cells.with_columns(pl.col(pl.String).replace("", None))
        return pl.DataFrame(
            [
                _typed(column, self._clean(cells[column.label], column), f"{column.label!r} cell")
                for column in self._columns
            ]
        )
