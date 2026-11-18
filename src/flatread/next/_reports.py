"""Reports: a file of text lines laid out for a page (ReportFile's), its rows among titles, headers, notes and rules.

:class:`Text` reads the file as its lines, each line one cell. :class:`Report` reads the table among them, each line
of one kind: a row, the column header's, or framing around and between the rows, which may state a field for the rows
below. :class:`Keyed` reads a report's rows long, a row per key (KeyedReport's): a run of value cells for each key its
header line names. A format's own report subclasses :class:`Report` and checks more in :meth:`Report.check` (a total
line against the rows above it).
"""

import dataclasses
import re
import typing
from collections.abc import Sequence

import polars as pl

from flatread.core import Column, _check_ending, _decoded, _lines
from flatread.errors import (
    EmptyBodyError,
    LayoutError,
    RaggedRowError,
    ReadError,
    TruncatedFileError,
    UnknownLayoutError,
)
from flatread.next._model import Grid, Lines, ReadContext, Stream, Table
from flatread.reports import _engine

__all__ = ["Keyed", "Report", "Text"]


@dataclasses.dataclass(frozen=True, kw_only=True)
class Text:
    r"""A file of text lines, each line one cell (its raw text too), numbered from 1.

    Its lines are polars' (``\n`` and ``\r\n``, UTF-8 only, a byte-order mark off), or, with *splitlines*, those
    ``str.splitlines`` breaks the text at (a form feed, ``\x0b`` and ``\r\r\n`` end a line too), UTF-8 or else
    latin-1. *final_line_ending* is :class:`Csv`'s. An empty body, or one of whitespace alone, raises.
    """

    splitlines: bool = False
    final_line_ending: bool | None = None

    def grid(self, stream: Stream, read: ReadContext) -> Lines:
        """Return the file's lines, each one cell."""
        del read
        body = stream.read()
        if not body.strip():
            msg = "the body is empty"
            raise EmptyBodyError(msg)
        if self.final_line_ending is not None:
            _check_ending(body, final_line_ending=self.final_line_ending)
        lines = (
            pl.Series("raw", _decoded(body, ("utf-8", "latin-1")).splitlines(), pl.String)
            if self.splitlines
            else _lines(body).alias("raw")
        )
        return Lines(
            cells=pl.DataFrame({"0": lines}),
            widths=pl.repeat(1, lines.len(), dtype=pl.Int64, eager=True).alias("width"),
            raw=lines,
            numbers=pl.int_range(1, lines.len() + 1, dtype=pl.Int64, eager=True).alias("number"),
        )


