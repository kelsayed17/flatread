"""How many times a sheet reader opens the workbook and loads a sheet, per read: once, and two or three times a sheet.

fastexcel parses the whole sheet on every ``load_sheet`` (an ``n_rows=1`` load costs most of a full one), so a read
loads its sheet's text once, which serves everything around the table, and its table typed once. A table with a
number field loads its number columns a third time, as booleans: no text tells a text cell written as a number
(``2``) from a number cell, and fastexcel's boolean load reads only a number (or boolean) cell as a value, so the
third load is what refuses the text cell (NumberTextSheet, which reads such text, skips it). A plain table whose every
column holds one kind of cell, its field's, is read from one strict load, which refuses a column mixing number cells
and number text; one with an empty cell pays a second, as text, since the strict load reads a word for none (``NA``)
as none too; one that mixes kinds pays the failed strict load and the two or three.
"""

import collections
import datetime as dt
import re
import typing
from typing import Annotated

import fastexcel
import msgspec
import pytest

from flatread.fields import Cell, Flag, Format, Label
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
from tests.support.xlsx import Grid, workbook

if typing.TYPE_CHECKING:
    from collections.abc import Callable

    import polars as pl

    from flatread.core import File
    from tests.support import xlsx

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
    ("SheetFile", SheetFile(_Item, sheet="S"), workbook({"S": _TABLE}), 1, 1),
    ("a column of mixed kinds", SheetFile(_Item, sheet="S"), workbook({"S": [*_TABLE, [2, 3]]}), 1, 4),
    ("tokens for none", SheetFile(_Item, sheet="S", null_values=["."]), workbook({"S": _TABLE}), 1, 3),
    ("BesideSheet", BesideSheet(_Item, sheet="S"), workbook({"S": [["Name", "Count", "x"], ["a", 1, "y"]]}), 1, 3),
    ("NotedSheet", NotedSheet(_Item, sheet="S"), workbook({"S": [*_TABLE, ["Source: x"]]}), 1, 3),
    ("DatedSheet", DatedSheet(_Day, sheet="S"), workbook({"S": [["Day", "Value"], [_DAY, 1.5], ["note"]]}), 1, 3),
    ("PaddedSheet", PaddedSheet(_Item, sheet="S"), workbook({"S": [["Name", "Count"], [" a", 1]]}), 1, 3),
    ("NumberTextSheet", NumberTextSheet(_Item, sheet="S"), workbook({"S": [["Name", "Count"], ["a", "1"]]}), 1, 2),
    ("CellHeaderSheet", CellHeaderSheet(_Item, sheet="S"), workbook({"S": _TABLE}), 1, 3),
    ("a stripped header", _Stripped(_Item, sheet="S"), workbook({"S": _TABLE}), 1, 3),
    ("TitledSheet", _Titled(_Item, sheet="S", header_row=2), workbook({"S": _UNDER_TITLE}), 1, 3),
    ("PreambleSheet", PreambleSheet(_Item, sheet="S"), workbook({"S": [["banner"], ["link"], *_TABLE]}), 1, 3),
    ("OnlySheet", OnlySheet(_Item), workbook({"Any name": _TABLE}), 1, 3),
    (
        "GroupedSheet",
        _Answers(_Answer, sheet="S", header_row=2),
        workbook({"S": [["a link", "One", None, "Two"], [None, "a", "b", "a"], [_DAY, 1, None, 3]]}),
        1,
        3,
    ),
    (
        "SheetsFile, three sheets",
        SheetsFile(_Year, sheets=re.compile(r"(?P<year>\d{4})"), each=lambda sheet: SheetFile(_Name, sheet=sheet)),
        workbook({"2024": [["Name"], ["a"]], "2025": [["Name"], ["b"]], "2026": [["Name"], ["c"]]}),
        1,
        6,
    ),
    ("Cell fields", SheetFile(_Stated, sheet="S", header_row=2), workbook({"S": [["x", None, _DAY], *_TABLE]}), 1, 3),
    (
        "a date Cell over a column of numbers",
        SheetFile(_Over, sheet="S", header_row=2),
        workbook({"S": [[None, _DAY], *_TABLE]}),
        1,
        4,
    ),
    (
        "a date and a time Cell over one column of numbers",
        SheetFile(_Twice, sheet="S", header_row=3),
        workbook({"S": [[None, _DAY], [None, dt.time(9, 30)], *_TABLE]}),
        1,
        5,
    ),
    ("SheetText", SheetText(_Item, sheet="S"), workbook({"S": _TABLE}), 1, 2),
]


def _counted[**P, R](calls: collections.Counter[str], name: str, call: Callable[P, R]) -> Callable[P, R]:
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        calls[name] += 1
        return call(*args, **kwargs)

    return wrapper


