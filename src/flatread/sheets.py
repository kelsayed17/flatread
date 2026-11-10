"""One sheet of an xls or xlsx workbook, read by fastexcel (calamine) in one load of the table's rows.

The fast path lets fastexcel type each column from every one of its cells with ``dtype_coercion="strict"``: a column
of numbers reads as floats at full precision, of dates as datetimes, of text as text, and a column that mixes kinds
(a ``.`` among numbers, a footnote under dates) makes the load raise. Explicit dtypes are not used there because
fastexcel turns a text cell in a float column into a silent null. A refused load falls back to one text load and one
typed load of the number and date fields' columns (``coerce``), taking the typed value where there is one and the
text elsewhere: the cost today's hand-written readers pay on every sheet. Either way the shared core then reads the
cells, so a whole float is an ``int`` field's only where it is whole, and the text a provider wrote is typed as any
other format's.

A body is a workbook by its bytes, not its URL: an xls served as ``.xlsx`` reads.
"""

import datetime as dt
import io
import logging
import re
import zipfile
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, ClassVar, Final, Literal, override
from xml.etree import ElementTree as ET

import fastexcel
import polars as pl

from flatread import core
from flatread.archives import Member, pick
from flatread.core import Plan, Text, column_names
from flatread.errors import CellError, CorruptArchiveError, RaggedRowError, UnknownLayoutError
from flatread.layout import Layout

__all__ = ["Sheet", "Sheets"]

#: An xlsx is a ZIP; an xls is a compound file.
_SIGNATURES: Final = (b"PK\x03\x04", bytes.fromhex("d0cf11e0a1b11ae1"))
#: calamine's text of a number cell, its fraction rounded to nine digits (1e-12 is "0"); a text cell written any
#: other way ("1.50", "1e5", "007") is the provider's text, kept as written.
_RENDERED: Final = r"^-?(0|[1-9]\d{0,14})(\.\d{0,8}[1-9])?$"
_MAIN: Final = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL: Final = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG: Final = "{http://schemas.openxmlformats.org/package/2006/relationships}"

# fastexcel logs a warning for every column it cannot type, which an all-empty column always is.
logging.getLogger("fastexcel").setLevel(logging.ERROR)


@dataclass(frozen=True, slots=True)
class Sheets:
    """Read every matching sheet into one table, each sheet's name in the field *name*."""

    name: str


def _column_range(columns: str) -> list[int]:
    """Return the 0-based indices of a range of sheet columns such as ``"A:G"``."""

    def index(letters: str) -> int:
        number = 0
        for letter in letters:
            number = number * 26 + ord(letter) - ord("A") + 1
        return number - 1

    if (found := re.fullmatch(r"([A-Z]+):([A-Z]+)", columns)) is None:
        msg = f"the column range {columns!r} is not written like 'A:G'"
        raise TypeError(msg)
    return list(range(index(found[1]), index(found[2]) + 1))


