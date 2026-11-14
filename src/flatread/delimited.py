"""A delimited file read into its row struct's fields: by polars' ``read_csv`` when clean, or as a grid of text cells.

* :class:`CsvFile`, the clean delimited file (RFC 4180): a header, then rows exactly as wide, ``"`` its quoting,
  nothing around them. polars' ``read_csv`` reads it with the struct's ``schema``, which refuses another column or a
  cell that is not its type, raised as ``ValueError`` (polars' errors are not), which ``body_reader`` turns into
  ``ResponseParseError``. Every row's width is counted in the bytes after the read (polars pads a short row with
  nulls), and a bad one named. A date or time column is parsed by its ``Format``, each cell exactly as the format
  writes it (polars' ``%Y`` alone takes ``71`` as the year 71); a flag by its words; an empty cell, quoted or not, is
  no value. Its options are the file's own: ``null_values`` (``NA``, ``--``), ``separator``, ``final_line_ending``.
  A format whose file writes no header (OCC's day's directory) sets ``_header`` false: every line is a row, its cells
  the fields by position, which need no ``Label``.
* :class:`HeadersCsv`, a file published under more than one header (EIA's BALANCE files, ECB's ``ACTION``): a body's
  header must be one of ``_headers``, and a field it lacks reads as null.
* :class:`TrailerCsv`, a file whose last line is a trailer (FINRA's record count, Nasdaq's time stamp): it must match
  ``_trailer``, a ``count`` group must be the rows read, and a group named for a field is that field on every row
  (the Reg SHO lists' ``generated_at``).
* :class:`NamedCsv`, a file whose header names its columns, any of its struct's labels in any order (SEC's data sets,
  whose quarters carry more columns or fewer): a field it lacks reads as null. A ``Label``'s *alternatives* are other
  words a year's header writes it in.
* :class:`TitledCsv`, a file whose header sits below title lines (Nasdaq's title and issue count, IBKR's ``#BOF``):
  each must match its regex in ``_titles``, and a ``count`` group must be the rows read.
* :class:`PreambleCsv`, a file whose header sits below a preamble of any length, unread (the Fed's research files'
  sources and notes): the header is the first line opening with the struct's first label.
* :class:`PaddedCsv`, a file whose cells are padded with spaces (CFTC's, Cboe's ``10/02/2019, 794943``): each cell
  is read as text, its spaces stripped, then typed strictly, as a :class:`TextFile`'s cell is.
* :class:`SpilledCsv`, an unquoted file that may write its separator inside one text field (SEC's ``BAM| ENTMT INC``):
  the surplus cells of a wide row belong to that field.
* :class:`ThousandsCsv`, a file whose numbers are grouped in thousands (``1,782,319``): each number cell holding a
  comma must be grouped so, and is read without them.
* :class:`SectionedCsv`, a file of several tables one after another, each opening with a header line its regex
  matches; one is read as a file of its own.
* :class:`CsvText`, a delimited file's grid of text cells as the ``csv`` module reads it (ragged rows, a line break
  inside a quoted cell), cut as a :class:`~flatread.core.GridFile` cuts its table.

The pieces compose, each overriding one step (``_table``) and calling the next: FINRA's short volume is
``class _ShortVolumeCsv(TrailerCsv, HeadersCsv)``, IBKR's FTP files ``class _FtpCsv(TitledCsv, TrailerCsv)``.
"""

import codecs
import csv
import datetime as dt
import io
import re
import typing
from collections.abc import Collection, Mapping, Sequence
from typing import Final, Literal

import msgspec
import polars as pl

from flatread.core import (
    _DTYPES,
    _MOMENTS,
    _NUL,
    File,
    Thousands,
    _check_ending,
    _Column,
    _first,
    _flagged,
    _lines,
    _moment,
    _repeated,
    _stated,
    _temporal,
    _typed,
)
from flatread.errors import EmptyBodyError, LayoutError, RaggedRowError, ReadError
from flatread.fields import Flag, Format
from flatread.grids import GridFile

__all__ = [
    "CsvFile",
    "CsvText",
    "HeadersCsv",
    "NamedCsv",
    "PaddedCsv",
    "PreambleCsv",
    "SectionedCsv",
    "SpilledCsv",
    "ThousandsCsv",
    "TitledCsv",
    "TrailerCsv",
]

#: A UTF-8 byte order mark, which polars and the field count skip.
_BOM: Final = codecs.BOM_UTF8


#: A boolean's words where its field names no Flag: exactly these, as XML Schema writes them.
_BOOLEAN_WORDS: Final = Flag("true", "false")


#: What an error calls a field of each of them.
_KINDS: Final[Mapping[object, str]] = {dt.date: "date", dt.datetime: "datetime", dt.time: "time-of-day"}


