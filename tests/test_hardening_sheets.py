"""Hostile and misleading workbooks: each refusal a ``ReadError`` naming the cell, nothing read as another value."""

import datetime as dt
import io
import re
import struct
import typing
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import fastexcel
import msgspec
import polars as pl
import pytest
from xlrd import compdoc

from flatread import sheets
from flatread.archives import ZippedFile, ZippedOrBare
from flatread.core import Column  # noqa: TC001 (a hook's annotations, read at runtime)
from flatread.errors import ReadError
from flatread.fields import Cell as CellAt
from flatread.fields import Label
from flatread.sheets import (
    BesideSheet,
    BiffSheet,
    FoundSheet,
    NumberTextSheet,
    OnlySheet,
    PreambleSheet,
    SheetFile,
    SheetText,
    sheet_grid,
)
from tests.support.xlsx import Cell, workbook

if TYPE_CHECKING:
    from collections.abc import Callable


def _raw(*rows: str) -> bytes:
    """Return an xlsx of one sheet ``S`` whose ``sheetData`` is *rows*, raw XML (style 1 is a date's)."""
    book = workbook({"S": [["x"]]})
    data = f"<sheetData>{''.join(rows)}</sheetData>".encode()
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(book)) as archive, zipfile.ZipFile(out, "w") as patched:
        for name in archive.namelist():
            part = archive.read(name)
            if name == "xl/worksheets/sheet1.xml":
                part = re.sub(rb"<sheetData>.*</sheetData>", data, part)
            patched.writestr(name, part)
    return out.getvalue()


def _text(ref: str, text: str) -> str:
    return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'


def _day(ref: str, serial: float) -> str:
    return f'<c r="{ref}" s="1"><v>{serial!r}</v></c>'


def _zip(name: str, member: bytes) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr(name, member)
    return out.getvalue()


class _Day(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Label("Day")]


class _Count(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("Name")]
    count: Annotated[int | None, Label("Count")]


class _Share(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("Name")]
    share: Annotated[float | None, Label("Share")]


class _Note(msgspec.Struct, frozen=True, gc=False):
    note: Annotated[str | None, Label("Note")]


def _sheet(*cells: Cell, label: str = "Count") -> bytes:
    return workbook({"S": [["Name", label], *(["a", cell] for cell in cells)]})


# --- 1. A panic of calamine's is a ReadError, at its source ---

_PANICS = _raw(f'<row r="1">{_text("A1", "Day")}</row>', f'<row r="2">{_day("A2", -1e13)}</row>')


@pytest.mark.parametrize(
    "read",
    [
        pytest.param(lambda: SheetFile(_Day, sheet="S").rows(_PANICS), id="SheetFile one load"),
        pytest.param(lambda: SheetFile(_Day, sheet="S", null_values=["."]).rows(_PANICS), id="SheetFile two loads"),
        pytest.param(lambda: SheetText(_Note, sheet="S").rows(_PANICS), id="SheetText"),
        pytest.param(lambda: sheet_grid(_PANICS), id="sheet_grid"),
        pytest.param(lambda: FoundSheet(_Note, key="note").rows(_PANICS), id="FoundSheet"),
    ],
)
def test_a_cell_calamine_panics_on_is_a_read_error(read: Callable[[], object]) -> None:
    """A date cell of serial -1e13 makes calamine panic (``TimeDelta::milliseconds out of bounds``) in every load."""
    with pytest.raises(ReadError, match="TimeDelta"):
        read()


# --- 2. A text cell in a boolean field ---


class _Flagged(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("A")]
    held: Annotated[bool | None, Label("F")]


class _Held(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("A")]
    held: Annotated[bool, Label("F")]


@pytest.mark.parametrize("row", [_Flagged, _Held])
@pytest.mark.parametrize("word", ["true", "false"])
def test_a_text_cell_in_a_boolean_field_is_no_boolean_cell(row: type[msgspec.Struct], word: str) -> None:
    body = workbook({"S": [["A", "F"], ["x", True], ["y", word]]})
    with pytest.raises(ReadError, match=f"the 'F' cell '{word}' is not a boolean cell: row 3"):
        SheetFile(row, sheet="S").rows(body)


