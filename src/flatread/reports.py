"""A table laid out for a page, not a parser, read into its row struct's fields.

:class:`ReportFile` is a table laid out for a page, not a parser (NOAA's degree-day summaries, the Fed's payment-system
tables, TIC's monthly text files): its rows among titles, a header split over lines, page headings, section names,
notes and rules. A format names each kind of line by a regex its whole line matches: a row's (:attr:`~ReportFile._line`,
a named group per field), the column header's lines (:attr:`~ReportFile._header_lines`, which must stand in order above
the first row) and the framing's (:attr:`~ReportFile._framing`), and optionally the report's last line
(:attr:`~ReportFile._last_line`, so a cut report is refused). Header and framing lines are dropped wherever they stand,
a framing group named for a field states it for every row below until a line states it again (a title's date, a
section's name carried down over its rows), and any line of no kind raises, naming it. Its cells are typed as a
:class:`~flatread.core.TextFile`'s. :class:`LongReport` reads a report long, a row per key its header line names.
"""

import re
import typing
from collections.abc import Collection, Sequence

import msgspec
import polars as pl

from flatread.core import TextFile, _check_ending, _decoded, _first, _lines
from flatread.errors import EmptyBodyError, LayoutError, RaggedRowError, ReadError

__all__ = ["LongReport", "ReportFile"]


class ReportFile[RowT: msgspec.Struct](TextFile[RowT]):
    """A table laid out for a page, not a parser: its rows among titles, headers, page headings, notes and rules.

    A format names its kinds of line, each a regex its whole line matches: :attr:`_line`, a row's, whose named groups
    are its fields' cells (a group named for each field); :attr:`_header_lines`, the column header's lines, which must
    stand in order above the first row (a header split over lines, one label's words over the next's); and
    :attr:`_framing`, one per kind of line around and between the rows. Header and framing lines are dropped, at any
    place (a page writes its header again). A framing group named for a field states it for every row below, until a
    line states it again: a title's date, written once; a section's name, carried down over its rows. A blank line is
    skipped, and any other line raises, naming it, as does a report whose last line is not :attr:`_last_line` (one cut
    short), when the format names one; a report of no rows raises, unless :attr:`_rows_required` is false (a report that
    prints ``NO DATA``). *null_values* are the report's words for no value (``n.a.``), and *final_line_ending* is
    :class:`CsvFile`'s. A field neither a row's group nor stated is the format's to add, in :meth:`_cells`.
    """

    #: A row's line; its named groups are the fields' cells.
    _line: re.Pattern[str]
    #: The column header's lines, in order, above the first row; blank lines may stand between them.
    _header_lines: Sequence[re.Pattern[str]] = ()
    #: The other lines framing the table, a regex for each kind; a line is of the first kind it matches.
    _framing: Sequence[re.Pattern[str]] = ()
    #: The report's last line that is not blank, when it writes one (``END OF REPORT``); it frames too.
    _last_line: typing.ClassVar[re.Pattern[str] | None] = None
    #: Whether a blank line is skipped: a format whose blank line is no line of its table's sets it false, and it is
    #: then a line like any other (a row, framing, or refused).
    _skip_blank: typing.ClassVar[bool] = True
    #: Whether the lines are ``str.splitlines``' of the text (a form feed, ``\x0b`` and ``\r\r\n`` end a line), UTF-8 or
    #: else latin-1, rather than polars' lines (``\n`` and ``\r\n``, UTF-8 only).
    _text_lines: typing.ClassVar[bool] = False
    #: Whether any lines above the first run of the header's lines are a preamble, no row whatever they hold.
    _preamble: typing.ClassVar[bool] = False

    def __init__(
        self, row: type[RowT], *, null_values: Collection[str] = (), final_line_ending: bool | None = None
    ) -> None:
        """Lay out a report of *row*: each field a group of the format's row, or one its framing states.

        Raises ``LayoutError`` for a group named for no field of *row*, a field both a row's cell and stated, or what
        :class:`TextFile` refuses.
        """
        super().__init__(row)
        self._kinds = [*self._header_lines, *self._framing, *(() if self._last_line is None else (self._last_line,))]
        cells, stated = set(self._line.groupindex), {name for kind in self._kinds for name in kind.groupindex}
        if stray := (cells | stated) - set(self._fields):
            msg = f"the format's groups {sorted(stray)} name no field of {row.__name__}"
            raise LayoutError(msg)
        if both := cells & stated:
            msg = f"the fields {sorted(both)} are both a row's cells and stated by its framing"
            raise LayoutError(msg)
        # The fields the report writes, in order: a format adds any other in its _cells.
        self._written = [name for name in self._fields if name in cells | stated]
        self._null_values = list(null_values)
        self._final_line_ending = final_line_ending

    @typing.override
    def _label(self, name: str) -> str | None:
        return name

    @typing.override
    def _cells(self, body: bytes) -> pl.DataFrame:
        if not body.strip():
            msg = "the body is empty"
            raise EmptyBodyError(msg)
        if self._final_line_ending is not None:
            _check_ending(body, final_line_ending=self._final_line_ending)
        lines, first = self._split(body)
        if self._last_line is not None:
            last = next((line for line in reversed(lines) if line.strip()), "")
            if self._last_line.fullmatch(last) is None:
                msg = f"the report ends with {last[:200]!r}, not {self._last_line.pattern!r}: it was cut"
                raise ReadError(msg)
        stated: dict[str, str | None] = {}
        records: list[dict[str, str | None]] = []
        read: list[tuple[int, re.Match[str]]] = []  # every line that is not blank, its number and its kind's match
        for number, line in enumerate(lines[first:], first + 1):
            if (row := self._line.fullmatch(line)) is not None:
                records.append(row.groupdict() | stated)
                read.append((number, row))
            elif line.strip() or not self._skip_blank:
                framed = next((match for kind in self._kinds if (match := kind.fullmatch(line))), None)
                if framed is None:
                    msg = f"line {number} {line[:200]!r} is neither a row nor a line framing the table"
                    raise ReadError(msg)
                stated |= {name: text for name, text in framed.groupdict().items() if text is not None}
                read.append((number, framed))
        if not records and self._rows_required:
            msg = "the report holds no rows"
            raise EmptyBodyError(msg)
        self._check_lines(read)
        named = {column.field: column.label for column in self._columns}
        cells = pl.DataFrame(
            [[record.get(name) for name in self._written] for record in records],
            schema={named[name]: pl.String for name in self._written},
            orient="row",
        )
        return cells.with_columns(pl.col(pl.String).replace(self._null_values, None)) if self._null_values else cells

    def _split(self, body: bytes) -> tuple[list[str], int]:
        """Return *body*'s lines, and the first one the table reads: past the preamble, if the format has one."""
        lines: list[str]
        if self._text_lines:
            lines = _decoded(body, ("utf-8", "latin-1")).splitlines()
        else:
            lines = [str(line) for line in _lines(body)]
        if not self._preamble:
            return lines, 0
        size = len(self._header_lines)
        found = (
            at
            for at in range(len(lines) - size + 1)
            if all(kind.fullmatch(line) for kind, line in zip(self._header_lines, lines[at : at + size], strict=True))
        )
        if (first := next(found, None)) is None:
            msg = f"the body holds no run of the header's lines {[kind.pattern for kind in self._header_lines]}"
            raise ReadError(msg)
        return lines, first

    def _check_lines(self, read: Sequence[tuple[int, re.Match[str]]]) -> None:
        """Refuse a report whose lines are not its own: *read*, each line that is not blank, its number and match.

        The lines above the first row must hold a run of the header's lines, in order; a format checks more (a total
        line against the rows above it, a count against the rows), each line's kind its match's ``re``.
        """
        first = next((at for at, (_, match) in enumerate(read) if match.re is self._line), len(read))
        above = [match.string for _, match in read[:first]]
        size = len(self._header_lines)
        if size and not any(
            all(kind.fullmatch(line) for kind, line in zip(self._header_lines, above[at : at + size], strict=True))
            for at in range(len(above) - size + 1)
        ):
            msg = f"the lines above the first row hold no column header {[kind.pattern for kind in self._header_lines]}"
            raise ReadError(msg)


