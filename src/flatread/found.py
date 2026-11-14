"""An export whose header is found below any title lines by its labels, each column read by its label.

:class:`FoundTable` finds the header by its key field's words and reads each column under the field its cell names;
:class:`FoundCsv` reads the export's lines from a CSV, :class:`FoundSheet` from a workbook's first sheet.
"""

import abc
import csv
import io
import typing
from collections.abc import Collection, Sequence

import fastexcel
import msgspec
import polars as pl

from flatread.core import TextFile, _Column, _decoded
from flatread.sheets import text_sheet

__all__ = ["FoundCsv", "FoundSheet", "FoundTable"]


class FoundTable[RowT: msgspec.Struct](TextFile[RowT]):
    """An export of text lines whose header is found below any title lines, each column read by its label.

    The header is the first line with a cell that is a word of the *key* field's label (its ``Label`` and
    alternatives, each cell as :meth:`_heading` folds it). Each cell of it names a column: one word of a field's
    label, which is that field's column, a field it lacks reading as null. A cell no field's words name, two cells
    naming one field, a line wider than the header, or a value under a blank header cell (unless :attr:`_open`) raises,
    naming the line. A line of nothing but spaces and ``-`` rules, whether a blank one or a rule, is skipped; a cell
    the format lists in :attr:`_nulls` is no value, an empty one too; and a file with no rows is empty. A first column
    with no header cell whose cells are whole numbers is the export's row count when the format says so
    (:attr:`_row_count`).
    A line that opens a section (:attr:`_section`, then its text) states the :attr:`_stated` field for the rows below.
    A format subclasses it with :meth:`_lines`, the file's lines of cells (:class:`FoundCsv`, :class:`FoundSheet`).
    """

    _by_name = True
    _rows_required = False
    #: Whether every header cell names a column of its own, as written (an adviser roster's, read long): a blank one
    #: names none, and what is under it is not the file's; a field's words are then only what finds the header.
    _open: typing.ClassVar[bool] = False
    #: A line that opens a section (``Request Type: FOIA``), its text after this stating :attr:`_stated` below it.
    _section: typing.ClassVar[str] = ""
    _stated: typing.ClassVar[str] = ""
    #: The cells' words for no value, besides an empty one; a field kept as written has none.
    _nulls: typing.ClassVar[Collection[str]] = ()
    #: Whether a first column with no header cell, its cells whole numbers, is the export's row count, and no data.
    _row_count: typing.ClassVar[bool] = False

    def __init__(self, row: type[RowT], *, key: str) -> None:
        """Lay out an export of *row*, whose header is the line naming its field *key* (refused if no field is)."""
        super().__init__(row)
        (key_column,) = (column for column in self._columns if column.field == key)
        self._key_field = key
        self._key_words = {key_column.label, *key_column.aliases}
        words = [
            (word, column.label)
            for column in self._columns
            if not (self._section and column.field == self._stated)
            for word in (column.label, *column.aliases)
        ]
        self._words = dict(words)
        if len(self._words) != len(words):
            msg = f"two fields of {row.__name__} share a label: {sorted(word for word, _ in words)}"
            raise ValueError(msg)

    @typing.override
    def _label(self, name: str) -> str | None:
        return name

    def _heading(self, cell: str) -> str:
        """Return the word a header *cell* writes: the cell, unless a format folds its headers' wording."""
        return cell

    @typing.override
    def _clean(self, cells: pl.Series, column: _Column) -> pl.Series:
        if column.field in self._as_written:
            return cells
        return pl.select(pl.when(~cells.str.strip_chars().is_in(list(self._nulls))).then(cells)).to_series()

    @abc.abstractmethod
    def _lines(self, body: bytes) -> list[list[str]]:
        """Return *body*'s lines, each its cells as text, or raise ValueError."""

    @staticmethod
    def _blank(line: Sequence[str]) -> bool:
        """Return whether *line* is blank: nothing but spaces and ``-`` rules, which no row holds."""
        return not "".join(line).strip(" -")

    @typing.override
    def _cells(self, body: bytes) -> pl.DataFrame:
        lines = self._lines(body)
        at = next((n for n, line in enumerate(lines) if self._key_words & {self._heading(c) for c in line}), None)
        if at is None:
            msg = f"the file has no header naming {self._key_field}: it is not a {self._row.__name__} file"
            raise ValueError(msg)
        header = lines[at]
        names = [
            (cell.strip() if self._open else self._words.get(self._heading(cell))) if cell.strip() else None
            for cell in header
        ]
        if unknown := [cell for cell, name in zip(header, names, strict=True) if cell.strip() and name is None]:
            msg = f"the header names columns {self._row.__name__} does not declare: {unknown}"
            raise ValueError(msg)
        if len(named := [name for name in names if name is not None]) != len(set(named)):
            msg = f"the header names one field twice: {header}"
            raise ValueError(msg)
        records: list[dict[str, str | None]] = []
        section: str | None = None
        for number, line in enumerate(lines[at + 1 :], start=at + 2):
            if self._blank(line):
                continue
            if self._section and line[0].startswith(self._section) and not "".join(line[1:]).strip():
                section = line[0].removeprefix(self._section).strip()
                continue
            # An unnamed first column of whole numbers is the export's row count (2013's BDC file), and not data.
            under = [cell for cell, name in zip(line, names, strict=False) if name is None]
            if self._row_count and names[0] is None and line[0].strip().isdecimal():
                under = under[1:]
            if any(cell.strip() for cell in [*line[len(header) :], *([] if self._open else under)]):
                msg = f"line {number} holds a value under no label: {line}"
                raise ValueError(msg)
            record: dict[str, str | None] = {
                name: cell for name, cell in zip(names, line, strict=False) if name is not None
            }
            records.append(record | {self._stated: section} if self._section else record)
        columns = named if self._open else self._labels
        return pl.DataFrame(
            [[record.get(label) for label in columns] for record in records],
            schema=dict.fromkeys(columns, pl.String),
            orient="row",
        )


class FoundCsv[RowT: msgspec.Struct](FoundTable[RowT]):
    """A :class:`FoundTable` that is a CSV: UTF-8, a byte-order mark dropped, or latin-1 where it is not UTF-8."""

    @typing.override
    def _lines(self, body: bytes) -> list[list[str]]:
        return list(csv.reader(io.StringIO(_decoded(body, ("utf-8-sig", "latin-1")), newline="")))


class FoundSheet[RowT: msgspec.Struct](FoundTable[RowT]):
    """A :class:`FoundTable` on the first sheet of a workbook (``.xlsx``, ``.xls``), its cells as text."""

    @typing.override
    def _lines(self, body: bytes) -> list[list[str]]:
        try:
            reader = fastexcel.read_excel(body)
            sheet = text_sheet(reader, reader.sheet_names[0])
        except fastexcel.FastExcelError as e:
            msg = f"the body is not a workbook: {e}"
            raise ValueError(msg) from e
        return [[cell or "" for cell in line] for line in sheet.iter_rows()]