@dataclasses.dataclass(frozen=True, kw_only=True)
class Report:
    """A table laid out for a page, not a parser: its rows among titles, headers, page headings, notes and rules.

    Each line is of one kind, a regex its whole raw text matches: *line*, a row's, whose named groups are its fields'
    cells (a group named for each field); *header_lines*, the column header's lines, which must stand in order above
    the first row (a header split over lines); and *framing*, one per kind of line around and between the rows. Header
    and framing lines are dropped, at any place (a page writes its header again). A framing group named for a field
    states it for every row below, until a line states it again: a title's date, written once; a section's name. A
    blank line is skipped (*blank_lines* ``"drop"``), or read as any other line (``"read"``), and any other line
    raises, naming it, as does a report whose last line that is not blank is not *last_line* (one cut short), when
    the format names one. *preamble* drops every line above the first run of the header's lines, whatever it holds.
    A report of no rows is refused unless the reader takes a table of none (one that prints ``NO DATA``).

    A format checks more by subclassing it: :meth:`check` is handed every line read that is not blank, its number and
    its kind's match (a total line against the rows above it).
    """

    line: re.Pattern[str]
    header_lines: Sequence[re.Pattern[str]] = ()
    framing: Sequence[re.Pattern[str]] = ()
    last_line: re.Pattern[str] | None = None
    blank_lines: typing.Literal["drop", "read"] = "drop"
    preamble: bool = False
    stated: frozenset[str] = frozenset()
    vouches: bool = False

    def __post_init__(self) -> None:
        patterns = [self.line, *self.header_lines, *self.framing, *([] if self.last_line is None else [self.last_line])]
        if not all(isinstance(pattern, re.Pattern) and isinstance(pattern.pattern, str) for pattern in patterns):
            msg = "a Report's lines are regexes compiled from str: its line, header_lines, framing and last_line"
            raise LayoutError(msg)
        if self.blank_lines not in ("drop", "read"):
            msg = f"a Report drops or reads its blank lines, not {self.blank_lines!r}"
            raise LayoutError(msg)
        if self.preamble and not self.header_lines:
            msg = "a Report's preamble ends at its header's lines, and it names none"
            raise LayoutError(msg)

    @property
    def kinds(self) -> list[re.Pattern[str]]:
        """The kinds of line that are not a row's: the header's, the framing's and the last line."""
        return [*self.header_lines, *self.framing, *(() if self.last_line is None else (self.last_line,))]

    def find(self, grid: Grid, read: ReadContext) -> Table:
        """Return the report's rows, each its cells and the fields stated above it, under the struct's labels."""
        columns = self._written(read)
        lines = grid.raw.to_list()
        numbers = grid.numbers.to_list()
        if self.last_line is not None:
            last = next((line for line in reversed(lines) if line.strip()), "")
            if self.last_line.fullmatch(last) is None:
                msg = f"the report ends with {last[:200]!r}, not {self.last_line.pattern!r}: it was cut"
                raise TruncatedFileError(msg)
        first = self._first(lines)
        records, matched = self.rows(lines[first:], numbers[first:])
        if not records and not read.empty:
            msg = "the report holds no rows"
            raise EmptyBodyError(msg)
        self.check(matched)
        cells = pl.DataFrame(
            [[record.get(column.field) for column in columns] for record, _ in records],
            schema={column.label: pl.String for column in columns},
            orient="row",
        )
        return Table(cells, pl.Series("number", [number for _, number in records], pl.Int64))

    def rows(
        self, lines: Sequence[str], numbers: Sequence[int]
    ) -> tuple[list[tuple[dict[str, str | None], int]], list[tuple[int, re.Match[str]]]]:
        """Return each row of *lines*, its cells and the fields stated above it, with its line's number.

        And every line read that is not blank, its number and its kind's match. Raises ``ReadError`` for a line of no
        kind. A format overrides it to read a line of its own (a row split over two lines).
        """
        stated: dict[str, str | None] = {}
        records: list[tuple[dict[str, str | None], int]] = []
        read: list[tuple[int, re.Match[str]]] = []
        kinds = self.kinds
        for number, line in zip(numbers, lines, strict=True):
            if (row := self.line.fullmatch(line)) is not None:
                records.append((row.groupdict() | stated, number))
                read.append((number, row))
            elif line.strip() or self.blank_lines == "read":
                framed = next((match for kind in kinds if (match := kind.fullmatch(line))), None)
                if framed is None:
                    msg = f"line {number} {line[:200]!r} is neither a row nor a line framing the table"
                    raise ReadError(msg, row=number)
                stated |= {name: text for name, text in framed.groupdict().items() if text is not None}
                read.append((number, framed))
        return records, read

    def check(self, read: Sequence[tuple[int, re.Match[str]]]) -> None:
        """Refuse a report whose lines are not its own: *read*, each line that is not blank, its number and match.

        The lines above the first row must hold a run of the header's lines, in order. A format checks more by
        overriding it (and calling it): a total line against the rows above it, each line's kind its match's ``re``.
        """
        first = next((at for at, (_, match) in enumerate(read) if match.re is self.line), len(read))
        above = [match.string for _, match in read[:first]]
        if self.header_lines and _run_at(self.header_lines, above) is None:
            msg = f"the lines above the first row hold no column header {[kind.pattern for kind in self.header_lines]}"
            raise UnknownLayoutError(msg)

    def _first(self, lines: Sequence[str]) -> int:
        """Return the first line the table reads: past the preamble, if the report has one."""
        if not self.preamble:
            return 0
        if (first := _run_at(self.header_lines, lines)) is None:
            msg = f"the body holds no run of the header's lines {[kind.pattern for kind in self.header_lines]}"
            raise UnknownLayoutError(msg)
        return first

    def _written(self, read: ReadContext) -> list[Column]:
        """Return the columns the report writes, in the struct's order; raise for a group no field is or both kinds."""
        cells = set(self.line.groupindex)
        stated = {name for kind in self.kinds for name in kind.groupindex}
        if stray := (cells | stated) - set(read.columns):
            msg = f"the report's groups {sorted(stray)} name no field of the row"
            raise LayoutError(msg)
        if both := cells & stated:
            msg = f"the fields {sorted(both)} are both a row's cells and stated by its framing"
            raise LayoutError(msg)
        return [column for field, column in read.columns.items() if field in cells | stated]


