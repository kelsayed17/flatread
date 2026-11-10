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
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import ClassVar, Final, Literal, override

import fastexcel
import polars as pl

from flatread import core
from flatread.archives import Member, pick
from flatread.core import Plan, Text, column_names
from flatread.errors import CorruptArchiveError, UnknownLayoutError
from flatread.layout import Layout

__all__ = ["Sheet", "Sheets"]

#: An xlsx is a ZIP; an xls is a compound file.
_SIGNATURES: Final = (b"PK\x03\x04", bytes.fromhex("d0cf11e0a1b11ae1"))
#: A cell the text load wrote as a decimal number, its digits rounded to nine: a point or an exponent.
_DECIMAL: Final = r"^[+-]?(\d+\.\d*|\.\d+|\d+(\.\d*)?[eE][+-]?\d+)$"

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
    def _load(cls, reader: fastexcel.ExcelReader, name: str, plan: Plan) -> Text:
        """Return sheet *name*'s table as cells under its labels, typed where fastexcel types them."""
        wanted = _column_range(cls.columns) if cls.columns is not None else None
        top = cls.header_row - (cls.headings is not None)
        head = reader.load_sheet(
            name, header_row=None, skip_rows=top, n_rows=cls.header_row - top + 1, dtypes="string",
            use_columns=wanted,
        ).to_polars()  # fmt: skip
        if head.height < cls.header_row - top + 1:
            msg = f"sheet {name!r} ends before its header row {cls.header_row + 1}"
            raise UnknownLayoutError(msg)
        cells = ["" if cell is None else str(cell) for cell in head.row(-1)]
        while cells and not cells[-1]:
            cells.pop()
        source = head.columns[: len(cells)]
        headings = tuple(head.row(0)[: len(cells)]) if cls.headings is not None else None
        try:
            frame = reader.load_sheet(
                name, header_row=None, skip_rows=cls.header_row + 1, use_columns=source,
                dtype_coercion="strict", schema_sample_rows=None,
            ).to_polars()  # fmt: skip
        except fastexcel.UnsupportedColumnTypeCombinationError:
            frame = cls._mixed(reader, name, source, cls._kind_of(plan, column_names(cells)))
        frame, rows = cls._bound(frame.select(source), name)
        frame.columns = column_names(cells)
        first = cls.header_row + 2
        return Text(frame, lambda i: f"sheet {name!r} row {first + rows[i]}", headings=headings)

    @classmethod
    def _mixed(cls, reader: fastexcel.ExcelReader, name: str, source: list[str], kinds: list[object]) -> pl.DataFrame:
        """Return the table as text, each number and date its typed load's value: the two-load fallback."""
        text = reader.load_sheet(
            name, header_row=None, skip_rows=cls.header_row + 1, use_columns=source, dtypes="string"
        ).to_polars()
        typed_kinds: dict[str | int, fastexcel.DType] = {
            column: "datetime" if kind in {dt.date, dt.datetime} else "float"
            for column, kind in zip(source, kinds, strict=True)
            if kind in {int, float, Decimal, dt.date, dt.datetime, str}
        }
        if not typed_kinds:
            return text
        typed = reader.load_sheet(
            name, header_row=None, skip_rows=cls.header_row + 1, use_columns=[str(column) for column in typed_kinds],
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
                    pl.when(whole).then(value.cast(pl.Int64).cast(pl.String)).otherwise(value.cast(pl.String))
                ).to_series()
                choose = value.is_not_null() & written.str.contains(_DECIMAL).fill_null(value=False)
                choose = choose | (value.is_not_null() & written.is_null())
            columns.append(pl.when(choose).then(pl.lit(as_text)).otherwise(written).alias(str(column)))
        return text.with_columns(columns)

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
        return cls._load(reader, cls._names(reader)[0], cls._checked_plan())

    @classmethod
    @override
    def frame(cls, body: bytes) -> pl.DataFrame:
        if cls.every_sheet is None:
            return super().frame(body)
        plan = cls._checked_plan()
        cls._refuse(body)
        reader = cls._reader(cls._unwrap(body))
        frames = []
        for name in cls._names(reader):
            text = cls._load(reader, name, plan)
            text.carried[cls.every_sheet.name] = name
            frames.append(core.run(plan, text, **cls._hooks()))
        return pl.concat(frames, how="vertical")


def _matches(name: str, member: Member) -> bool:
    if isinstance(member, str):
        return name == member
    if isinstance(member, re.Pattern):
        return bool(member.fullmatch(name))
    return member(name)
