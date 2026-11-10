"""Faults the second round of trial ports (12 more providers on their recorded bodies) found: each pins the fix."""

import datetime as dt
import io
import zipfile
from decimal import Decimal

import msgspec
import pytest

from flatread.core import Erratum, Long
from flatread.delimited import Csv, Tsv
from flatread.errors import CellError, EmptyBodyError, RaggedRowError, UnknownLayoutError
from flatread.records import Json
from flatread.sheets import Sheet, Sheets
from tests.xlsx import workbook

# --- dates ---


class Stamp(msgspec.Struct, frozen=True):
    at: dt.datetime


class Day(msgspec.Struct, frozen=True):
    day: dt.date


def test_an_upper_case_month_name_reads() -> None:
    class Oracle(Csv):  # SEC's data sets write their dates as Oracle does: 30-SEP-2026
        row = Day
        labels = {"D": "day"}
        dates = {"day": "%d-%b-%Y"}

    assert Oracle.frame(b"D\n30-SEP-2026\n30-Sep-2026\n")["day"].to_list() == [dt.date(2026, 9, 30)] * 2


def test_a_fraction_of_a_second_reads_however_many_digits_it_has() -> None:
    class Accepted(Csv):
        row = Stamp
        labels = {"T": "at"}
        dates = {"at": "%Y-%m-%d %H:%M:%S%.f"}

    body = b"T\n2026-08-04 17:40:00.0\n2026-08-04 17:40:00.000000\n2026-08-04 17:40:00.5\n2026-08-04 17:40:00\n"
    assert Accepted.frame(body)["at"].to_list() == [
        dt.datetime(2026, 8, 4, 17, 40), dt.datetime(2026, 8, 4, 17, 40),
        dt.datetime(2026, 8, 4, 17, 40, 0, 500000), dt.datetime(2026, 8, 4, 17, 40),
    ]  # fmt: skip


def test_an_offset_reads_as_its_utc_time_beside_naive_formats() -> None:
    class Quotes(Csv):
        row = Stamp
        labels = {"T": "at"}
        dates = {"at": ("%Y-%m-%dT%H:%M:%S%:z", "%Y-%m-%dT%H:%M:%S")}

    body = b"T\n2023-04-15T12:30:00-04:00\n2023-04-15T16:30:00\n"
    assert Quotes.frame(body)["at"].to_list() == [dt.datetime(2023, 4, 15, 16, 30)] * 2
    with pytest.raises(CellError):
        Quotes.frame(b"T\n2023-04-15T25:30:00-04:00\n")


# --- long tables and blank lines ---


class Point(msgspec.Struct, frozen=True):
    unit: str
    series: str
    value: float


class Wide(Csv):
    row = Point
    labels = {"U": "unit"}
    long = Long(name="series", value="value", names=r"\d+")


def test_an_empty_melted_cell_raises_unless_the_layout_drops_them() -> None:
    with pytest.raises(CellError, match="line 2"):
        Wide.frame(b"U,1,2\nx,,5\n")

    class Dropping(Wide):
        long = Long(name="series", value="value", names=r"\d+", drop_missing=True)

    assert Dropping.frame(b"U,1,2\nx,,5\n").rows() == [("x", "2", 5.0)]


class Pair(msgspec.Struct, frozen=True):
    a: str
    b: str


def test_a_blank_line_inside_the_table_raises_and_trailing_ones_are_framing() -> None:
    class Pairs(Tsv):
        row = Pair
        labels = {"A": "a", "B": "b"}

    assert Pairs.frame(b"A\tB\n1\t2\n\n\n").rows() == [("1", "2")]
    with pytest.raises(RaggedRowError, match="line 3 is blank"):
        Pairs.frame(b"A\tB\n1\t2\n\n3\t4\n")


class One(msgspec.Struct, frozen=True):
    s: str | None


def test_a_blank_line_in_a_one_column_table_is_an_empty_cell() -> None:
    class Column(Csv):
        row = One
        labels = {"S": "s"}

    assert Column.frame(b"S\na\n\nb\n")["s"].to_list() == ["a", None, "b"]


# --- archives and sheets ---


class Zipped(Csv):
    row = Pair
    labels = {"A": "a", "B": "b"}
    member = "a.csv"


def test_an_empty_or_unzipped_body_for_a_member_names_what_it_is() -> None:
    with pytest.raises(EmptyBodyError):
        Zipped.frame(b"")
    with pytest.raises(UnknownLayoutError, match="not a ZIP"):
        Zipped.frame(b"<html>maintenance</html>")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("a.csv", b"A,B\n1,2\n")
    assert Zipped.frame(buffer.getvalue()).rows() == [("1", "2")]


class Cell(msgspec.Struct, frozen=True):
    name: str
    value: float


def test_a_missing_sheet_is_called_a_sheet() -> None:
    class Cells(Sheet):
        row = Cell
        labels = {"Name": "name", "Value": "value"}
        sheet = "Data 1"

    with pytest.raises(UnknownLayoutError, match=r"no sheet of the workbook matches 'Data 1'"):
        Cells.frame(workbook({"Other": [["Name", "Value"], ["a", 1.0]]}))


class Named(msgspec.Struct, frozen=True):
    sheet: str
    value: float


def test_an_erratum_on_one_of_every_sheet_is_found() -> None:
    class Every(Sheet):
        row = Named
        labels = {"Value": "value"}
        every_sheet = Sheets("sheet")
        missing = frozenset({"", "."})  # the text load, as an erratum's own cell is text
        errata = (Erratum("Value", "1,5", "1.5", "a decimal comma on one sheet"),)

    body = workbook({"A": [["Value"], ["1,5"]], "B": [["Value"], [2.0]]})
    assert Every.frame(body).rows() == [("A", 1.5), ("B", 2.0)]

    class Gone(Every):
        errata = (Erratum("Value", "9,9", "9.9", "fixed by the provider"),)

    with pytest.raises(Exception, match="is gone"):
        Gone.frame(body)


# --- JSON ---


class Quote(msgspec.Struct, frozen=True):
    sym: str
    px: Decimal
    size: int
    shown: bool


class Quotes(Json):
    row = Quote
    labels = {"sym": "sym", "px": "px", "size": "size", "shown": "shown"}


def test_a_json_number_written_as_a_string_reads_beside_real_numbers() -> None:
    body = (
        b'[{"sym": "A", "px": "1.25", "size": "100", "shown": "true"},'
        b' {"sym": "B", "px": 2.5, "size": 7, "shown": false}]'
    )
    assert Quotes.frame(body).rows() == [("A", Decimal("1.25"), 100, True), ("B", Decimal("2.50"), 7, False)]


def test_strip_keeps_numbers_native() -> None:
    class Stripped(Quotes):
        strip = True

    assert Stripped.frame(b'[{"sym": " A ", "px": 1.5, "size": 3, "shown": true}]').rows() == [
        ("A", Decimal("1.5"), 3, True)
    ]


def test_a_json_body_may_open_with_a_byte_order_mark() -> None:
    assert Quotes.frame(b'\xef\xbb\xbf[{"sym": "A", "px": 1, "size": 1, "shown": true}]').height == 1