@pytest.mark.parametrize(
    ("reader", "body", "opens", "loads"), [read[1:] for read in _READS], ids=[r[0] for r in _READS]
)
def test_how_often_a_read_opens_the_workbook_and_loads_a_sheet(
    reader: File[typing.Any], body: bytes, opens: int, loads: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: collections.Counter[str] = collections.Counter()
    monkeypatch.setattr(fastexcel, "read_excel", _counted(calls, "read_excel", fastexcel.read_excel))
    monkeypatch.setattr(
        fastexcel.ExcelReader, "load_sheet", _counted(calls, "load_sheet", fastexcel.ExcelReader.load_sheet)
    )
    assert reader.rows(body)
    assert calls == {"read_excel": opens, "load_sheet": loads}


class _Kinds(msgspec.Struct, frozen=True, gc=False):
    """A field of each kind a sheet's cell holds, and one whose column is empty all down."""

    name: Annotated[str | None, Label("Name")]
    count: Annotated[int, Label("Count")]
    rate: Annotated[float | None, Label("Rate")]
    held: Annotated[bool, Label("Held")]
    day: Annotated[dt.date, Label("Day")]
    at: Annotated[dt.datetime, Label("At")]
    clock: Annotated[dt.time, Label("Clock")]
    written: Annotated[dt.date, Label("Written"), Format("%m/%d/%Y")]
    flagged: Annotated[bool, Label("Flagged"), Flag("Y", "N")]
    empty: Annotated[float | None, Label("Empty")]


class _TwoLoads[RowT: msgspec.Struct](SheetFile[RowT]):
    """A SheetFile by a hook of its own, which reads by the two loads."""

    @typing.override
    def _end(self, text: pl.DataFrame) -> int:
        return super()._end(text)


_KINDS: list[xlsx.Cell] = ["Name", "Count", "Rate", "Held", "Day", "At", "Clock", "Written", "Flagged", "Empty"]
_KIND_ROWS: Grid = [
    ["a", 1, 1.5, True, _DAY, _DAY, dt.time(9, 30), "01/02/2026", "Y", None],
    ["", 2, None, False, _DAY, _DAY, dt.time(16, 0, 5), "12/31/2025", "N", None],
]


def test_one_load_reads_a_plain_table_as_the_two_loads_do(monkeypatch: pytest.MonkeyPatch) -> None:
    body = workbook({"S": [_KINDS, *_KIND_ROWS]})
    expected = _TwoLoads(_Kinds, sheet="S").frame(body)
    calls: collections.Counter[str] = collections.Counter()
    monkeypatch.setattr(
        fastexcel.ExcelReader, "load_sheet", _counted(calls, "load_sheet", fastexcel.ExcelReader.load_sheet)
    )
    frame = SheetFile(_Kinds, sheet="S").frame(body)
    # The strict load, and the empty cells' columns as text: a word for none (NA) is none to the strict load too.
    assert calls["load_sheet"] == 2
    assert frame.equals(expected)
    assert frame.schema == expected.schema


def _with(at: int, *, cell: xlsx.Cell) -> Grid:
    rows = [list(row) for row in _KIND_ROWS]
    rows[1][at] = cell
    return [_KINDS, *rows]


@pytest.mark.parametrize(
    "grid",
    [
        pytest.param(_with(1, cell=1.5), id="a fraction in a whole-number column"),
        pytest.param(_with(2, cell="x"), id="text among numbers"),
        pytest.param(_with(2, cell=True), id="a boolean among numbers"),
        pytest.param(_with(0, cell=7), id="a number among text"),
        pytest.param(_with(4, cell=dt.time(9, 30)), id="a time in a date column"),
        pytest.param(_with(6, cell=_DAY), id="a date in a time column"),
        pytest.param(_with(7, cell="2026-01-02"), id="text not in its Format"),
        pytest.param(_with(8, cell="maybe"), id="a word not its Flag's"),
        pytest.param([_KINDS, _KIND_ROWS[0], [None] * len(_KINDS), _KIND_ROWS[1]], id="an empty row"),
        pytest.param([_KINDS, _KIND_ROWS[0], [*_KIND_ROWS[1], "stray"]], id="a cell beside the table"),
        pytest.param([["Label", *_KINDS[1:]], *_KIND_ROWS], id="a header that is not the labels"),
    ],
)
def test_what_one_load_cannot_read_the_two_loads_decide(grid: Grid) -> None:
    body = workbook({"S": grid})
    try:
        expected: object = _TwoLoads(_Kinds, sheet="S").rows(body)
    except ValueError as e:
        expected = str(e)
    try:
        read: object = SheetFile(_Kinds, sheet="S").rows(body)
    except ValueError as e:
        read = str(e)
    assert read == expected