class CsvFile[RowT: msgspec.Struct](File[RowT]):
    """A clean delimited file, read by ``pl.read_csv`` with a schema its row struct's ``Label`` fields lay out."""

    #: The character a cell is quoted with (RFC 4180's): a format that quotes nothing sets ``None``, so a quote in a
    #: cell is text (Nasdaq's ``12" PIPE``), where polars would read an unpaired one as opening a cell.
    _quote_char: typing.ClassVar[str | None] = '"'
    #: Whether the file opens with a header line: a format whose file writes none (OCC's directory of listed products)
    #: sets it false, and its rows are read by position, its fields' names their labels.
    _header: typing.ClassVar[bool] = True
    #: Whether a line ends with one separator more, an empty cell past its last: ``"always"``, every line, the header's
    #: too (IBKR's ``|``-ended lines), read as a last column of an empty label that never holds a value (see
    #: :attr:`File._blank_columns`); ``"sometimes"``, a row may (Cboe's 2005 VX archive, a query's rows under a shorter
    #: header), and the separator closing a line is dropped where written, so a row whose last field's cell is empty is
    #: then one short. ``None``, the default: a line ends with its last cell. Unlike :attr:`File._blank_columns`, the
    #: closing cell has no label: with ``"always"`` the header's is empty, with ``"sometimes"`` it has none.
    _closing_separator: typing.ClassVar[Literal["always", "sometimes"] | None] = None

    def __init__(
        self,
        row: type[RowT],
        *,
        null_values: Sequence[str] = (),
        final_line_ending: bool | None = None,
        separator: str = ",",
    ) -> None:
        """Lay out a file of *row*, each of whose fields names its column with a :class:`~.fields.Label`.

        Each column takes its field's type, a date or time field's :class:`~flatread.fields.Format` gives
        the ``strptime`` format the file writes it in (``Format("%m-%d-%Y")``) and, for a time the file writes in one
        zone, that zone, and a boolean field's :class:`~flatread.fields.Flag` its words (``true`` and
        ``false`` without one). *null_values* are the file's tokens for no value, ``read_csv``'s own.
        *final_line_ending* is whether the file always ends its last line with a line ending, so a body that ends
        otherwise was cut at a row boundary, which reads whole otherwise; ``None`` leaves it unchecked. *separator* is
        the one character between a row's cells (``|`` for CPC's tables). Raises ``ValueError`` for a field without a
        ``Label``, a date or time field without a ``Format``, a ``Format`` on another field or with a zone on a date, a
        ``Flag`` on a field that is no boolean, or a field of a type no column reads.
        """
        super().__init__(row)
        self._dates: dict[str, tuple[pl.Date | pl.Datetime | pl.Time, Format]] = {}
        self._flags: dict[str, Flag] = {}
        # Keyed as polars names a header's columns: a label's repeat by its place (OCC's two Fillers).
        labels = [column.label for column in self._columns]
        unread = [*self._unread, *self._blank_columns, *([""] if self._closing_separator == "always" else [])]
        named = _polars_names([*labels, *unread])
        names, others = named[: len(labels)], named[len(labels) :]
        self._names = dict(zip(names, (column.field for column in self._columns), strict=True))
        for column in self._columns:
            if column.flag is not None or column.scalar is bool:
                # polars reads ``TRUE`` and ``tRuE`` as true: a boolean is read as text, then by its words.
                self._flags[column.field] = column.flag or _BOOLEAN_WORDS
            elif column.scalar in _MOMENTS:
                if column.written is None:
                    kind = _KINDS[column.scalar]
                    msg = f"the {kind} field {column.field!r} needs the Format its file writes it in"
                    raise LayoutError(msg)
                self._dates[column.field] = (_temporal(column.scalar, column.written), column.written)
        self._schema = dict(zip(names, map(self._dtype, self._columns), strict=True))
        # The columns no field reads: text under their own names, kept through every step to _read, which drops them.
        self._unread_names = others[: len(self._unread)]
        self._blank_names = dict(zip(others[len(self._unread) :], unread[len(self._unread) :], strict=True))
        self._names.update((name, name) for name in others)
        self._schema.update(dict.fromkeys(others, pl.String))
        self._null_values = list(null_values) or None
        self._final_line_ending = final_line_ending
        self._separator = separator

    def _dtype(self, column: _Column) -> type[pl.DataType]:
        """Return the polars type *column*'s cells are read as: its field's, or text for a flag, date or time."""
        return _csv_dtype(column)

    def _stated_columns(self, groups: Collection[str]) -> dict[str, _Column]:
        """Return the columns the file states once (a group of *groups* names each), keyed as polars names them.

        Each is taken out of the table read and out of the flag and date parsing after it: :func:`_stated` types it.
        """
        fields = list(self._names)[: len(self._columns)]  # the columns no field reads come after the fields'
        stated = {name: column for name, column in zip(fields, self._columns, strict=True) if column.field in groups}
        for name, column in stated.items():
            del self._schema[name]
            self._dates.pop(column.field, None)
            self._flags.pop(column.field, None)
        return stated

    @typing.override
    def _label(self, name: str) -> str | None:
        return super()._label(name) if self._header else name

    def _closed(self, body: bytes) -> bytes:
        """Return *body* without the separator closing a line, where one sometimes does (:attr:`_closing_separator`)."""
        if self._closing_separator != "sometimes":
            return body
        return re.sub(re.escape(self._separator.encode()) + rb"(?=\r?\n)", b"", body)

    def _check(self, body: bytes) -> None:
        """Refuse a body its format knows is not its file, once its rows are known as wide as its header.

        Nothing, unless a format checks more (SDMX's values).
        """

    def _quoting(self, body: bytes) -> str | None:
        """Return the character *body*'s cells are quoted with: :attr:`_quote_char`, unless a format reads the body."""
        del body
        return self._quote_char

    def _polars(
        self,
        body: bytes,
        *,
        missing_columns: Literal["insert", "raise"] = "raise",
        schema: Mapping[str, type[pl.DataType]] | None = None,
    ) -> pl.DataFrame:
        """Read *body* with ``read_csv``, its cells as the layout's columns, or *schema*'s (its header's, in order).

        polars' error is raised as ``ValueError``, naming the row where a row's width is what polars refused. A long
        table's columns are the ones its header lays out (:meth:`File._layout`).
        """
        quote_char = self._quoting(body)
        layout = renamed = None
        if schema is None:
            header = _header(body, self._separator, quote_char) if self._header else None
            layout = None if header is None else self._layout(header)
            renamed = header if layout is not None else None
            schema = (
                self._schema
                if layout is None
                else dict(
                    zip(_polars_names([column.label for column in layout]), map(self._dtype, layout), strict=True)
                )
            )
        try:
            frame = pl.read_csv(
                body,
                has_header=self._header,
                separator=self._separator,
                schema=dict(schema),
                null_values=self._null_values,
                missing_columns=missing_columns,
                quote_char=quote_char,
                # A laid-out table's names are its header's, as the csv module reads them (polars keeps ``""``);
                # a layout that is not the header's (a member's, by the struct) is left to polars' refusal.
                new_columns=None if renamed is None or list(schema) != _polars_names(renamed) else list(schema),
            )
        except pl.exceptions.PolarsError as e:
            _check_widths(body, self._separator, quoting=quote_char is not None, header=self._header)
            if not self._header and len(first := _header(body, self._separator, quote_char)) != len(schema):
                msg = f"row 1 has {len(first)} fields, not the file's {len(schema)}"
                raise RaggedRowError(msg) from e
            self._check(body)  # its rows as wide as its header: a format's own check may name the cell polars refused
            try:  # the rows are as wide as the header: read as text, the cell polars refused is named by its row
                mistyped = _mistyped(
                    pl.read_csv(
                        body,
                        has_header=self._header,
                        separator=self._separator,
                        schema=dict.fromkeys(schema, pl.String),
                        null_values=self._null_values,
                        missing_columns=missing_columns,
                        quote_char=quote_char,
                    ),
                    schema,
                )
            except pl.exceptions.PolarsError:
                mistyped = None
            raise ReadError(mistyped or str(e)) from e
        if layout is None or not self._repeats:
            return frame
        # A long table's repeated heading is read as polars names it (``Difference_duplicated_0``), which a heading may
        # write too: it is named by its heading, a NUL and its number instead, which no heading holds.
        named = _repeated([column.label for column in layout], "{label}" + _NUL * 2 + "{n}", 0, empties=True)
        return frame.rename(dict(zip(frame.columns, named, strict=True)))

    @typing.override
    def _read(self, body: bytes) -> pl.DataFrame:
        if not body or body.isspace():  # no copy of the body, as strip() would make
            msg = "the body is empty"
            raise EmptyBodyError(msg)
        if self._final_line_ending is not None:
            _check_ending(body, final_line_ending=self._final_line_ending)
        if body.startswith((b"\n", b"\r\n"), len(_BOM) if body.startswith(_BOM) else 0):  # no copy of the body
            msg = "the body opens with a blank line, not its header"
            raise ReadError(msg)
        # A long table's key columns keep their labels, and its key and value fields have no column: not strict, and
        # a field without a column is not parsed.
        frame = self._table(body).rename(self._names, strict=False)
        # polars reads a quoted empty cell ("") as text; it holds no value, as an unquoted empty cell does.
        frame = frame.with_columns(pl.col(pl.String).replace("", None))
        for name, label in self._blank_names.items():
            if name in frame.columns and (at := _first(frame[name].is_not_null())) is not None:
                closing = label == "" and self._closing_separator == "always"
                what = "the line's closing separator is followed by" if closing else f"the {label!r} column holds"
                msg = f"{what} {frame[name][at - 1]!r}, and it never holds a value: row {at}"
                raise ReadError(msg)
        frame = frame.drop(*self._unread_names, *self._blank_names, strict=False)
        # Parsed under its label, which an error names, as every other cell's error does.
        labels = {column.field: column.label for column in self._columns}
        frame = frame.with_columns(
            _flagged(frame[name].alias(labels.get(name, name)), flag).alias(name)
            for name, flag in self._flags.items()
            if name in frame.columns
        )
        frame = frame.with_columns(
            _moment(frame[name].alias(labels.get(name, name)), *parse).alias(name)
            for name, parse in self._dates.items()
            if name in frame.columns
        )
        if self._numbered is None:
            return frame
        # shortcut: a row's number is its line, a line a row under the header; a file with a cell over several lines
        # numbers its later rows from its earlier ones, until a long table needs its rows' lines exactly.
        lines = pl.int_range(1 + self._header, 1 + self._header + frame.height, dtype=pl.Int64, eager=True)
        return frame.insert_column(0, lines.alias(self._numbered))

    def _table(self, body: bytes) -> pl.DataFrame:
        """Read *body*'s table, a row of cells per row of the file, each row checked as wide as the header."""
        body = self._closed(body)
        self._check(body)
        frame = self._polars(body)
        if not _full(body, frame, self._separator, header=self._header):
            # names the row the count found
            _check_widths(body, self._separator, quoting=self._quoting(body) is not None, header=self._header)
            msg = "the body's separators are not as many as its rows, each as wide as the header, hold"
            raise RaggedRowError(msg)
        return frame