def test_a_column_of_text_booleans_is_no_boolean_cells() -> None:
    body = workbook({"S": [["A", "F"], ["x", "true"]]})
    with pytest.raises(ReadError, match="the 'F' cell 'true' is not a boolean cell: row 2"):
        SheetFile(_Flagged, sheet="S").rows(body)


# --- 3. A text cell in a number field ---


@pytest.mark.parametrize("text", ["1e3", "1.0", "+5", "05", "inf", "nan", "1E400", "Infinity"])
@pytest.mark.parametrize("number", [1, None], ids=["beside a number", "alone"])
def test_a_text_cell_its_float_load_reads_as_a_number_is_none_in_an_int_field(text: str, number: int | None) -> None:
    body = _sheet(*([number] if number is not None else []), text)
    row = 3 if number is not None else 2
    with pytest.raises(ReadError, match=f"the 'Count' cell '{re.escape(text)}' is not a whole number: row {row}"):
        SheetFile(_Count, sheet="S").rows(body)


@pytest.mark.parametrize("text", ["1e3", "2.50", ".5", "+2.5", "nan", "-inf"])
def test_a_text_cell_its_float_load_reads_as_a_number_is_none_in_a_float_field(text: str) -> None:
    with pytest.raises(ReadError, match=f"the 'Share' cell '{re.escape(text)}' is not a number: row 3"):
        SheetFile(_Share, sheet="S").rows(_sheet(1.5, text, label="Share"))


def test_number_cells_of_every_shape_fastexcel_writes_still_read() -> None:
    """A number cell's text: no exponent past 1e15, a negative zero, nine decimals rounded, a tiny number's ``0``."""
    numbers = [1e20, -0.0, 1 / 3, 1.5e-10, -2.5, 0.1 + 0.2]
    body = _sheet(*numbers, ".", label="Share")
    assert [row.share for row in SheetFile(_Share, sheet="S", null_values=["."]).rows(body)] == [
        *numbers,
        None,
    ]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("1e3", "is not a whole number"),
        ("1.0", "is not a whole number"),
        ("+5", "is not a whole number"),
        ("inf", "is not a whole number"),
        ("99999999999999999999", "is not a whole number of 64 bits"),
    ],
)
def test_a_number_text_sheet_reads_an_int_fields_text_strictly(text: str, message: str) -> None:
    with pytest.raises(ReadError, match=f"the 'Count' cell '{re.escape(text)}' {message}: row 3"):
        NumberTextSheet(_Count, sheet="S").rows(_sheet(1, text))


def test_a_number_text_sheet_reads_whole_numbers_and_decimals_written_as_text() -> None:
    assert NumberTextSheet(_Count, sheet="S").rows(_sheet(1, "-12", "007", "9007199254740993")) == [
        _Count("a", 1),
        _Count("a", -12),
        _Count("a", 7),
        _Count("a", 9007199254740993),
    ]
    shares = NumberTextSheet(_Share, sheet="S").rows(_sheet(0.6, "1.03", ".5", "-2", label="Share"))
    assert [row.share for row in shares] == [0.6, 1.03, 0.5, -2.0]


@pytest.mark.parametrize("text", ["1e3", "nan", "inf", "1,5"])
def test_a_number_text_sheet_reads_a_float_fields_text_as_a_decimal_only(text: str) -> None:
    with pytest.raises(ReadError, match=f"the 'Share' cell '{re.escape(text)}' is not a number: row 3"):
        NumberTextSheet(_Share, sheet="S").rows(_sheet(1.5, text, label="Share"))


def test_a_number_text_sheet_refuses_a_number_cell_that_is_no_whole_number() -> None:
    """A number cell of ``3.0000000004``, whose text is ``3``: its own number says it is no whole number."""
    with pytest.raises(ReadError, match="the 'Count' cell '3' is not a whole number: row 2"):
        NumberTextSheet(_Count, sheet="S").rows(_sheet(3.0000000004))


# --- 4. A whole number past 64 bits ---


@pytest.mark.parametrize("null_values", [[], ["."]], ids=["one load", "two loads"])
def test_a_number_cell_past_64_bits_is_no_int(null_values: list[str]) -> None:
    with pytest.raises(ReadError, match="the 'Count' cell '100000000000000000000' is not a whole number of 64 bits"):
        SheetFile(_Count, sheet="S", null_values=null_values).rows(_sheet(1, 1e20))


