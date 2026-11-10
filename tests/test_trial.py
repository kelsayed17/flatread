"""Faults the trial ports (Nasdaq Trader, IBKR, Treasury on their recorded bodies) found: each pins the fix."""

import datetime as dt
from decimal import Decimal

import msgspec
import pytest

from flatread.delimited import Csv, Line, Pipe
from flatread.errors import CellError, EmptyBodyError, UnknownLayoutError
from flatread.records import Json
from flatread.sheets import Sheet
from tests.xlsx import workbook

# --- variants that leave a field unfilled ---


class Threshold(msgspec.Struct, frozen=True):
    symbol: str
    rule: str | None


class Thresholds(Csv):
    row = Threshold
    labels = {"Symbol": "symbol", "Rule": "rule"}
    variants = ({"Symbol": "symbol"},)  # the 2005 file, before the column was added


def test_a_variant_leaving_a_nullable_field_unfilled_reads_it_as_null() -> None:
    assert Thresholds.frame(b"Symbol\nAAII\n").rows() == [("AAII", None)]
    assert Thresholds.frame(b"Symbol,Rule\nAAII,Y\n").rows() == [("AAII", "Y")]


def test_the_labels_may_leave_a_nullable_field_to_a_variant() -> None:
    class Later(Csv):
        row = Threshold
        labels = {"Symbol": "symbol"}
        variants = ({"Symbol": "symbol", "Rule": "rule"},)

    assert Later.frame(b"Symbol,Rule\nA,Y\n").rows() == [("A", "Y")]


def test_a_variant_leaving_a_required_field_unfilled_is_refused_at_build() -> None:
    with pytest.raises(TypeError, match=r"variant 1 .*'symbol'"):

        class Bad(Csv):
            row = Threshold
            labels = {"Symbol": "symbol", "Rule": "rule"}
            variants = ({"Rule": "rule"},)


# --- decimals and JSON ---


class Money(msgspec.Struct, frozen=True):
    b: Decimal


def test_a_decimal_past_38_digits_is_a_cell_error() -> None:
    class Amounts(Json):
        row = Money
        labels = {"b": "b"}

    with pytest.raises(CellError, match="record 1"):
        Amounts.frame(b'[{"b": "0.' + b"1" * 39 + b'"}]')


class Note(msgspec.Struct, frozen=True):
    code: str
    amount: Decimal | None


class Notes(Json):
    row = Note
    records = "data"
    labels = {"code": "code", "amount": "amount"}
    missing = frozenset({"", "null"})  # text mode: every value a string, as Treasury writes them
    min_rows = 0


def test_a_null_records_list_raises() -> None:
    with pytest.raises(UnknownLayoutError, match="data"):
        Notes.frame(b'{"data": null}')


@pytest.mark.parametrize("record", [b'{"code": true, "amount": "1"}', b'{"code": "A", "amount": 1.5}'])
def test_text_mode_refuses_a_value_that_is_not_a_string(record: bytes) -> None:
    with pytest.raises(UnknownLayoutError, match="str"):
        Notes.frame(b'{"data": [' + record + b"]}")


def test_a_text_field_refuses_a_json_number_in_native_mode() -> None:
    class Codes(Json):
        row = Note
        labels = {"code": "code", "amount": "amount"}

    assert Codes.frame(b'[{"code": "A", "amount": 1.5}]').rows() == [("A", Decimal("1.5"))]
    with pytest.raises(UnknownLayoutError, match="str"):
        Codes.frame(b'[{"code": 7, "amount": 1.5}]')


# --- carried values and empty bodies ---


class Short(msgspec.Struct, frozen=True):
    symbol: str
    as_of: dt.datetime


class Shortable(Pipe):
    row = Short
    labels = {"#SYM": "symbol"}
    preamble = Line(r"#BOF\|(?P<as_of>.*)", carry={"as_of": "as_of"})
    trailer = Line(r"#EOF\|(?P<count>\d+)", count="count")
    dates = {"as_of": "%Y.%m.%d|%H:%M:%S"}
    min_rows = 0


def test_a_carried_value_is_typed_on_a_file_with_no_rows() -> None:
    assert Shortable.frame(b"#BOF|2026.11.10|05:21:33\n#SYM\n#EOF|0\n").height == 0
    with pytest.raises(CellError, match=r"outside the table.*25:21:33"):
        Shortable.frame(b"#BOF|2026.11.10|25:21:33\n#SYM\n#EOF|0\n")


def test_a_bad_carried_value_is_not_blamed_on_a_row() -> None:
    with pytest.raises(CellError, match="outside the table") as caught:
        Shortable.frame(b"#BOF|2026.02.30|05:21:33\n#SYM\nAAPL\n#EOF|1\n")
    assert "line" not in str(caught.value)


@pytest.mark.parametrize("body", [b"", b" \r\n"])
def test_an_empty_body_raises_even_where_a_table_may_have_no_rows(body: bytes) -> None:
    with pytest.raises(EmptyBodyError):
        Shortable.frame(body)


# --- line endings, quotes, times ---


class Pair(msgspec.Struct, frozen=True):
    a: str
    b: str


def test_final_line_ending_false_refuses_one() -> None:
    class Never(Csv):
        row = Pair
        labels = {"A": "a", "B": "b"}
        final_line_ending = False

    class Either(Never):
        final_line_ending = None

    assert Never.frame(b"A,B\n1,2").rows() == [("1", "2")]
    with pytest.raises(UnknownLayoutError, match="line ending"):
        Never.frame(b"A,B\n1,2\n")
    assert Either.frame(b"A,B\n1,2\n").rows() == Either.frame(b"A,B\n1,2").rows() == [("1", "2")]


def test_an_unquoted_format_skips_the_quote_scan() -> None:
    class Piped(Pipe):
        row = Pair
        labels = {"A": "a", "B": "b"}

    assert Piped._fast(b'x"|y\n')
    assert Piped.frame(b'A|B\nx"|y\n').rows() == [('x"', "y")]


class Halt(msgspec.Struct, frozen=True):
    at: dt.time
    when: dt.time | None


def test_a_time_field_reads_iso_or_its_format() -> None:
    class Halts(Csv):
        row = Halt
        labels = {"At": "at", "When": "when"}
        dates = {"when": "%I:%M %p"}

    assert Halts.frame(b"At,When\n14:30:00,02:30 PM\n09:05:01,\n").rows() == [
        (dt.time(14, 30), dt.time(14, 30)), (dt.time(9, 5, 1), None)
    ]  # fmt: skip
    with pytest.raises(CellError, match="a time written %H:%M:%S"):
        Halts.frame(b"At,When\n25:00:00,\n")


# --- sheets ---


class Cell(msgspec.Struct, frozen=True):
    name: str
    value: float


def test_a_sheet_header_on_the_wrong_row_names_the_header() -> None:
    class Cells(Sheet):
        row = Cell
        labels = {"Name": "name", "Value": "value"}

    body = workbook({"S": [["Title"], ["Name", "Value"], ["a", 1.0]]})
    with pytest.raises(UnknownLayoutError, match="the header is"):
        Cells.frame(body)