def _run_at(kinds: Sequence[re.Pattern[str]], lines: Sequence[str]) -> int | None:
    """Return where *lines* first hold a run of lines matching *kinds*, in order, or ``None``."""
    size = len(kinds)
    return next(
        (
            at
            for at in range(len(lines) - size + 1)
            if all(kind.fullmatch(line) for kind, line in zip(kinds, lines[at : at + size], strict=True))
        ),
        None,
    )


@dataclasses.dataclass(frozen=True, kw_only=True)
class Keyed:
    """A report read long, a row per key: a row's last cells, a run per key, the keys its header line names.

    The struct's leading fields are the record's, then the key (*key_field*, by default the second to last field),
    then the values. A framing line states the keys in a group named for the key field, for the rows below; a row's
    group named for the first value field holds every value, a run of one cell per value field for each key. *cell*
    finds each value cell (a run of characters other than spaces, by default) and each key, unless *key_cell* finds
    those; a row holds one run per key, or raises, naming it. Rows come record by record, a record's keys in its
    header's order. polars' regex engine finds the cells, so each pattern is one it reads (no look-around), its flags
    among ``IGNORECASE``, ``MULTILINE``, ``DOTALL`` and ``VERBOSE``; another raises ``LayoutError`` when built.
    """

    cell: re.Pattern[str] = re.compile(r"\S+")
    key_cell: re.Pattern[str] | None = None
    key_field: str | None = None

    def __post_init__(self) -> None:
        _engine(self.cell, "cell")
        if self.key_cell is not None:
            _engine(self.key_cell, "key_cell")

    def shape(self, table: Table, columns: Sequence[Column], read: ReadContext) -> Table:
        """Return *table* read long, a row per key, under the struct's labels."""
        del read
        fields = [column.field for column in columns]
        if self.key_field is not None and self.key_field not in fields:
            msg = f"the key field {self.key_field!r} is no field of the row"
            raise LayoutError(msg)
        at = fields.index(self.key_field) if self.key_field is not None else len(fields) - 2
        if at < 0 or at > len(fields) - 2:
            msg = "a keyed report's row has a key field and a value field after it"
            raise LayoutError(msg)
        key, first, *rest = columns[at:]
        if table.kinds is not None:
            msg = "a keyed report reads text lines, not a sheet's cells"
            raise LayoutError(msg)
        cells_found = _engine(self.cell, "cell")
        keys_found = cells_found if self.key_cell is None else _engine(self.key_cell, "key_cell")
        found = table.cells.with_columns(
            pl.col(key.label).str.extract_all(keys_found), pl.col(first.label).str.extract_all(cells_found)
        )
        width = 1 + len(rest)
        wanted = found.select(pl.col(key.label).list.len().fill_null(0) * width).to_series()
        held = found.select(pl.col(first.label).list.len().fill_null(0)).to_series()
        if (wrong := (wanted != held).arg_true()).len():
            place = int(wrong[0])
            row = int(table.numbers[place])
            msg = f"row {row} holds {held[place]} values, not {wanted[place]}: {width} for each key above it"
            raise RaggedRowError(msg, row=row)
        values = pl.col(first.label)
        found = found.with_columns(
            *(values.list.gather_every(width, offset).alias(column.label) for offset, column in enumerate(rest, 1)),
            values.list.gather_every(width).alias(first.label),
        )
        split = [key.label, first.label, *(column.label for column in rest)]
        # Each row's number goes with it, the explode repeating it once per key (a row of no keys keeps one, null).
        long = found.with_columns(pl.Series("_number", table.numbers)).explode(*split)
        return Table(long.drop("_number"), long["_number"].alias("number"))