def test_the_last_whole_number_of_64_bits_reads() -> None:
    """2^63 - 1024, the largest float under 2^63, and -2^63 are whole numbers of 64 bits."""
    assert [row.count for row in SheetFile(_Count, sheet="S").rows(_sheet(2.0**63 - 1024, -(2.0**63)))] == [
        2**63 - 1024,
        -(2**63),
    ]


# --- 5. OnlySheet and PreambleSheet composed with a zip ---


class _Row(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("Name")]
    n: Annotated[int, Label("N")]


_BOOK = workbook({"Data": [["Name", "N"], ["a", 1], ["b", 2]]})
_PREAMBLE = workbook({"Data": [["title"], [], ["Name", "N"], ["a", 1]]})


class _ZippedOnly(ZippedFile[_Row], OnlySheet[_Row]):
    _member = re.compile(r".*\.xlsx")


class _ZippedPreamble(ZippedFile[_Row], PreambleSheet[_Row]):
    _member = re.compile(r".*\.xlsx")


class _MaybeZippedOnly(ZippedOrBare[_Row], OnlySheet[_Row]):
    _member = re.compile(r".*\.xlsx")


def test_a_sheet_found_in_each_body_reads_from_a_zip() -> None:
    assert _ZippedOnly(_Row).rows(_zip("a.xlsx", _BOOK)) == [_Row("a", 1), _Row("b", 2)]
    assert _MaybeZippedOnly(_Row).rows(_zip("a.xlsx", _BOOK)) == [_Row("a", 1), _Row("b", 2)]
    assert _ZippedPreamble(_Row, sheet="Data").rows(_zip("a.xlsx", _PREAMBLE)) == [_Row("a", 1)]


def test_a_preamble_sheets_checks_name_each_bodys_own_rows() -> None:
    """One shared reader, two bodies whose headers stand on rows 3 and 1: an empty cell names its sheet row."""
    reader = PreambleSheet(_Row, sheet="Data")
    deep = workbook({"Data": [["title"], [], ["Name", "N"], ["a", 1], ["b", None]]})
    shallow = workbook({"Data": [["Name", "N"], ["b", None]]})
    for body, row in [(deep, 5), (shallow, 2), (deep, 5)]:
        with pytest.raises(ReadError, match=rf"\['N'\] columns have an empty cell.*row {row}"):
            reader.rows(body)
    with pytest.raises(ReadError, match=r"\['N'\] columns have an empty cell.*row 5"):
        _ZippedPreamble(_Row, sheet="Data").rows(_zip("a.xlsx", deep))


# --- 7. A wide sheet's columns are checked as a set ---


class _Wide(BesideSheet[_Row]):
    _columns_at = (0, 16_383)


def test_a_table_column_past_the_sheets_is_refused() -> None:
    with pytest.raises(ReadError, match=r"has no column \[0, 16383\] of its table"):
        _Wide(_Row, sheet="Data").rows(_BOOK)


# --- 8 and 9. BIFF cells xlrd fails on, and Excel's day 60 ---


def _record(kind: int, body: bytes) -> bytes:
    return struct.pack("<HH", kind, len(body)) + body


def _biff3(*cells: bytes) -> bytes:
    """Return a BIFF3 worksheet: a header ``When`` in A1, then *cells*; XF 1 is a date's."""
    formats = b"".join(_record(0x001E, bytes([len(f)]) + f.encode()) for f in ("General", "mm/dd/yyyy"))
    xfs = b"".join(_record(0x0243, bytes([0, f]) + bytes(10)) for f in (0, 1))
    header = _record(0x0204, struct.pack("<HHHH", 0, 0, 0, 4) + b"When")
    bof = _record(0x0209, struct.pack("<HHH", 0, 0x10, 0))
    return bof + formats + xfs + header + b"".join(cells) + _record(0x000A, b"")


def _biff_date(serial: float) -> bytes:
    return _record(0x0203, struct.pack("<HHHd", 1, 0, 1, serial))


class _When(msgspec.Struct, frozen=True, gc=False):
    when: Annotated[str, Label("When")]