class HeadersCsv[RowT: msgspec.Struct](CsvFile[RowT]):
    """A clean file published under more than one header: a field a body's header lacks reads as null.

    A format subclasses it and names its headers, :attr:`_headers`, each a run of its struct's labels; one whose headers
    are not a short list overrides :meth:`_check_header` instead (Treasury's rates: ``Date``, then that year's columns).
    """

    #: The headers the file has been published with; a format's subclass (or its constructor, ECB's) sets them.
    _headers: Collection[tuple[str, ...]] = ()

    def __init__(
        self,
        row: type[RowT],
        *,
        null_values: Sequence[str] = (),
        final_line_ending: bool | None = None,
        separator: str = ",",
    ) -> None:
        """Lay out a file of *row* (see :class:`CsvFile`); refuse a header naming a label no field has."""
        super().__init__(row, null_values=null_values, final_line_ending=final_line_ending, separator=separator)
        if stray := {label for header in self._headers for label in header} - self._names.keys():
            msg = f"the headers name {sorted(stray)}, which no field of {row.__name__} labels"
            raise LayoutError(msg)

    def _check_header(self, header: list[str]) -> None:
        """Refuse a *header* that is none of the file's: a column it lacks reads as null, so any column could go."""
        if tuple(header) not in self._headers:
            msg = f"the header {header!r} is none of the {len(self._headers)} the {self._row.__name__} file has had"
            raise ReadError(msg)

    @typing.override
    def _table(self, body: bytes) -> pl.DataFrame:
        body = self._closed(body)
        quote = self._quoting(body)
        header = _header(body, self._separator, quote)
        self._check_header(header)
        # Keyed as polars names the header's columns: a repeat by its place (Nasdaq's two Fillers).
        names = _polars_names(header)
        # A label written more often than its fields take it (two where one field has it) is a column twice.
        if any(name not in self._schema and label in self._schema for label, name in zip(header, names, strict=True)):
            msg = f"the header {header!r} names a column twice"
            raise ReadError(msg)
        if unknown := [label for label in header if label not in self._schema]:
            msg = f"the header names {unknown}, which no field of {self._row.__name__} labels"
            raise ReadError(msg)
        # Read under the header's own cells, so polars refuses a long row (a column the header leaves out would take
        # its cell) and the count finds a short one; then each column it leaves out is a column of nulls.
        frame = self._polars(body, schema={name: self._schema[name] for name in names})
        if not _full(body, frame, self._separator):
            # names the row the count found
            _check_widths(body, self._separator, quoting=quote is not None)
            msg = "the body's separators are not as many as its rows, each as wide as the header, hold"
            raise RaggedRowError(msg)
        self._check(body)
        return frame.select(
            pl.col(name) if name in frame.columns else pl.lit(None, dtype).alias(name)
            for name, dtype in self._schema.items()
        )


