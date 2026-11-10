"""Bodies the final review found read wrong or raised the wrong error: each pins what a reader gets now."""

import datetime as dt
from decimal import Decimal
from typing import override

import msgspec
import polars as pl
import pytest

from flatread.core import Erratum
from flatread.delimited import Csv
from flatread.errors import CellError, RaggedRowError, ReadError, UnknownLayoutError
from flatread.fixed import FixedWidth
from flatread.records import Json
from flatread.sheets import Sheet, Sheets
from tests.xlsx import Error, workbook

# --- sheets ---


class Mixed(msgspec.Struct, frozen=True):
    code: str
    value: float | None


class MixedSheet(Sheet):
    row = Mixed
    labels = {"Code": "code", "Value": "value"}
    missing = frozenset({"", "."})  # a token: the two-load fallback


def test_the_fallback_keeps_text_cells_as_written() -> None:
    body = workbook({"S": [["Code", "Value"], ["1.50", 1.0], ["1e5", "."], ["007.10", 2.0], [7.0, 3.0]]})
    assert MixedSheet.frame(body)["code"].to_list() == ["1.50", "1e5", "007.10", "7"]


def test_the_fallback_keeps_tiny_numbers() -> None:
    body = workbook({"S": [["Code", "Value"], ["a", 1e-12], ["b", "."], ["c", -3e-10]]})
    assert MixedSheet.frame(body)["value"].to_list() == [1e-12, None, -3e-10]


class Sized(msgspec.Struct, frozen=True):
    sheet: str
    amount: Decimal


def test_every_sheet_keeps_each_sheets_decimal_digits() -> None:
    class Amounts(Sheet):
        row = Sized
        labels = {"Amount": "amount"}
        every_sheet = Sheets("sheet")

    body = workbook({"a": [["Amount"], [1.5]], "b": [["Amount"], [1.25]]})
    assert Amounts.frame(body)["amount"].to_list() == [Decimal("1.5"), Decimal("1.25")]


@pytest.mark.parametrize("missing", [frozenset({""}), frozenset({"", "."})])
def test_an_excel_error_cell_raises(missing: frozenset[str]) -> None:
    errors = type("Errors", (Sheet,), {"row": Mixed, "labels": {"Code": "code", "Value": "value"}, "missing": missing})
    body = workbook({"S": [["Code", "Value"], ["a", 1.0], ["b", Error("#N/A")]]})
    with pytest.raises(CellError, match=r"row 3.*'#N/A'"):
        errors.frame(body)


def test_an_error_cell_outside_the_table_is_no_fault() -> None:
    body = workbook({"S": [["Code", "Value", None, Error("#REF!")], ["a", 1.0]]})
    assert MixedSheet.frame(body).height == 1


def test_an_erratum_and_a_cells_hook_see_text_on_an_all_number_sheet() -> None:
    class Fixed(Sheet):
        row = Mixed
        labels = {"Code": "code", "Value": "value"}
        errata = (Erratum("Value", "1.5", "2.5", "a typo"),)

        @classmethod
        @override
        def cells(cls, frame: pl.DataFrame) -> pl.DataFrame:
            return frame.with_columns(pl.col("Code").str.replace("x", "y"))

    body = workbook({"S": [["Code", "Value"], [1.0, 1.5], [2.0, 3.0]]})
    assert Fixed.frame(body).rows() == [("1", 2.5), ("2", 3.0)]


def test_a_filled_column_past_the_header_raises() -> None:
    body = workbook({"S": [["Code", "Value"], ["a", 1.0, "surprise"]]})
    with pytest.raises(RaggedRowError, match="column C"):
        MixedSheet.frame(body)


class Whole(msgspec.Struct, frozen=True):
    n: int | None


def test_a_huge_float_in_an_int_field_is_a_cell_error() -> None:
    class Ints(Sheet):
        row = Whole
        labels = {"N": "n"}

    class MixedInts(Ints):
        missing = frozenset({"", "."})

    body = workbook({"S": [["N"], [1e20], [1.0]]})
    with pytest.raises(CellError):
        Ints.frame(body)
    with pytest.raises(CellError):
        MixedInts.frame(workbook({"S": [["N"], [1e20], ["."]]}))


# --- JSON ---


class Amount(msgspec.Struct, frozen=True):
    n: int


class Paged(Json):
    row = Amount
    records = "data"
    labels = {"n": "n"}
    next = "next"
    min_rows = 0


def test_a_json_body_without_its_records_key_raises() -> None:
    with pytest.raises(UnknownLayoutError, match="data"):
        Paged.page(b'{"error": "rate limited"}')
    assert Paged.page(b'{"data": null}')[0].height == 0


def test_a_json_int_past_int64_is_a_read_error() -> None:
    with pytest.raises(ReadError):
        Paged.frame(b'{"data": [{"n": 99999999999999999999}]}')


# --- delimited and fixed width ---


def test_an_unclosed_quote_in_the_header_is_a_read_error() -> None:
    class Pairs(Csv):
        row = Amount
        labels = {"n": "n"}

    with pytest.raises(UnknownLayoutError, match="header"):
        Pairs.frame(b'"n,b\n1,2\n')


def test_a_short_fixed_width_line_raises() -> None:
    class Fixed(FixedWidth):
        row = Mixed
        columns = {"code": (1, 3), "value": (4, 6)}

    assert Fixed.frame(b"abc1.5\n").rows() == [("abc", 1.5)]
    with pytest.raises(RaggedRowError, match="line 2"):
        Fixed.frame(b"abc1.5\nab\n")


def test_dates_stay_dates() -> None:  # a guard on the fallback's datetime branch
    class Day(msgspec.Struct, frozen=True):
        day: dt.date | None

    class Days(Sheet):
        row = Day
        labels = {"Day": "day"}
        missing = frozenset({"", "."})

    assert Days.frame(workbook({"S": [["Day"], ["2026-11-09"], ["."]]}))["day"].to_list() == [
        dt.date(2026, 11, 9),
        None,
    ]