@pytest.mark.parametrize(
    ("cell", "message"),
    [
        pytest.param(_biff_date(1e20), "the cell A2 is a date cell of no date: its serial is 1e[+]20", id="1e20"),
        pytest.param(_biff_date(-1e6), "the cell A2 is a date cell of no date", id="-1e6"),
        pytest.param(_biff_date(float("inf")), "the cell A2 is a date cell of no date", id="inf"),
        pytest.param(_biff_date(float("nan")), "the cell A2 is a date cell of no date", id="nan"),
        pytest.param(_biff_date(60.0), "the cell A2 is Excel's day 60, 1900-02-29", id="day 60"),
        pytest.param(_biff_date(60.75), "the cell A2 is Excel's day 60, 1900-02-29", id="day 60 at 18:00"),
        pytest.param(
            _record(0x0205, struct.pack("<HHHBB", 1, 0, 0, 0x99, 1)),
            "the cell A2 is an error cell of a code none of Excel's: 0x99",
            id="error code",
        ),
    ],
)
def test_a_biff_cell_xlrd_cannot_read_is_named(cell: bytes, message: str) -> None:
    with pytest.raises(ReadError, match=message):
        BiffSheet(_When).rows(_biff3(cell))


def test_biff_days_around_excels_day_60_read() -> None:
    assert BiffSheet(_When).rows(_biff3(_biff_date(59.0))) == [_When("1900-02-28 00:00:00")]
    assert BiffSheet(_When).rows(_biff3(_biff_date(61.0))) == [_When("1900-03-01 00:00:00")]


class _Moment(msgspec.Struct, frozen=True, gc=False):
    at: Annotated[dt.datetime, Label("Day")]


@pytest.mark.parametrize("null_values", [[], ["."]], ids=["one load", "two loads"])
@pytest.mark.parametrize(("row", "serial"), [(_Day, 60.0), (_Moment, 60.5)], ids=["date", "datetime"])
def test_a_date_cell_on_excels_day_60_is_refused(
    null_values: list[str], row: type[msgspec.Struct], serial: float
) -> None:
    body = _raw(
        f'<row r="1">{_text("A1", "Day")}</row>',
        f'<row r="2">{_day("A2", 59.0)}</row>',
        f'<row r="3">{_day("A3", serial)}</row>',
    )
    with pytest.raises(ReadError, match="the 'Day' cell on row 3 is Excel's day 60, 1900-02-29"):
        SheetFile(row, sheet="S", null_values=null_values).rows(body)


def test_days_59_and_61_read_as_their_dates() -> None:
    body = _raw(f'<row r="1">{_text("A1", "Day")}</row>', f'<row r="2">{_day("A2", 59.0)}</row>')
    assert SheetFile(_Day, sheet="S").rows(body) == [_Day(dt.date(1900, 2, 28))]
    body = _raw(f'<row r="1">{_text("A1", "Day")}</row>', f'<row r="2">{_day("A2", 61.0)}</row>')
    assert SheetFile(_Day, sheet="S").rows(body) == [_Day(dt.date(1900, 3, 1))]


# --- 10. A text field's number cell, every digit ---


class _Code(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("Name")]
    code: Annotated[str | None, Label("Code")]


@pytest.mark.parametrize(
    ("cell", "text"),
    [
        (0.1234567891234, "0.1234567891234"),
        (12345.6789012345, "12345.6789012345"),
        (1.5e-10, "1.5e-10"),
        (2.0, "2"),
        (1e20, "100000000000000000000"),
        ("0.10", "0.10"),
        ("1e3", "1e3"),
        ("0.1234567891234", "0.1234567891234"),
        (True, "true"),
    ],
)
def test_a_text_fields_cell_reads_with_every_digit_in_the_two_loads(cell: Cell, text: str) -> None:
    body = workbook({"S": [["Name", "Code"], ["a", cell], ["b", "."]]})
    assert SheetFile(_Code, sheet="S", null_values=["."]).rows(body) == [_Code("a", text), _Code("b", None)]


# --- 6. An Excel 97 sheet spanning past what calamine is let build ---


