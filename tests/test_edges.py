"""The refusals and native paths the per-format tests leave out: each pins what a reader gets."""

import datetime as dt
import io
import re
import zipfile
from decimal import Decimal
from typing import Literal

import msgspec
import polars as pl
import pytest

import flatread
from flatread import __main__ as cli
from flatread.cells import EXCEL_SERIAL, fields_of, type_frame
from flatread.check import report
from flatread.core import Long
from flatread.delimited import Csv, Line, Pipe
from flatread.errors import (
    CellError,
    CorruptArchiveError,
    EmptyBodyError,
    EncodingError,
    NotPublishedError,
    UnknownLayoutError,
)
from flatread.fixed import FixedWidth
from flatread.records import Json
from flatread.scaffold import scaffold
from flatread.sheets import Sheet, Sheets
from tests.xlsx import workbook

# --- cells: columns a format already typed ---


class Native(msgspec.Struct):
    day: dt.date
    stamp: dt.datetime
    flag: bool
    code: str
    money: Decimal
    serial: dt.date


def test_native_columns_are_checked_and_converted() -> None:
    fields = fields_of(Native, dates={"serial": EXCEL_SERIAL})
    text = pl.DataFrame({
        "day": [dt.datetime(2026, 11, 9), dt.datetime(2026, 11, 9, 12)],
        "stamp": pl.Series([dt.datetime(2026, 11, 9, 14, 30)] * 2, dtype=pl.Datetime("ms")),
        "flag": [True, False],
        "code": [1001.0, 2020.5],
        "money": [1.25, 3.0],
        "serial": [45000.0, 45001.0],
    })  # fmt: skip
    frame, bad = type_frame(text, fields)
    assert frame.row(0) == (
        dt.date(2026, 11, 9), dt.datetime(2026, 11, 9, 14, 30), True, "1001", Decimal("1.25"), dt.date(2023, 3, 15)
    )  # fmt: skip
    assert frame["code"].to_list() == ["1001", "2020.5"]
    assert bad["day"].to_list() == [False, True]  # a date cell with a time is no date


class Kinds(msgspec.Struct):
    kind: Literal["US", "State"] | None
    day: dt.date


def test_messages_name_what_the_cell_must_be() -> None:
    fields = fields_of(Kinds, dates={"day": ("%d/%m/%Y", flatread.QUARTER)})
    assert fields[0].kind == ("US", "State")
    assert fields[0].expected == "one of ['US', 'State']"
    assert fields[1].expected == "a date written %d/%m/%Y or a quarter written 2024Q1"

    class Days(Csv):
        row = Kinds
        labels = {"Kind": "kind", "Day": "day"}

    with pytest.raises(CellError, match="is not a date written %Y-%m-%d: the field always holds a value"):
        Days.frame(b"Kind,Day\nUS,\n")

    class Empty(Days):
        min_rows = 0

    assert Empty.frame(b"Kind,Day\n").schema["kind"] == pl.Enum(["US", "State"])


def test_settings_naming_no_field_or_the_wrong_kind_raise() -> None:
    with pytest.raises(TypeError, match="no field 'nope'"):
        fields_of(Kinds, missing_by_field={"nope": frozenset()})
    with pytest.raises(TypeError, match="not a date"):
        fields_of(Kinds, dates={"kind": "%Y"})
    with pytest.raises(TypeError, match="polars cannot compile"):

        class Lookaround(Csv):
            row = Kinds
            labels = {"Kind": "kind"}
            long = Long(name="day", value="kind", names=r"(?=x)")

    with pytest.raises(TypeError, match="no field 'when'"):

        class BadKey(Csv):
            row = Kinds
            labels = {"Kind": "kind", "Day": "day"}
            key = ("when",)


# --- delimited ---


class Pair(msgspec.Struct, frozen=True):
    a: str
    b: str


class Pairs(Csv):
    row = Pair
    labels = {"A": "a", "B": "b"}


def test_delimited_settings_are_checked() -> None:
    with pytest.raises(TypeError, match="not one byte"):

        class Wide(Pairs):
            separator = "||"

    with pytest.raises(TypeError, match="no group 'when'"):

        class NoGroup(Pairs):
            trailer = Line(r"END", carry={"when": "a"})


def test_delimited_refusals() -> None:
    class Ascii(Pairs):
        encoding = "ascii"

    with pytest.raises(EncodingError):
        Ascii.frame(b"A,B\nx,\xe9\n")

    class Skipped(Pairs):
        skip_lines = 3

    with pytest.raises(UnknownLayoutError, match="ends before its skipped lines"):
        Skipped.frame(b"one\ntwo\n")

    class Stamped(Pairs):
        preamble = Line(r"#BOF")

    with pytest.raises(UnknownLayoutError, match="line 1 'oops' is not the file's preamble"):
        Stamped.frame(b"oops\nA,B\n1,2\n")

    class Trailing(Pairs):
        trailing_separator = True

    with pytest.raises(UnknownLayoutError, match="does not end with the separator"):
        Trailing.frame(b"A,B\n1,2,\n")
    with pytest.raises(UnknownLayoutError, match="line 3"):
        Pairs.frame(b'A,B\n1,2\n"open,2\n')


def test_mixed_line_endings_read_on_the_fast_path_and_a_lone_cr_on_the_fallback() -> None:
    body = b"A,B\r\n1,2\n3,4\r\n"
    assert Csv._fast(body.split(b"\n", 1)[1])
    assert Pairs.frame(body).rows() == [("1", "2"), ("3", "4")]
    body = b'A,B\n"x\ry",2\n'
    assert not Csv._fast(body.split(b"\n", 1)[1])
    assert Pairs.frame(body).rows() == [("x\ry", "2")]