class Sheet(Layout):
    """One sheet of an Excel workbook (the ``excel`` extra)."""

    sheet: ClassVar[Member | None] = None
    header_row: ClassVar[int] = 0
    columns: ClassVar[str | None] = None
    end: ClassVar[Literal["blank_row", "last_row"]] = "blank_row"
    footer: ClassVar[str | None] = None
    every_sheet: ClassVar[Sheets | None] = None

    _SETTINGS: ClassVar[frozenset[str]] = Layout._SETTINGS | {  # noqa: SLF001  # a base extends its parent's
        "sheet", "header_row", "columns", "end", "footer", "every_sheet",
    }  # fmt: skip

    @classmethod
    @override
    def _compile(cls, row: type) -> Plan:
        if cls.columns is not None:
            _column_range(cls.columns)
        if cls.footer is not None:
            re.compile(cls.footer)
        return super()._compile(row)  # ty: ignore[invalid-argument-type]

    @classmethod
    @override
    def _carried_fields(cls) -> frozenset[str]:
        return frozenset[str]() if cls.every_sheet is None else frozenset({cls.every_sheet.name})

    @staticmethod
    def _reader(body: bytes) -> fastexcel.ExcelReader:
        if not body.startswith(_SIGNATURES):
            msg = f"the body is not a workbook (an xlsx or xls file): it opens with {body[:40]!r}"
            raise UnknownLayoutError(msg)
        try:
            return fastexcel.read_excel(body)
        except fastexcel.FastExcelError as exc:
            msg = f"the workbook cannot be read: {exc}"
            raise CorruptArchiveError(msg) from exc

    @classmethod
    def _names(cls, reader: fastexcel.ExcelReader) -> list[str]:
        """Return the sheets this table reads: the one ``sheet`` names (or the first), or every match."""
        names = list(reader.sheet_names)
        if cls.every_sheet is not None:
            matched = [n for n in names if cls.sheet is None or _matches(n, cls.sheet)]
            if not matched:
                msg = f"no sheet of the workbook matches {cls.sheet!r}: it holds {names!r}"
                raise UnknownLayoutError(msg)
            return matched
        if cls.sheet is None:
            return names[:1]
        return [pick(names, cls.sheet)]

    @classmethod
    def _kind_of(cls, plan: Plan, labels: list[str]) -> list[object]:
        """Return each column's field kind, by the label set the header is written in (``None`` for text)."""
        kinds = {f.name: f.kind for f in plan.fields}
        for label_set in plan.label_sets:
            names = column_names([label for label, _ in label_set])
            if (labels[: len(names)] if plan.long else labels) == names:
                fields = [f for _, f in label_set]
                rest = [plan.long.value] * (len(labels) - len(fields)) if plan.long else []
                return [kinds.get(f) if f is not None else None for f in [*fields, *rest]]
        return [None] * len(labels)  # the core names the header that is not the layout's

    @classmethod
    def _strict_first(cls) -> bool:
        """Return whether the strict load is worth trying: no setting says a column mixes text into its values."""
        if cls.errata or "cells" in cls._hooks():  # each reads the cells as text, as every other format gives them
            return False
        return not (frozenset(cls.missing) - {""} or cls.missing_by_field or cls.strip or cls.footer is not None)

    @classmethod
    def _load(cls, reader: fastexcel.ExcelReader, name: str, plan: Plan, body: bytes) -> Text:
        """Return sheet *name*'s table as cells under its labels, typed where fastexcel types them.

        calamine parses a sheet whole on every load, so the header comes from the table's own load: one parse on the
        fast path, two on the fallback, as many as today's hand-written readers make.
        """
        common: dict[str, Any] = {"header_row": cls.header_row}
        if cls.columns is not None:
            common["use_columns"] = _column_range(cls.columns)
        sheet = None
        try:
            if cls._strict_first():
                try:
                    sheet = reader.load_sheet(name, dtype_coercion="strict", schema_sample_rows=None, **common)
                except fastexcel.UnsupportedColumnTypeCombinationError:
                    sheet = None
            mixed = sheet is None
            if sheet is None:
                sheet = reader.load_sheet(name, dtypes="string", **common)
        except fastexcel.InvalidParametersError as exc:  # fastexcel's answer to a header row past the sheet's end
            msg = f"sheet {name!r} has no header in row {cls.header_row + 1}"
            raise UnknownLayoutError(msg) from exc
        labels, source = _labels(sheet.selected_columns)
        if not labels:
            msg = f"sheet {name!r} has no header in row {cls.header_row + 1}"
            raise UnknownLayoutError(msg)
        whole = sheet.to_polars()
        frame = whole.select(source)
        if mixed:
            frame = cls._mixed(reader, name, frame, common, cls._kind_of(plan, column_names(labels)))
        frame, rows = cls._bound(frame, name)
        first = cls.header_row + 2
        cls._past_header(whole.drop(source), sheet.selected_columns, rows, name, first)
        under = {_letters(c.absolute_index): label for c, label in zip(sheet.selected_columns, labels, strict=False)}
        cls._errors(body, name, under, {first + r for r in rows})
        frame.columns = column_names(labels)
        headings = None
        if cls.headings is not None:  # the one setting that costs a parse of its own: the row above the header
            above = reader.load_sheet(
                name, header_row=None, skip_rows=cls.header_row - 1, n_rows=1, dtypes="string",
                use_columns=common.get("use_columns"),
            ).to_polars()  # fmt: skip
            headings = tuple(above.row(0)[: len(labels)]) if above.height else None
        return Text(frame, lambda i: f"sheet {name!r} row {first + rows[i]}", headings=headings)

    @classmethod
    def _mixed(
        cls,
        reader: fastexcel.ExcelReader,
        name: str,
        text: pl.DataFrame,
        common: dict[str, Any],
        kinds: list[object],
    ) -> pl.DataFrame:
        """Return the text load, each number and date its typed load's value: the fallback's second parse."""
        typed_kinds: dict[str | int, fastexcel.DType] = {
            column: "datetime" if kind in {dt.date, dt.datetime} else "float"
            for column, kind in zip(text.columns, kinds, strict=True)
            if kind in {int, float, Decimal, dt.date, dt.datetime, str}
        }
        if not typed_kinds:
            return text
        typed = reader.load_sheet(
            name, header_row=common["header_row"], use_columns=[str(column) for column in typed_kinds],
            dtypes=typed_kinds, dtype_coercion="coerce",
        ).to_polars()  # fmt: skip
        columns = []
        for column, kind in typed_kinds.items():
            value, written = typed[str(column)], pl.col(str(column))
            if kind == "datetime":
                as_text = value.dt.strftime("%Y-%m-%d")
                choose = value.is_not_null() & (value.dt.time() == dt.time())
            else:
                whole = value.is_finite() & (value == value.floor()) & (value.abs() < 2**53)
                as_text = pl.select(
                    pl.when(whole)
                    .then(value.cast(pl.Int64, strict=False).cast(pl.String))
                    .otherwise(value.cast(pl.String))
                ).to_series()
                choose = value.is_not_null() & written.str.contains(_RENDERED).fill_null(value=False)
                choose = choose | (value.is_not_null() & written.is_null())
            columns.append(pl.when(choose).then(pl.lit(as_text)).otherwise(written).alias(str(column)))
        return text.with_columns(columns)

    @staticmethod
    def _past_header(
        rest: pl.DataFrame, columns: list[fastexcel.ColumnInfo], rows: list[int], name: str, first: int
    ) -> None:
        """Raise where a column past the header's last label holds a cell in the table's rows: a row wider than it."""
        letters = {c.name: _letters(c.absolute_index) for c in columns}
        for column in rest.iter_columns():
            cells = column[rows].cast(pl.String)
            if (filled := (cells.is_not_null() & (cells != "")).arg_true()).len():
                at = int(filled[0])
                msg = (
                    f"sheet {name!r} row {first + rows[at]}: column {letters[column.name]} holds {cells[at]!r} "
                    "under no label, so the row is wider than the header"
                )
                raise RaggedRowError(msg)

    @staticmethod
    def _errors(body: bytes, name: str, under: dict[str, str], rows: set[int]) -> None:
        """Raise at the table's first Excel error cell (``#N/A``, ``#DIV/0!``), which fastexcel reads as empty.

        *under* maps each labeled column's letters to its label, and *rows* holds the table's sheet row numbers.
        """
        for letters, row, text in _error_cells(body, name):
            if letters in under and row in rows:
                where = f"sheet {name!r} row {row}"
                raise CellError(where, under[letters], text, "a value: it is an Excel error, a formula that failed")

    @classmethod
    def _bound(cls, frame: pl.DataFrame, name: str) -> tuple[pl.DataFrame, list[int]]:
        """Return the table's rows, where it ends, and each kept row's place; a filled row after the end raises."""
        blank = pl.all_horizontal(pl.all().is_null() | (pl.all().cast(pl.String) == ""))
        empty = frame.select(blank).to_series()
        if cls.end == "last_row":
            kept = (~empty).arg_true()
            return frame.filter(~empty), [int(i) for i in kept]
        stop = int(empty.arg_true()[0]) if empty.any() else frame.height
        after = frame.slice(stop).with_row_index("\x00at").filter(~empty.slice(stop))
        for at, first in zip(after["\x00at"], after.to_series(1), strict=True):
            text = "" if first is None else str(first)
            if cls.footer is None or not re.fullmatch(cls.footer, text):
                row = cls.header_row + 2 + stop + int(at)
                msg = f"sheet {name!r} row {row}: a filled row after the table's end ({text[:200]!r})"
                raise UnknownLayoutError(msg)
        return frame.head(stop), list(range(stop))

    @classmethod
    @override
    def read_text(cls, body: bytes) -> Text:
        reader = cls._reader(body)
        return cls._load(reader, cls._names(reader)[0], cls._checked_plan(), body)

    @classmethod
    @override
    def frame(cls, body: bytes) -> pl.DataFrame:
        if cls.every_sheet is None:
            return super().frame(body)
        plan = cls._checked_plan()
        cls._refuse(body)
        body = cls._unwrap(body)
        reader = cls._reader(body)
        frames = []
        for name in cls._names(reader):
            text = cls._load(reader, name, plan, body)
            text.carried[cls.every_sheet.name] = name
            frames.append(core.run(plan, text, **cls._hooks()))
        return pl.concat(frames, how="vertical_relaxed")  # a Decimal column takes the widest sheet's scale