def _moved(row: int, column: int) -> bytes:
    """Return the tests' EIA workbook, its first sheet's first number cell moved to sheet *row* and *column*."""
    body = bytearray((Path(__file__).parent / "fixtures" / "eia_ngsstats.xls").read_bytes())
    stream = compdoc.CompDoc(bytes(body), logfile=io.StringIO()).get_named_stream("Workbook")
    at, sheets = 0, 0
    while True:
        kind, size = struct.unpack_from("<HH", stream, at)
        sheets += kind == 0x0809
        if sheets == 2 and kind in {0x00FD, 0x0203, 0x027E}:
            break
        at += 4 + size
    cell = stream[at : at + 4 + size]
    place = bytes(body).find(cell)
    body[place + 4 : place + 8] = struct.pack("<HH", row, column)
    return bytes(body)


class _Stat(msgspec.Struct, frozen=True, gc=False):
    date: Annotated[str | None, Label("Report Date")]


@pytest.mark.parametrize(
    ("row", "column", "span"),
    [(65_535, 255, "65535 rows by 256 columns"), (0, 65_535, "by 65536 columns"), (40_000, 254, "by 255 columns")],
)
def test_an_xls_sheet_spanning_past_the_cap_is_refused_before_a_load(row: int, column: int, span: str) -> None:
    with pytest.raises(ReadError, match=f"a sheet of the workbook spans .*{span}, past the 8388608 cells"):
        SheetText(_Stat, sheet="ngsstats 2026 (2021-2025)").rows(_moved(row, column))


def test_an_xls_sheet_spanning_under_the_cap_reads() -> None:
    sheet_grid(_moved(1_000, 30), "ngsstats 2026 (2021-2025)")


# --- 3, its rest. A text cell written as a number, and a word for none the strict load reads as none ---


@pytest.mark.parametrize("text", ["2", "2.5", "-3", "0"])
@pytest.mark.parametrize(
    ("cells", "row", "null_values"),
    [([1], 3, []), ([], 2, []), ([1], 3, ["."])],
    ids=["beside a number", "alone", "two loads"],
)
def test_a_text_cell_written_as_a_number_is_no_number_cell(
    text: str, cells: list[Cell], row: int, null_values: list[str]
) -> None:
    message = f"the 'Share' cell '{re.escape(text)}' is not a number cell: row {row}"
    with pytest.raises(ReadError, match=message):
        SheetFile(_Share, sheet="S", null_values=null_values).rows(_sheet(*cells, text, label="Share"))
    if text != "2.5":  # an int field's 2.5 is no whole number first
        with pytest.raises(ReadError, match=message.replace("Share", "Count")):
            SheetFile(_Count, sheet="S", null_values=null_values).rows(_sheet(*cells, text))


def test_a_number_text_sheet_reads_a_text_cell_written_as_a_number() -> None:
    assert [row.count for row in NumberTextSheet(_Count, sheet="S").rows(_sheet(1, "2", "-3"))] == [1, 2, -3]


def test_number_cells_beside_words_for_none_and_empty_cells_read() -> None:
    body = _sheet(1, ".", None, 0, label="Share")
    assert [row.share for row in SheetFile(_Share, sheet="S", null_values=["."]).rows(body)] == [1.0, None, None, 0.0]


class _StatedCount(msgspec.Struct, frozen=True, gc=False):
    count: Annotated[int, CellAt("B1")]
    name: Annotated[str, Label("Name")]


def test_a_number_cell_field_over_a_text_cell_written_as_a_number_is_refused() -> None:
    with pytest.raises(ReadError, match="the 'S' sheet's cell 'count' '7' is not a number cell"):
        SheetFile(_StatedCount, sheet="S", header_row=2).rows(workbook({"S": [[None, "7"], ["Name"], ["a"]]}))
    assert SheetFile(_StatedCount, sheet="S", header_row=2).rows(workbook({"S": [[None, 7], ["Name"], ["a"]]})) == [
        _StatedCount(7, "a")
    ]


class _Kinds(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str | None, Label("Name")]
    share: Annotated[float | None, Label("Share")]
    held: Annotated[bool | None, Label("Held")]
    day: Annotated[dt.date | None, Label("Day")]


_KIND_ROW: list[Cell] = ["a", 1.5, True, dt.date(2026, 1, 2)]


