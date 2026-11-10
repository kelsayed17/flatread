import datetime as dt
from decimal import Decimal
from typing import Any, Literal

import msgspec
import polars as pl
import pytest

from flatread.cells import EXCEL_SERIAL, QUARTER, YEAR_MONTH, fields_of, first_bad, type_frame
from flatread.errors import CellError


def typed(
    row: type[msgspec.Struct],
    columns: dict[str, list[Any]],
    **settings: Any,  # noqa: ANN401  # each setting fields_of takes
) -> tuple[pl.DataFrame, pl.DataFrame]:
    fields = fields_of(row, **settings)
    return type_frame(pl.DataFrame(columns), fields)


def bad_rows(bad: pl.DataFrame, name: str) -> list[int]:
    return [int(i) for i in bad[name].arg_true()]


class Basic(msgspec.Struct):
    n: int
    x: float
    s: str
    b: bool


def test_int_float_str_bool_read_strictly() -> None:
    frame, bad = typed(Basic, {"n": ["7", "7.5"], "x": ["1.25", "2"], "s": ["a", "b"], "b": ["TRUE", "false"]})
    assert frame.row(0) == (7, 1.25, "a", True)
    assert frame["b"].to_list() == [True, False]
    assert bad_rows(bad, "n") == [1]
    assert not bad.drop("n").select(pl.any_horizontal(pl.all())).to_series().any()


class Optional(msgspec.Struct):
    x: float | None
    y: float


def test_missing_tokens_become_null_only_where_nullable() -> None:
    frame, bad = typed(Optional, {"x": [".", "1"], "y": [".", "2"]}, missing=frozenset({"", "."}))
    assert frame["x"].to_list() == [None, 1.0]
    assert bad_rows(bad, "x") == []
    assert bad_rows(bad, "y") == [0]


def test_missing_by_field_adds_tokens_to_one_field() -> None:
    _, bad = typed(Optional, {"x": ["NA"], "y": ["1"]}, missing_by_field={"x": frozenset({"NA"})})
    assert bad_rows(bad, "x") == []


class Floats(msgspec.Struct):
    x: float


@pytest.mark.parametrize("text", ["nan", "NaN", "inf", "-inf"])
def test_nan_and_infinity_refused_unless_nonfinite(text: str) -> None:
    _, bad = typed(Floats, {"x": [text]})
    assert bad_rows(bad, "x") == [0]
    frame, bad = typed(Floats, {"x": [text]}, nonfinite=True)
    assert bad_rows(bad, "x") == []
    assert frame["x"].is_nan().any() or frame["x"].is_infinite().any()


def test_an_overflowing_float_is_refused() -> None:
    _, bad = typed(Floats, {"x": ["1e400"]})
    assert bad_rows(bad, "x") == [0]


class Dates(msgspec.Struct):
    d: dt.date


def test_dates_write_back() -> None:
    _, bad = typed(Dates, {"d": ["12/31/25"]}, dates={"d": "%m/%d/%Y"})
    assert bad_rows(bad, "d") == [0]
    frame, bad = typed(Dates, {"d": ["4/2/1971", "12/31/2025"]}, dates={"d": ("%m/%d/%Y", "%-m/%-d/%Y")})
    assert frame["d"].to_list() == [dt.date(1971, 4, 2), dt.date(2025, 12, 31)]
    assert bad_rows(bad, "d") == []


def test_iso_default_dates() -> None:
    frame, bad = typed(Dates, {"d": ["2026-11-09", "2026-13-01", "20261109"]})
    assert frame["d"][0] == dt.date(2026, 11, 9)
    assert bad_rows(bad, "d") == [1, 2]


def test_quarter_year_month_and_serial() -> None:
    frame, bad = typed(Dates, {"d": ["2024Q1", "2024:Q3", "2024Q5"]}, dates={"d": QUARTER})
    assert frame["d"].to_list()[:2] == [dt.date(2024, 1, 1), dt.date(2024, 7, 1)]
    assert bad_rows(bad, "d") == [2]
    frame, bad = typed(Dates, {"d": ["2026-09", "2026-13"]}, dates={"d": YEAR_MONTH})
    assert frame["d"][0] == dt.date(2026, 9, 1)
    assert bad_rows(bad, "d") == [1]
    frame, bad = typed(Dates, {"d": ["45000", "4.5"]}, dates={"d": EXCEL_SERIAL})
    assert frame["d"][0] == dt.date(2023, 3, 15)
    assert bad_rows(bad, "d") == [1]


class Stamp(msgspec.Struct):
    t: dt.datetime


def test_datetimes_read_iso_by_default() -> None:
    frame, bad = typed(Stamp, {"t": ["2026-11-09T14:30:00", "2026-11-09 14:30:00", "14:30"]})
    assert frame["t"][0] == dt.datetime(2026, 11, 9, 14, 30)
    assert bad_rows(bad, "t") == [2]


class Money(msgspec.Struct):
    m: Decimal


def test_decimal_keeps_every_digit() -> None:
    frame, bad = typed(Money, {"m": ["1.5", "2.123456789012", "-0.25", "7"]})
    assert frame["m"].to_list() == [Decimal("1.5"), Decimal("2.123456789012"), Decimal("-0.25"), Decimal(7)]
    assert frame["m"].dtype == pl.Decimal(38, 12)
    assert bad_rows(bad, "m") == []


def test_decimal_refuses_what_does_not_write_back() -> None:
    _, bad = typed(Money, {"m": ["1e3", "abc", "1.5"]})
    assert bad_rows(bad, "m") == [0, 1]


class Kind(msgspec.Struct):
    k: Literal["US", "State", "CBSA"]


def test_literal_is_an_enum_and_refuses_others() -> None:
    frame, bad = typed(Kind, {"k": ["US", "CBSA", "County"]})
    assert frame["k"].dtype == pl.Enum(["US", "State", "CBSA"])
    assert frame["k"].to_list()[:2] == ["US", "CBSA"]
    assert bad_rows(bad, "k") == [2]


def test_strip_takes_whitespace_off_first() -> None:
    frame, bad = typed(Optional, {"x": [" 1.03", "   "], "y": [" 2 ", "3"]}, strip=True)
    assert frame["x"].to_list() == [1.03, None]
    assert bad_rows(bad, "x") == []
    _, bad = typed(Floats, {"x": [" 1.03"]})
    assert bad_rows(bad, "x") == [0]


class Counts(msgspec.Struct):
    n: int
    x: float


def test_an_already_typed_column_is_checked_not_cast() -> None:
    frame, bad = type_frame(pl.DataFrame({"n": [3.0, 3.5], "x": [1.0, float("nan")]}), fields_of(Counts))
    assert frame["n"].dtype == pl.Int64
    assert frame["n"][0] == 3
    assert bad_rows(bad, "n") == [1]
    assert bad_rows(bad, "x") == [1]


def test_unsupported_field_type_raises_at_build() -> None:
    class Bad(msgspec.Struct):
        xs: list[int]

    with pytest.raises(TypeError, match="xs"):
        fields_of(Bad)


def test_first_bad_names_the_first_failing_cell() -> None:
    text = pl.DataFrame({"n": ["1", "2", "x"], "x": ["1", "y", "3"]})
    fields = fields_of(Counts)
    _, bad = type_frame(text, fields)
    error = first_bad(text, bad, fields, {"n": "Count", "x": "Value"}, lambda i: f"line {i + 2}")
    assert isinstance(error, CellError)
    assert str(error) == "line 3: the 'Value' cell 'y' is not a number"