class NamedCsv[RowT: msgspec.Struct](CsvFile[RowT]):
    """A clean file whose header names its columns: any of its struct's labels, in any order, each once.

    A field the header lacks reads as null, and a label it writes that no field has raises. A field's ``Label`` may
    name *alternatives*, other words a header writes it in (``Label("file_number", alternatives=("file_no",))``). A
    format subclasses it and names :attr:`_required_labels`, the labels every header names, and a format whose headers
    write its labels another way folds each header cell with :meth:`_heading` (SEC's, in any case).
    """

    _by_name = True
    #: The labels every header names, each a field's own label; a format's subclass (or its constructor) sets them.
    _required_labels: Collection[str] = ()

    def __init__(
        self,
        row: type[RowT],
        *,
        null_values: Sequence[str] = (),
        final_line_ending: bool | None = None,
        separator: str = ",",
    ) -> None:
        """Lay out a file of *row* (see :class:`CsvFile`); refuse a label two fields share or a carried one none has."""
        super().__init__(row, null_values=null_values, final_line_ending=final_line_ending, separator=separator)
        words = [(word, column.label) for column in self._columns for word in (column.label, *column.aliases)]
        self._labeled = dict(words)
        if len(self._labeled) != len(words):
            msg = f"two fields of {row.__name__} share a label: {sorted(word for word, _ in words)}"
            raise LayoutError(msg)
        if stray := set(self._required_labels) - self._names.keys():
            msg = f"the required labels {sorted(stray)} are no field of {row.__name__}'s labels"
            raise LayoutError(msg)

    def _heading(self, cell: str) -> str:
        """Return the label a header *cell* writes: the cell, unless a format folds its headers' wording."""
        return cell

    @typing.override
    def _table(self, body: bytes) -> pl.DataFrame:
        body = self._closed(body)
        try:  # polars, and _header, read a header that is not UTF-8 lossily
            body[: end if (end := body.find(b"\n")) >= 0 else len(body)].decode()
        except UnicodeDecodeError as e:
            msg = f"the header is not UTF-8: {e}"
            raise ReadError(msg) from e
        header = _header(body, self._separator, self._quoting(body))
        found = [(cell, self._labeled.get(self._heading(cell))) for cell in header]
        if unknown := [cell for cell, label in found if label is None]:
            msg = f"the header names {unknown}, which no field of {self._row.__name__} labels"
            raise ReadError(msg)
        named = {cell: label for cell, label in found if label is not None}
        if len(set(named.values())) != len(header):
            msg = f"the header {header!r} names a column twice"
            raise ReadError(msg)
        if missing := [label for label in self._required_labels if label not in named.values()]:
            msg = f"the header {header!r} lacks {missing}, which every one names"
            raise ReadError(msg)
        self._check(body)
        # Read under the header's own cells, in its order, so polars refuses a long row (an inserted column would
        # take its cell) and the count finds a short one: as fast as a fixed header.
        frame = self._polars(body, schema={cell: self._schema[label] for cell, label in named.items()})
        if not _full(body, frame, self._separator):
            # names the row the count found
            _check_widths(body, self._separator, quoting=self._quoting(body) is not None)
            msg = "the body's separators are not as many as its rows, each as wide as the header, hold"
            raise RaggedRowError(msg)
        frame = frame.rename(named)
        return frame.select(
            pl.col(label) if label in frame.columns else pl.lit(None, dtype).alias(label)
            for label, dtype in self._schema.items()
        )