@pytest.mark.parametrize("word", ["NA", "null", "#N/A", "None", "n/a"])
@pytest.mark.parametrize(
    ("at", "label", "what"), [(1, "Share", "number"), (2, "Held", "boolean cell"), (3, "Day", "date cell")]
)
def test_a_word_for_none_the_strict_load_reads_as_none_is_a_text_cell(
    word: str, at: int, label: str, what: str
) -> None:
    second = list(_KIND_ROW)
    second[at] = word
    body = workbook({"S": [["Name", "Share", "Held", "Day"], _KIND_ROW, second]})
    with pytest.raises(ReadError, match=f"the '{label}' cell '{re.escape(word)}' is not a {what}: row 3"):
        SheetFile(_Kinds, sheet="S").rows(body)
    assert getattr(SheetFile(_Kinds, sheet="S", null_values=[word]).rows(body)[1], _Kinds.__struct_fields__[at]) is None


def test_a_text_fields_word_for_none_and_an_empty_cell_read_as_written() -> None:
    body = workbook({"S": [["Name", "Share", "Held", "Day"], _KIND_ROW, ["NA", None, None, None]]})
    assert SheetFile(_Kinds, sheet="S").rows(body)[1] == _Kinds("NA", None, None, None)


# --- 6, its rest. A sparse .xlsx refused before calamine builds its grid ---


def _parts(sheet: str, *, path: str = "xl/worksheets/sheet1.xml", target: str = "worksheets/sheet1.xml") -> bytes:
    """Return an xlsx of one sheet ``S``: worksheet XML *sheet*, its part at *path*, its relationship's *target*."""
    book = workbook({"S": [["x"]]})
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(book)) as archive, zipfile.ZipFile(out, "w") as patched:
        for member in archive.namelist():
            name, part = member, archive.read(member)
            if member == "xl/worksheets/sheet1.xml":
                name, part = path, sheet.encode()
            elif member == "xl/_rels/workbook.xml.rels":
                part = part.replace(b'Target="worksheets/sheet1.xml"', f'Target="{target}"'.encode())
            patched.writestr(name, part)
    return out.getvalue()


_NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'


def _data(*rows: str) -> str:
    return f'<?xml version="1.0"?><worksheet {_NS}><sheetData>{"".join(rows)}</sheetData></worksheet>'