def _labels(columns: list[fastexcel.ColumnInfo]) -> tuple[list[str], list[str]]:
    """Return the header's labels as written and fastexcel's names for their columns, trailing empty ones dropped.

    fastexcel names an empty label ``__UNNAMED__<i>`` and a repeat ``<label>_1``, ``<label>_2``.
    """
    labels: list[str] = []
    names: list[str] = []
    for column in columns:
        label = "" if column.column_name_from == "generated" else column.name
        # shortcut: a label written `X_1` after an `X` reads as X's repeat; split the load if a provider writes one.
        if (found := re.fullmatch(r"(.+)_(\d+)", label)) and found[1] in labels:
            label = found[1]
        labels.append(label)
        names.append(column.name)
    while labels and not labels[-1]:
        labels.pop()
        names.pop()
    return labels, names


def _matches(name: str, member: Member) -> bool:
    if isinstance(member, str):
        return name == member
    if isinstance(member, re.Pattern):
        return bool(member.fullmatch(name))
    return member(name)


def _letters(index: int) -> str:
    """Return a 0-based column index as the sheet's letters: 0 is A, 26 is AA."""
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def _error_cells(body: bytes, name: str) -> list[tuple[str, int, str]]:
    """Return sheet *name*'s error cells, each its column's letters, its row and its text, from the xlsx's XML.

    fastexcel reads an error cell as empty, and an empty cell is a missing value, so the XML is the only place it
    shows. Only a sheet holding one pays more than a substring search of its XML.
    """
    if not body.startswith(b"PK"):
        return []  # shortcut: an xls's error cells read as empty; parse its BIFF records if a provider writes them.
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        # expat refuses entity-expansion bombs, and these two parts are small.
        book = ET.fromstring(zf.read("xl/workbook.xml"))  # noqa: S314
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))  # noqa: S314
        rid = next(s.get(f"{_REL}id") for s in book.iter(f"{_MAIN}sheet") if s.get("name") == name)
        target = next(r.get("Target", "") for r in rels.iter(f"{_PKG}Relationship") if r.get("Id") == rid)
        xml = zf.read(target.lstrip("/") if target.startswith("/") else f"xl/{target}")
    if b't="e"' not in xml and b"t='e'" not in xml:
        return []
    found: list[tuple[str, int, str]] = []
    for cell in re.finditer(rb"<c\b([^>]*\bt=[\"']e[\"'][^>]*)>(.*?)</c>", xml, re.DOTALL):
        ref = re.search(rb"\br=[\"']([A-Z]+)(\d+)[\"']", cell[1])
        value = re.search(rb"<v>(.*?)</v>", cell[2], re.DOTALL)
        if ref is not None:
            found.append((ref[1].decode(), int(ref[2]), "" if value is None else value[1].decode()))
    return found