class TrailerCsv[RowT: msgspec.Struct](CsvFile[RowT]):
    """A clean file whose last line is a trailer (a record count, the time it was written), not a row.

    A format subclasses it and names its trailer: :attr:`_trailer`, the regex the last line matches. A group named
    ``count`` is the file's number of rows, checked; a group named for a field is that field's value on every row (the
    time the file was written), typed as the field (:func:`_stated`), a field no column holds, which takes no
    ``Label``. The trailer is what a cut body lacks, so it vouches for the table it closes, a table of no rows too.
    """

    _rows_required = False
    #: The regex a file's last line matches; each format's subclass sets it.
    _trailer: typing.ClassVar[re.Pattern[str]]

    def __init__(
        self,
        row: type[RowT],
        *,
        null_values: Sequence[str] = (),
        final_line_ending: bool | None = None,
        separator: str = ",",
    ) -> None:
        """Lay out a file of *row* (see :class:`CsvFile`), each field a trailer's group names stated, not a column."""
        super().__init__(row, null_values=null_values, final_line_ending=final_line_ending, separator=separator)
        self._stated = self._stated_columns(self._trailer.groupindex)

    @typing.override
    def _label(self, name: str) -> str | None:
        return name if name in self._trailer.groupindex else super()._label(name)

    @typing.override
    def _table(self, body: bytes) -> pl.DataFrame:
        end = len(body) - (2 if body.endswith(b"\r\n") else 1 if body.endswith((b"\n", b"\r")) else 0)
        cut = body.rfind(b"\n", 0, end)
        last = body[cut + 1 : end].removesuffix(b"\r").decode("utf-8", errors="replace")
        if cut < 0 or (match := self._trailer.fullmatch(last)) is None:
            msg = f"the last line {last!r} is not the file's trailer: the body was cut"
            raise ReadError(msg)
        # shortcut: the table is copied without its trailer, since polars 2.1 has no option that ends a read at a line
        # (``n_rows`` still parses the lines past it, and a trailer is no row); about one body's bytes at the peak.
        frame = super()._table(body[: cut + 1])
        if "count" in self._trailer.groupindex and int(match["count"]) != frame.height:
            msg = f"the trailer counts {match['count']} rows, and the body holds {frame.height}"
            raise ReadError(msg)
        if not self._stated:
            return frame
        stated = frame.with_columns(
            _stated(column, match[column.field], frame.height, f"{column.label!r} cell").alias(name)
            for name, column in self._stated.items()
        )
        return stated.select(name for name in self._names if name in stated.columns)


class SectionedCsv[RowT: msgspec.Struct](CsvFile[RowT]):
    """A file of several tables one after another, each opening with its own header line (EIA's ``table1.csv``).

    A format subclasses it and names :attr:`_section`, the regex a table's header line starts with (matched on lines);
    this reads the table *section* of *sections*, from 0: the file must open with a header and hold exactly that many,
    and the table is read as a file of its own, its row numbers its own.
    """

    #: The regex a header line starts with, matched where a line begins; each format's subclass sets it.
    _section: typing.ClassVar[re.Pattern[bytes]]

    def __init__(
        self,
        row: type[RowT],
        *,
        section: int = 0,
        sections: int = 1,
        null_values: Sequence[str] = (),
        final_line_ending: bool | None = None,
    ) -> None:
        """Lay out table *section* of the *sections* a file holds, as :class:`CsvFile` lays out *row*."""
        super().__init__(row, null_values=null_values, final_line_ending=final_line_ending)
        if not 0 <= section < sections:
            msg = f"the file's {sections} tables have no table {section}"
            raise LayoutError(msg)
        self._which = section
        self._sections = sections

    @typing.override
    def _table(self, body: bytes) -> pl.DataFrame:
        starts = [match.start() for match in self._section.finditer(body)]
        if starts[:1] != [0] or len(starts) != self._sections:
            msg = f"the body has {len(starts)} tables, at bytes {starts}, not {self._sections} from the first byte"
            raise ReadError(msg)
        # shortcut: the table is copied, as a trailer's is: polars has no option that ends a read at a line
        return super()._table(body[starts[self._which] : (*starts, len(body))[self._which + 1]])