def _loads(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    calls: list[object] = []
    load = fastexcel.ExcelReader.load_sheet
    monkeypatch.setattr(fastexcel.ExcelReader, "load_sheet", lambda *a, **k: calls.append(a) or load(*a, **k))
    return calls


_A1 = '<row r="1"><c r="A1" t="inlineStr"><is><t>x</t></is></c></row>'
_TEN = "".join('<c t="n"><v>1</v></c>' for _ in range(10))


@pytest.mark.parametrize(
    ("sheet", "span"),
    [
        pytest.param(_data(_A1, '<row r="2"><c r="J1000000"><v>1</v></c></row>'), "1000000 rows by 10", id="far"),
        pytest.param(_data(_A1, '<row r="2"><c r="ZZZZ1"><v>1</v></c></row>'), "1 rows by 475254", id="wide"),
        pytest.param(_data('<row r="2"><c r="J1000000"><v>1</v></c></row>', _A1), "1000000 rows by 10", id="unsorted"),
        pytest.param(_data(_A1, '<row r="2"><c t="n" r="J1000000"><v>1</v></c></row>'), "1000000", id="r after t"),
        pytest.param(_data(_A1, "<row r='2'><c r='J1000000'><v>1</v></c></row>"), "1000000", id="single quotes"),
        pytest.param(_data(_A1, '<row r="2"><c\n r = "J1000000"><v>1</v></c></row>'), "1000000", id="spaced"),
        pytest.param(_data(_A1, '<row r="2"><c r="j1000000"><v>1</v></c></row>'), "1000000", id="lower case"),
        pytest.param(_data(_A1, '<row r="2"><c r="A2" r="J1000000"><v>1</v></c></row>'), "1000000", id="a second r"),
        pytest.param(_data(_A1, '<row r="2"><c s=">" r="J1000000"><v>1</v></c></row>'), "1000000", id="> in a value"),
        pytest.param(_data(_A1, f'<row r="900000">{_TEN}</row>'), "900000 rows by 10", id="cells without r"),
        pytest.param(_data(_A1, '<row r="899999"/>', f"<row>{_TEN}</row>"), "900000 rows by 10", id="row after r"),
        pytest.param(_data(f"<row>{'<c><v>1</v></c>' * 16_385}</row>"), "1 rows by 16385 columns", id="a long row"),
        pytest.param(
            '<x:worksheet xmlns:x="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><x:sheetData>'
            '<x:row r="1"><x:c r="A1"><x:v>1</x:v></x:c><x:c r="J1000000"><x:v>1</x:v></x:c></x:row>'
            "</x:sheetData></x:worksheet>",
            "1000000 rows by 10",
            id="prefixed",
        ),
    ],
)
def test_a_sheet_spanning_past_the_cap_is_refused_before_a_load(
    sheet: str, span: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _loads(monkeypatch)
    with pytest.raises(ReadError, match=f"a sheet of the workbook spans {span}"):
        SheetFile(_Note, sheet="S").rows(_parts(sheet))
    assert calls == []


@pytest.mark.parametrize(
    ("path", "target"),
    [("elsewhere/sheet.xml", "/elsewhere/sheet.xml"), ("xl/Worksheets/SHEET1.xml", "worksheets/sheet1.xml")],
    ids=["from the root", "another case"],
)
def test_a_sheet_part_is_found_as_calamine_finds_it(path: str, target: str) -> None:
    sheet = _data(_A1, '<row r="2"><c r="J1000000"><v>1</v></c></row>')
    with pytest.raises(ReadError, match="spans 1000000 rows by 10 columns"):
        SheetFile(_Note, sheet="S").rows(_parts(sheet, path=path, target=target))


class _Wide2(msgspec.Struct, frozen=True, gc=False):
    note: Annotated[str | None, Label("x")]


@pytest.mark.parametrize(
    "sheet",
    [
        pytest.param(_data(_A1, '<row r="150000"><c r="A150000" t="inlineStr"><is><t>y</t></is></c></row>'), id="tall"),
        pytest.param(_data('<row r="1"><c r="A1" t="inlineStr"><is><t>x</t></is></c><c r="AD1"/></row>'), id="wide"),
        pytest.param(_data('<row><c t="inlineStr"><is><t>x</t></is></c></row>', "<row/>" * 3), id="without r"),
    ],
)
def test_a_sheet_spanning_under_the_cap_reads(sheet: str) -> None:
    assert sheet_grid(_parts(sheet))["0"][0] == "x"


def test_a_zip_that_will_not_open_is_refused() -> None:
    with pytest.raises(ReadError, match="the body is a workbook's zip that will not open"):
        SheetFile(_Note, sheet="S").rows(b"PK\x03\x04 cut")


class _Total(msgspec.Struct, frozen=True, gc=False):
    key: Annotated[str, Label("K")]
    total: Annotated[float | None, Label("Total")]


class _RequiredTotal(msgspec.Struct, frozen=True, gc=False):
    key: Annotated[str, Label("K")]
    total: Annotated[float, Label("Total")]


@pytest.mark.parametrize("row", [_Total, _RequiredTotal])
@pytest.mark.parametrize("word", ["n/a", "N/A", "NA", "null", "NULL", "None", "#N/A"])
def test_the_one_load_names_a_word_for_none_in_a_number_field(row: type[msgspec.Struct], word: str) -> None:
    """The strict load reads these words as none, as an empty cell; the reader declares no null_values."""
    body = workbook({"S": [["K", "Total"], ["a", 1.5], ["b", word]]})
    with pytest.raises(ReadError, match=f"the 'Total' cell '{re.escape(word)}' is not a number: row 3"):
        SheetFile(row, sheet="S").rows(body)


@pytest.mark.parametrize(
    ("sheet", "span"),
    [
        (_data(_A1, '<row r="2"><c r="J1000000"><v>1</v></c></row>'), "1000000 rows by 10"),
        (_data(_A1, f'<row r="900000">{_TEN}</row>'), "900000 rows by 10"),
        (_data(_A1, f'<row r="2"><c r="B2" t="inlineStr"><is><t>{"y" * 100}</t></is></c></row>'), None),
    ],
)
def test_a_sheet_part_is_scanned_in_chunks_no_tag_spans(
    sheet: str, span: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sheets, "_CHUNK", 16)
    if span is None:
        assert sheet_grid(_parts(sheet))["0"][0] == "x"
        return
    with pytest.raises(ReadError, match=f"spans {span}"):
        sheet_grid(_parts(sheet))


def test_an_error_not_fastexcels_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_: object, **__: object) -> None:
        raise RuntimeError

    monkeypatch.setattr(fastexcel, "read_excel", broken)
    with pytest.raises(RuntimeError):
        SheetFile(_Row, sheet="Data").rows(_BOOK)


def test_an_error_not_fastexcels_in_the_one_load_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_: object, **__: object) -> None:
        raise RuntimeError

    monkeypatch.setattr(fastexcel.ExcelReader, "load_sheet", broken)
    with pytest.raises(RuntimeError):
        SheetFile(_Row, sheet="Data").rows(_BOOK)


