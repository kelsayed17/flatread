"""How many times a sheet reader opens the workbook and loads a sheet, per read: once, and twice a sheet.

fastexcel parses the whole sheet on every ``load_sheet`` (an ``n_rows=1`` load costs most of a full one), so a read
loads its sheet's text once, which serves everything around the table, and its table typed once.
"""

import collections
import datetime as dt
import re
import typing
from typing import Annotated

import fastexcel
import msgspec
import pytest

from flatread.fields import Cell, Label
from flatread.sheets import (
    BesideSheet,
    CellHeaderSheet,
    DatedSheet,
    GroupedSheet,
    NotedSheet,
    NumberTextSheet,
    OnlySheet,
    PaddedSheet,
    PreambleSheet,
    SheetFile,
    SheetsFile,
    SheetText,
    TitledSheet,
)
from tests.support.xlsx import workbook

if typing.TYPE_CHECKING:
    from collections.abc import Callable

    from flatread.core import File

_DAY = dt.date(2026, 1, 2)


class _Item(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("Name")]
    count: Annotated[int, Label("Count")]


class _Day(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Label("Day")]
    value: Annotated[float, Label("Value")]


class _Answer(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Label("")]
    question: str
    answer: str
    share: float


class _Name(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("Name")]


class _Year(msgspec.Struct, frozen=True, gc=False):
    year: int
    name: Annotated[str, Label("Name")]


class _Stated(msgspec.Struct, frozen=True, gc=False):
    """A date stated over the table, in column ``C``, which no column of the table is, and a code in ``A1``."""

    code: Annotated[str, Cell("A1")]
    as_of: Annotated[dt.date, Cell("C1")]
    name: Annotated[str, Label("Name")]
    count: Annotated[int, Label("Count")]


class _Over(msgspec.Struct, frozen=True, gc=False):
    """A date stated over a column of whole numbers, which one typed load cannot read as a date too."""

    as_of: Annotated[dt.date, Cell("B1")]
    name: Annotated[str, Label("Name")]
    count: Annotated[int, Label("Count")]


class _Twice(msgspec.Struct, frozen=True, gc=False):
    """A date and a time of day stated over one column of whole numbers: the second takes a load of its own."""

    as_of: Annotated[dt.date, Cell("B1")]
    at: Annotated[dt.time, Cell("B2")]
    name: Annotated[str, Label("Name")]
    count: Annotated[int, Label("Count")]


class _Titled(TitledSheet[_Item]):
    _titles = (re.compile("Title"),)


class _Stripped(SheetFile[_Item]):
    _strip = True


class _Answers(GroupedSheet[_Answer]):
    _groups_row = 1
    _empty_rows = False


_TABLE = [["Name", "Count"], ["a", 1]]
_UNDER_TITLE = [["Title"], *_TABLE]
_READS: list[tuple[str, File[typing.Any], bytes, int, int]] = [
    ("SheetFile", SheetFile(_Item, sheet="S"), workbook({"S": _TABLE}), 1, 2),
    ("BesideSheet", BesideSheet(_Item, sheet="S"), workbook({"S": [["Name", "Count", "x"], ["a", 1, "y"]]}), 1, 2),
    ("NotedSheet", NotedSheet(_Item, sheet="S"), workbook({"S": [*_TABLE, ["Source: x"]]}), 1, 2),
    ("DatedSheet", DatedSheet(_Day, sheet="S"), workbook({"S": [["Day", "Value"], [_DAY, 1.5], ["note"]]}), 1, 2),
    ("PaddedSheet", PaddedSheet(_Item, sheet="S"), workbook({"S": [["Name", "Count"], [" a", 1]]}), 1, 2),
    ("NumberTextSheet", NumberTextSheet(_Item, sheet="S"), workbook({"S": [["Name", "Count"], ["a", "1"]]}), 1, 2),
    ("CellHeaderSheet", CellHeaderSheet(_Item, sheet="S"), workbook({"S": _TABLE}), 1, 2),
    ("a stripped header", _Stripped(_Item, sheet="S"), workbook({"S": _TABLE}), 1, 2),
    ("TitledSheet", _Titled(_Item, sheet="S", header_row=2), workbook({"S": _UNDER_TITLE}), 1, 2),
    ("PreambleSheet", PreambleSheet(_Item, sheet="S"), workbook({"S": [["banner"], ["link"], *_TABLE]}), 1, 2),
    ("OnlySheet", OnlySheet(_Item), workbook({"Any name": _TABLE}), 1, 2),
    (
        "GroupedSheet",
        _Answers(_Answer, sheet="S", header_row=2),
        workbook({"S": [["a link", "One", None, "Two"], [None, "a", "b", "a"], [_DAY, 1, None, 3]]}),
        1,
        2,
    ),
    (
        "SheetsFile, three sheets",
        SheetsFile(_Year, sheets=re.compile(r"(?P<year>\d{4})"), each=lambda sheet: SheetFile(_Name, sheet=sheet)),
        workbook({"2024": [["Name"], ["a"]], "2025": [["Name"], ["b"]], "2026": [["Name"], ["c"]]}),
        1,
        6,
    ),
    ("Cell fields", SheetFile(_Stated, sheet="S", header_row=2), workbook({"S": [["x", None, _DAY], *_TABLE]}), 1, 2),
    (
        "a date Cell over a column of numbers",
        SheetFile(_Over, sheet="S", header_row=2),
        workbook({"S": [[None, _DAY], *_TABLE]}),
        1,
        3,
    ),
    (
        "a date and a time Cell over one column of numbers",
        SheetFile(_Twice, sheet="S", header_row=3),
        workbook({"S": [[None, _DAY], [None, dt.time(9, 30)], *_TABLE]}),
        1,
        4,
    ),
    ("SheetText", SheetText(_Item, sheet="S"), workbook({"S": _TABLE}), 1, 2),
]


@pytest.mark.parametrize(
    ("reader", "body", "opens", "loads"), [read[1:] for read in _READS], ids=[r[0] for r in _READS]
)
def test_a_read_opens_the_workbook_once_and_loads_each_sheet_twice(
    reader: File[typing.Any], body: bytes, opens: int, loads: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: collections.Counter[str] = collections.Counter()

    def counted[**P, R](name: str, call: Callable[P, R]) -> Callable[P, R]:
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            calls[name] += 1
            return call(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(fastexcel, "read_excel", counted("read_excel", fastexcel.read_excel))
    monkeypatch.setattr(fastexcel.ExcelReader, "load_sheet", counted("load_sheet", fastexcel.ExcelReader.load_sheet))
    assert reader.rows(body)
    assert calls == {"read_excel": opens, "load_sheet": loads}