class SpilledCsv[RowT: msgspec.Struct](CsvFile[RowT]):
    """A clean, unquoted file that may write its separator inside one text field (SEC's ``BAM| ENTMT INC``).

    A format names that field, :attr:`_spilled`: a row wider than the header holds its surplus cells in the field,
    where the cells from the header's place of its label on, up to the row's last, are the field's text, the separator
    kept. The place is the body's header's, so a header in any order is read right. A row as wide as the header, and
    any narrower, are read as any CSV's rows are. The format must quote nothing (``_quote_char = None``), name a text
    field with a ``Label`` the header writes as it is (not a field a trailer states), and have a header line. Raises
    ``ValueError`` at construction for a field that is not a text field, a quoted or headerless format, and when
    reading for a header without the field's label.
    """

    #: The name of the text field that may hold the separator; each format's subclass sets it.
    _spilled: typing.ClassVar[str]
    #: What may stand for the separator inside the field while polars reads: the first of these the body lacks.
    _CARRIERS: typing.ClassVar[Sequence[str]] = (chr(0x1F), chr(0x1E), chr(0x1D), chr(0x1C))

    def __init__(
        self,
        row: type[RowT],
        *,
        null_values: Sequence[str] = (),
        final_line_ending: bool | None = None,
        separator: str = ",",
    ) -> None:
        """Lay out a file of *row* (see :class:`CsvFile`), whose :attr:`_spilled` field is a text one."""
        super().__init__(row, null_values=null_values, final_line_ending=final_line_ending, separator=separator)
        if not self._header or self._quote_char is not None:
            msg = "a spilled field is read from a file with a header line that quotes nothing"
            raise LayoutError(msg)
        columns = [column for column in self._columns if column.field == self._spilled]
        if not columns or columns[0].scalar is not str:
            msg = f"the spilled field {self._spilled!r} is no text field of {row.__name__}"
            raise LayoutError(msg)
        self._spilled_label = columns[0].label
        (self._spilled_name,) = (name for name, field in self._names.items() if field == self._spilled)

    @typing.override
    def _table(self, body: bytes) -> pl.DataFrame:
        header = _header(body, self._separator)
        if self._spilled_label not in header:
            msg = f"the header {header!r} lacks {self._spilled_label!r}, the label of the field that holds it"
            raise ReadError(msg)
        carrier = next((text for text in self._CARRIERS if text.encode() not in body), None)
        if carrier is None:
            msg = f"the body holds every character this reader carries the separator in a cell by: {self._CARRIERS!r}"
            raise ReadError(msg)
        separator = self._separator.encode()
        at, width = header.index(self._spilled_label), len(header)
        lines = body.split(b"\n")
        for number, line in enumerate(lines):
            ending = b"\r" if line.endswith(b"\r") else b""
            cells = line.removesuffix(ending).split(separator)
            if (extra := len(cells) - width) > 0:
                joined = carrier.encode().join(cells[at : at + extra + 1])
                lines[number] = separator.join([*cells[:at], joined, *cells[at + extra + 1 :]]) + ending
        frame = super()._table(b"\n".join(lines))
        spilled = pl.col(self._spilled_name).str.replace_all(carrier, self._separator, literal=True)
        return frame.with_columns(spilled)


class TitledCsv[RowT: msgspec.Struct](CsvFile[RowT]):
    """A clean file whose header sits below title lines (a title, an as-of date, a note, a blank line).

    A format subclasses it and names its titles: :attr:`_titles`, a regex per line above the header, in order, each
    matching its whole line (a blank line is ``re.compile("")``). A group named ``count`` is the file's number of rows,
    checked; a group named for a field is that field's value on every row (the week a title is as of), typed as the
    field (:func:`_stated`), a field no column holds, which takes no ``Label``.
    """

    #: The regexes the lines above the header match, one a line; each format's subclass (or its constructor) sets them.
    _titles: Sequence[re.Pattern[str]]

    def __init__(
        self,
        row: type[RowT],
        *,
        null_values: Sequence[str] = (),
        final_line_ending: bool | None = None,
        separator: str = ",",
    ) -> None:
        """Lay out a file of *row* (see :class:`CsvFile`), each field a title's group names stated, not a column."""
        super().__init__(row, null_values=null_values, final_line_ending=final_line_ending, separator=separator)
        self._titled = self._stated_columns(self._title_groups())

    def _title_groups(self) -> set[str]:
        return {group for title in self._titles for group in title.groupindex if group != "count"}

    @typing.override
    def _label(self, name: str) -> str | None:
        return name if name in self._title_groups() else super()._label(name)

    @typing.override
    def _table(self, body: bytes) -> pl.DataFrame:
        at, count, stated = len(_BOM) if body.startswith(_BOM) else 0, None, dict[str, str]()
        for number, title in enumerate(self._titles, 1):
            if (end := body.find(b"\n", at)) < 0:
                msg = f"the body ends before line {number}, the file's title {title.pattern!r}"
                raise ReadError(msg)
            line = body[at:end].removesuffix(b"\r").decode("utf-8", errors="replace")
            if (match := title.fullmatch(line)) is None:
                msg = f"line {number} {line[:200]!r} is not the file's title {title.pattern!r}"
                raise ReadError(msg)
            count = match["count"] if "count" in title.groupindex else count
            stated |= {group: text for group, text in match.groupdict().items() if text is not None}
            at = end + 1
        # shortcut: the table is copied without its titles; polars' skip_lines would skip them in place, but every
        # format's _table would then take the offset, to save one copy of a body of at most 10 MB recorded (IBKR's).
        frame = super()._table(body[at:])
        if count is not None and int(count) != frame.height:
            msg = f"the title counts {count} rows, and the body holds {frame.height}"
            raise ReadError(msg)
        if not self._titled:
            return frame
        frame = frame.with_columns(
            _stated(column, stated.get(column.field), frame.height, f"{column.label!r} cell").alias(name)
            for name, column in self._titled.items()
        )
        return frame.select(name for name in self._names if name in frame.columns)