_XLS = (Path(__file__).parent / "fixtures" / "eia_ngsstats.xls").read_bytes()


@pytest.mark.parametrize(
    ("body", "message"),
    [
        pytest.param(_XLS[:8] + b"\x00" * 600, "not a readable Excel workbook", id="corrupt compound document"),
        pytest.param(
            _XLS.replace("Workbook".encode("utf-16-le"), "Xorkbook".encode("utf-16-le")),
            "not a workbook",
            id="no workbook stream",
        ),
        pytest.param(_zip("a.txt", b"x"), "no .xlsx workbook", id="a zip of no workbook"),
    ],
)
def test_a_body_whose_span_cannot_be_read_is_refused(body: bytes, message: str) -> None:
    with pytest.raises(ReadError, match=message):
        sheet_grid(body)


def test_found_sheet_refuses_a_workbook_of_no_sheet() -> None:
    body = _parts(_data(_A1))
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(body)) as archive, zipfile.ZipFile(out, "w") as patched:
        for name in archive.namelist():
            part = archive.read(name)
            patched.writestr(
                name, re.sub(rb"<sheets>.*</sheets>", b"<sheets/>", part) if name == "xl/workbook.xml" else part
            )
    with pytest.raises(ReadError, match="no sheet"):
        FoundSheet(_Note, key="note").rows(out.getvalue())


def test_a_preamble_sheet_read_outside_frame_places_its_own_copy() -> None:
    reader = PreambleSheet(_Row, sheet="Data")
    assert reader._read(_PREAMBLE)["name"].to_list() == ["a"]
    assert reader._row_one == 2  # the shared reader keeps its own rows


@pytest.mark.parametrize(
    "parts",
    [
        {"content.xml": b"<office:document-content/>", "mimetype": b"application/vnd.oasis.opendocument.spreadsheet"},
        {"xl/workbook.bin": b"\x83\x01\x00", "[Content_Types].xml": b"<Types/>"},
        {"x.csv": b"a\n1\n"},
    ],
    ids=["ods", "xlsb", "a zip of no workbook"],
)
def test_a_zip_that_is_no_xlsx_is_refused_before_a_load(parts: dict[str, bytes]) -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    with pytest.raises(
        ReadError, match=r"^the body is a zip but no \.xlsx workbook \(an \.xlsb or \.ods is not read\)"
    ):
        SheetFile(_Count, sheet="S").rows(out.getvalue())


class _Starred(SheetFile[_Count]):
    """A format whose own _value reads a count written as text with a footnote star (``-1,249,056*``)."""

    _number_text = ("count",)

    @typing.override
    def _value(self, column: Column, text: pl.Series, typed: pl.Series) -> pl.Series:
        if column.field != "count":
            return super()._value(column, text, typed)
        digits = text.str.strip_suffix("*").str.replace_all(",", "")
        return super()._value(column, digits, typed.fill_null(digits.cast(pl.Float64, strict=False)))


def test_a_format_names_the_number_fields_its_own_value_reads_from_text() -> None:
    body = workbook({"S": [["Name", "Count"], ["a", 1], ["b", "-1,249,056*"]]})
    assert _Starred(_Count, sheet="S").rows(body) == [_Count("a", 1), _Count("b", -1249056)]
    with pytest.raises(ReadError, match=r"^the 'Count' cell '-1,249,056\*' is not a whole number: row 3$"):
        SheetFile(_Count, sheet="S").rows(body)