class LongReport[RowT: msgspec.Struct](ReportFile[RowT]):
    """A report read long, a row per key: a line's last cells, a run per key, the keys its header line names.

    The row struct is the long row: its leading fields are the record's (a row's groups, or stated), then the key, then
    the values (:attr:`_key_field` names the key, the second to last field by default). A header line (a framing kind)
    names the keys in a group named for the key field, stated for the rows below until a header names them again (a
    block a year); a row's group named for the first value field holds every value, a run of one cell per value field
    for each key. :attr:`_cell` finds each value cell (a run of characters other than spaces, by default) and each
    key, unless :attr:`_key_cell` finds those; a row holds one run per key, or raises, naming it. Rows come record by
    record, a record's keys in its header's order, and each cell is typed as its field's.
    """

    #: A value cell's text, found in a row's values; and a key's in a header's keys, when other (keys of two words).
    _cell: typing.ClassVar[re.Pattern[str]] = re.compile(r"\S+")
    _key_cell: typing.ClassVar[re.Pattern[str] | None] = None
    #: The struct's key field: the one after the record's, by default the second to last.
    _key_field: typing.ClassVar[str | None] = None

    @typing.override
    def _cells(self, body: bytes) -> pl.DataFrame:
        cells = super()._cells(body)
        fields = [column.field for column in self._columns]  # a given field has no column
        at = fields.index(self._key_field) if self._key_field is not None else len(fields) - 2
        key, first, *rest = self._columns[at:]
        keys = self._cell if self._key_cell is None else self._key_cell
        found = cells.with_columns(
            pl.col(key.label).str.extract_all(keys.pattern), pl.col(first.label).str.extract_all(self._cell.pattern)
        )
        width = 1 + len(rest)
        counted = found.select(pl.col(key.label).list.len().fill_null(0) * width, pl.col(first.label).list.len())
        if (row := _first(counted[key.label] != counted[first.label])) is not None:
            wanted = counted[key.label][row - 1]
            msg = f"row {row} holds {counted[first.label][row - 1]} values, not {wanted}: {width} for each key above it"
            raise RaggedRowError(msg)
        values = pl.col(first.label)
        found = found.with_columns(
            *(values.list.gather_every(width, offset).alias(column.label) for offset, column in enumerate(rest, 1)),
            values.list.gather_every(width).alias(first.label),
        )
        split = [key.label, first.label, *(column.label for column in rest)]
        found = found.explode(*split).select(self._labels)
        # The report's words for no value are cells of the run, not the run.
        return found.with_columns(pl.col(split).replace(self._null_values, None)) if self._null_values else found