class PreambleCsv[RowT: msgspec.Struct](CsvFile[RowT]):
    """A clean file whose header sits below a preamble of any length (a title, a source, notes, blank lines).

    The header is the first line whose first cell is the struct's first label, quoted or not. The lines above it are
    dropped unread; a body with no such line raises.
    """

    @typing.override
    def _table(self, body: bytes) -> pl.DataFrame:
        first = self._columns[0].label
        quoted = f'"{first}"'
        at = len(_BOM) if body.startswith(_BOM) else 0
        while at < len(body):
            end = body.find(b"\n", at)
            end = len(body) if end < 0 else end
            cell = body[at:end].removesuffix(b"\r").decode("utf-8", errors="replace").split(self._separator, 1)[0]
            if cell in {first, quoted}:
                return super()._table(body[at:])  # shortcut: copied without its preamble, as TitledCsv's titles
            at = end + 1
        msg = f"no line opens with the header's first label {first!r}"
        raise ReadError(msg)


class _TextCsv[RowT: msgspec.Struct](CsvFile[RowT]):
    """A file whose cells are read as text, each cleaned by the format's pieces (:meth:`_clean`), then typed.

    A cell is typed as a :class:`TextFile`'s is, strictly; a cell cleaned to nothing is no value.
    """

    @typing.override
    def _dtype(self, column: _Column) -> type[pl.DataType]:
        return pl.String

    @typing.override
    def _table(self, body: bytes) -> pl.DataFrame:
        frame = super()._table(body)
        # A long table's columns are named by its headings, each repeat a NUL and its number after it (see _polars).
        laid = self._layout([name.partition(_NUL)[0] for name in frame.columns])
        # Keyed as polars names the columns (a repeated label's second is ``Filler_duplicated_0``); a field a format
        # states that is not yet a column (a trailer's, added after) is left to it.
        named = (
            zip(frame.columns, laid, strict=True)
            if laid is not None
            else zip(_polars_names([column.label for column in self._columns]), self._columns, strict=True)
        )
        cleaned = {
            name: (column, self._clean(frame[name], column).replace("", None))
            for name, column in named
            if name in frame.columns
        }
        # A flag, a date or a time stays text: CsvFile parses it next.
        return frame.with_columns(
            (cells if _csv_dtype(column) is pl.String else _typed(column, cells, f"{column.label!r} cell")).alias(name)
            for name, (column, cells) in cleaned.items()
        )


class PaddedCsv[RowT: msgspec.Struct](_TextCsv[RowT]):
    """A file whose cells are padded with spaces (CFTC's ``"ABC  "``, Cboe's ``10/02/2019, 794943``), stripped."""

    @typing.override
    def _clean(self, cells: pl.Series, column: _Column) -> pl.Series:
        return super()._clean(cells, column).str.strip_chars(" ")


class ThousandsCsv[RowT: msgspec.Struct](Thousands[RowT], _TextCsv[RowT]):
    """A file whose numbers are written with thousands separators (``1,782,319``), each read as its number."""


class CsvText[RowT: msgspec.Struct](GridFile[RowT]):
    """A delimited file too irregular for :class:`CsvFile` (a header over several lines, blocks, ragged lines): cut.

    Its grid is every row's cells as the ``csv`` module reads them (a quoted cell may hold the separator and line
    breaks), a grid row per row of the file from its first, a blank line a row of no values, as wide as its widest
    row: a shorter row's missing cells are no value. The body is UTF-8, a byte-order mark dropped (:class:`Transcoded`
    reads another encoding first); an empty body, one not UTF-8 or one cut inside a quoted cell raises.

    A format whose notes misquote (treasury's SLT history: ``"ts." Holdings o"``) sets :attr:`_misquoted`: a quote
    that does not close its cell is read as text, and a cell left open at the end runs to it (``csv``'s ``strict``
    off); the data rows, quoted cleanly, read as before. Example: ``docs/examples/csv_text-misquoted.csv``.
    Unlike :attr:`CsvFile._quote_char` (``None``), which reads every ``"`` as text, a well-formed quoted cell still
    holds its separator.
    """

    #: Whether a stray quote is text rather than an error: for a file whose prose notes misquote.
    _misquoted: typing.ClassVar[bool] = False

    def __init__(
        self, row: type[RowT], *, header_row: int = 1, separator: str = ",", null_values: Sequence[str] = ()
    ) -> None:
        """Lay out a table of *row* under its header in the file's row *header_row*, *separator* between cells."""
        super().__init__(row, header_row=header_row, null_values=null_values)
        self._separator = separator

    @typing.override
    def _body_grid(self, body: bytes) -> pl.DataFrame:
        try:
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError as e:
            msg = f"the body is not UTF-8: {e}"
            raise ReadError(msg) from e
        if not text or text.isspace():
            msg = "the body is empty"
            raise EmptyBodyError(msg)
        try:
            rows = list(
                csv.reader(io.StringIO(text, newline=""), delimiter=self._separator, strict=not self._misquoted)
            )
        except csv.Error as e:
            msg = f"the body is no delimited file: {e}"
            raise ReadError(msg) from e
        width = max(map(len, rows))
        names = [str(at) for at in range(width)]
        grid = pl.DataFrame(
            [[*row, *[None] * (width - len(row))] for row in rows], schema=dict.fromkeys(names, pl.String), orient="row"
        )
        cell = pl.col(pl.String)
        return grid.with_columns(pl.when((cell.str.strip_chars() != "") & ~cell.is_in(self._null_values)).then(cell))