def test_a_header_and_no_rows() -> None:
    with pytest.raises(EmptyBodyError, match="0 rows"):
        Pairs.frame(b"A,B\n")

    class Maybe(Pairs):
        min_rows = 0

    assert Maybe.frame(b"A,B\n").height == 0


def test_a_pipe_on_the_fallback_keeps_quotes_as_text() -> None:
    class Piped(Pipe):
        row = Pair
        labels = {"A": "a", "B": "b"}

    body = b'A|B\n"x"|y\x01\nz|w\n'
    assert Piped.frame(body).rows() == [('"x"', "y\x01"), ("z", "w")]


# --- records ---


class Amount(msgspec.Struct, frozen=True):
    amount: Decimal
    as_of: str | None


def test_json_settings_are_checked() -> None:
    with pytest.raises(TypeError, match="no variants"):

        class Varied(Json):
            row = Amount
            labels = {"amt": "amount"}
            variants = ({"amount": "amount"},)

    with pytest.raises(TypeError, match="no envelope"):

        class Bare(Json):
            row = Amount
            labels = {"amt": "amount"}
            envelope = {"as_of": "as_of"}

    class NoCursor(Json):
        row = Amount
        records = "data"
        labels = {"amt": "amount", "d": "as_of"}

    with pytest.raises(TypeError, match="no next cursor"):
        NoCursor.page(b'{"data": []}')


def test_a_json_number_keeps_its_decimal_digits_and_an_absent_envelope_is_none() -> None:
    class Amounts(Json):
        row = Amount
        records = "data"
        labels = {"amt": "amount"}
        envelope = {"meta.as_of": "as_of"}

    frame = Amounts.frame(b'{"data": [{"amt": 0.1000000000000000055511151231257827}]}')
    assert frame["amount"][0] == Decimal("0.1000000000000000055511151231257827")
    assert frame["as_of"][0] is None


# --- sheets ---


class Cell(msgspec.Struct, frozen=True):
    name: str
    value: float


class Cells(Sheet):
    row = Cell
    labels = {"Name": "name", "Value": "value"}


def test_sheet_refusals() -> None:
    with pytest.raises(TypeError, match="not written like 'A:G'"):

        class BadRange(Cells):
            columns = "1:3"

    with pytest.raises(CorruptArchiveError):
        Cells.frame(b"PK\x03\x04 not a workbook at all")

    class Every(Cells):
        sheet = re.compile(r"Data \d")
        every_sheet = Sheets("name")

    with pytest.raises(UnknownLayoutError, match="no sheet"):
        Every.frame(workbook({"Other": [["Name", "Value"], ["a", 1.0]]}))

    class Deep(Cells):
        header_row = 5

    with pytest.raises(UnknownLayoutError, match="no header in row 6"):
        Deep.frame(workbook({"S": [["Name", "Value"]]}))


class Named(msgspec.Struct, frozen=True):
    sheet: str
    value: float


def _not_a(name: str) -> bool:
    return name != "A"


def test_every_sheet_picks_by_name_and_callable() -> None:
    class ByName(Sheet):
        row = Named
        labels = {"Value": "value"}
        sheet = "B"
        every_sheet = Sheets("sheet")

    class ByCall(ByName):
        sheet = _not_a

    body = workbook({"A": [["Value"], [1.0]], "B": [["Value"], [2.0]]})
    assert ByName.frame(body).rows() == [("B", 2.0)]
    assert ByCall.frame(body).rows() == [("B", 2.0)]


# --- archives, fixed width, report, scaffold, the package ---


def test_a_damaged_member_raises() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("a.csv", b"A,B\n1,2\n")
    data = bytearray(buffer.getvalue())
    data[data.index(b"A,B")] = ord("X")  # the bytes no longer match the member's CRC

    class Zipped(Pairs):
        member = "a.csv"

    with pytest.raises(CorruptArchiveError, match=r"a\.csv"):
        Zipped.frame(bytes(data))


def test_fixed_width_refuses_bytes_that_are_not_utf8() -> None:
    class Fixed(FixedWidth):
        row = Pair
        columns = {"a": (1, 1), "b": (2, 2)}

    with pytest.raises(EncodingError):
        Fixed.frame(b"x\xe9\n")


def test_report_names_a_refusal_as_its_structure() -> None:
    class Refusing(Pairs):
        refusals = {b"Page Not Available": NotPublishedError}

    found = report(Refusing, b"Page Not Available")
    assert found.structure is not None
    assert "no file" in found.structure


def test_scaffold_reads_a_sheet_and_an_empty_column() -> None:
    body = workbook({"Data": [["Name", "Notes"], ["a", None], ["b", None]]})
    source = scaffold(body, format="sheet", name="Notes", sheet="Data")
    assert "notes: str | None" in source
    assert 'sheet = "Data"' in source


def test_the_cli_main_prints_the_source(tmp_path: object, capsys: pytest.CaptureFixture[str]) -> None:
    import pathlib  # noqa: PLC0415

    path = pathlib.Path(str(tmp_path)) / "my-rates_file.csv"
    path.write_bytes(b"A,B\n1,x\n")
    assert cli.main(["scaffold", str(path), "--format", "csv"]) == 0
    assert "class MyRatesFile(flatread.Csv):" in capsys.readouterr().out


def test_the_package_loads_sheets_lazily() -> None:
    assert flatread.Sheet is Sheet
    with pytest.raises(AttributeError, match="nothing"):
        _ = flatread.nothing
