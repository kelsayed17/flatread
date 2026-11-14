"""Hostile and misleading workbooks: each refusal a ``ReadError`` naming the cell, nothing read as another value."""

import datetime as dt
import io
import re
import struct
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import msgspec
import pytest
from xlrd import compdoc

from flatread.archives import ZippedFile, ZippedOrBare
from flatread.errors import ReadError
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