def _mistyped(text: pl.DataFrame, schema: Mapping[str, type[pl.DataType]]) -> str | None:
    """Return what is wrong with the first cell of *text*, a table read as text, that is not its type in *schema*.

    What polars' typed read refused, said as every reader says it: the cell, its column's label and its row.
    """
    for name, dtype in schema.items():
        cells = text[name]
        unread = cells.is_not_null() & cells.cast(dtype, strict=False).is_null()
        if dtype is not pl.String and (at := _first(unread)) is not None:
            what = "whole number" if dtype is pl.Int64 else "number"
            return f"the {name!r} cell {cells[at - 1]!r} is not a {what}: row {at}"
    return None  # shortcut: polars refused what every cast reads; its own message says what


def _csv_dtype(column: _Column) -> type[pl.DataType]:
    """Return the polars type a CSV reads *column* as: a boolean, a date or a time as text, which is then parsed."""
    text = column.flag is not None or column.written is not None or column.scalar is bool
    return pl.String if text else _DTYPES[column.scalar]


def _polars_names(labels: Sequence[str]) -> list[str]:
    """Return the names polars' ``read_csv`` gives a header of *labels*: ``Filler``, then ``Filler_duplicated_0``."""
    return _repeated(labels, "{label}_duplicated_{n}", 0, empties=True)


def _header(body: bytes, separator: str, quote: str | None = '"') -> list[str]:
    """Return the labels of the first line of a delimited *body*, its byte order mark and line ending off.

    A format that quotes (*quote*) may hold a line ending inside a quoted label: the line runs on while its quotes are
    odd, each line counted once.
    """
    end = body.find(b"\n")
    quotes = body.count(b'"', 0, end if end >= 0 else len(body)) if quote is not None else 0
    while end >= 0 and quotes % 2:
        start = end + 1
        end = body.find(b"\n", start)
        quotes += body.count(b'"', start, end if end >= 0 else len(body))
    line = body[: end if end >= 0 else len(body)].removeprefix(_BOM).removesuffix(b"\r")
    try:
        (header,) = csv.reader([line.decode("utf-8", errors="replace")], delimiter=separator)
    except csv.Error as e:  # a line ending inside the first line: lines ended by CR alone
        msg = f"the header line is not one row of cells: {e}"
        raise ReadError(msg) from e
    return header


def _full(body: bytes, frame: pl.DataFrame, separator: str, *, header: bool = True) -> bool:
    """Return whether every row of *body* is as wide as its header, *frame* being polars' read of it.

    Full rows hold ``(rows + 1) * (columns - 1)`` separators (``rows * (columns - 1)`` with no *header*), plus the ones
    inside a label or a text cell (quoted): a short row, which polars pads with nulls, holds fewer. Counted in the bytes
    (C), with no line split: on a 79 MB file of 1.7 million rows, about 25 ms beside polars' 50 ms read.
    """
    texts = [name for name, dtype in frame.schema.items() if dtype == pl.String]
    inside = sum(label.count(separator) for label in frame.columns) if header else 0
    if texts:
        counted = frame.select(pl.sum_horizontal(pl.col(texts).str.count_matches(separator, literal=True).fill_null(0)))
        inside += int(counted.to_series().sum())
    return body.count(separator.encode()) == (frame.height + header) * (frame.width - 1) + inside


def _check_widths(body: bytes, separator: str, *, quoting: bool = True, header: bool = True) -> None:
    """Refuse a row that is not as wide as the header, naming it: :func:`_full`'s count finds one, this names it.

    polars pads a short row with nulls, and a long one may fill an inserted column. A row's fields are its separators
    outside quoted cells (any, where the format quotes nothing), plus one, counted a line at a time in polars; a body
    with any other quote (a quoted cell spanning lines, a quote inside a cell), which a line cannot count, or a NUL
    byte, on which polars splits the lines, is counted by the ``csv`` module instead. A blank line is a row of one
    empty field. With no *header*, a row is refused that is not as wide as the first.
    """
    lines = None if _NUL.encode() in body else _lines(body)
    if lines is not None and quoting:
        # A quoted cell, its doubled quotes included, and the separator before it: what a row's field count skips.
        lines = lines.str.replace_all(r"(^|" + re.escape(separator) + r')"(?:[^"]|"")*"', "${1}")
    if lines is None or (quoting and lines.str.contains('"', literal=True).any()):
        try:
            text = body.removeprefix(_BOM).decode("utf-8")
        except UnicodeDecodeError as e:
            msg = f"the body is not UTF-8: {e}"
            raise ReadError(msg) from e
        try:
            quote = csv.QUOTE_MINIMAL if quoting else csv.QUOTE_NONE
            reader = csv.reader(io.StringIO(text, newline=""), delimiter=separator, quoting=quote, strict=True)
            rows = [len(row) or 1 for row in reader]
        except csv.Error as e:
            msg = f"a quoted cell does not close: {e}"
            raise ReadError(msg) from e
        fields = pl.Series(rows, dtype=pl.Int64)
    else:
        fields = lines.str.count_matches(separator, literal=True) + 1
    if (at := _first(fields.slice(1) != fields[0])) is not None:
        first = "the header's" if header else "row 1's"
        msg = f"row {at + (not header)} has {fields[at]} fields, not {first} {fields[0]}"
        raise RaggedRowError(msg)
