"""Tests for ``flatread.cells``: a column declared once, and every cell read as its value, as missing, or raising.

What is pinned here is the rule no single wire shows whole: each type's shape, the three ends a cell may come to, the
cell an error names, and the declarations that fail before any cell is read.
"""

import datetime as dt
from typing import Annotated, NewType

import msgspec
import polars as pl
import pytest
from flatread.cells import Column, Format, UtcDatetime, column_specs, read_cells, read_labelled_cells
from flatread.delimited import wall_time
from flatread.errors import UnknownLayoutError

_Ticker = NewType("_Ticker", str)


class _Row(msgspec.Struct, frozen=True, gc=False):
    symbol: Annotated[_Ticker, Column("Symbol")]
    day: Annotated[dt.date, Column("Day", format="%m/%d/%Y")]
    count: Annotated[int | None, Column("Count", missing=("", "NA"))]
    price: Annotated[float | None, Column("Price", missing=(".",), strip=" ")]
    at: Annotated[UtcDatetime | None, Column("At", format="%Y-%m-%d %H:%M:%S", missing=("",))]


def _cells(*rows: tuple[str, ...]) -> pl.DataFrame:
    names = [spec.name for spec in column_specs(_Row)]
    return pl.DataFrame([dict(zip(names, row, strict=True)) for row in rows], schema=dict.fromkeys(names, pl.String))


def _read(*rows: tuple[str, ...]) -> pl.DataFrame:
    return read_cells(_cells(*rows), _Row, pl.Series(range(2, 2 + len(rows))))


def test_every_cell_reads_as_its_value_or_as_a_declared_missing_token() -> None:
    frame = _read(("AAA", "01/02/2026", "12", " 1.5 ", "2026-01-02 03:04:05"), ("BBB", "12/31/2025", "NA", ".", ""))

    assert frame.rows() == [
        ("AAA", dt.date(2026, 1, 2), 12, 1.5, dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)),
        ("BBB", dt.date(2025, 12, 31), None, None, None),
    ]
    assert frame.schema == pl.Schema(
        {"symbol": pl.String, "day": pl.Date, "count": pl.Int64, "price": pl.Float64, "at": pl.Datetime("us", "UTC")}
    )


@pytest.mark.parametrize(
    ("row", "named"),
    [
        (("AAA", "1/2/2026", "1", "1", ""), r"the 'Day' cell '1/2/2026' is not a date as %m/%d/%Y$"),
        (("AAA", "02/30/2026", "1", "1", ""), r"the 'Day' cell '02/30/2026' is not a date as %m/%d/%Y$"),
        (("AAA", "01/02/2026", "1e3", "1", ""), r"the 'Count' cell '1e3' is not a whole number, nor one of"),
        (("AAA", "01/02/2026", "92233720368547758070", "1", ""), r"the 'Count' cell '92233720368547758070'"),
        (("AAA", "01/02/2026", "1", "n/a", ""), r"the 'Price' cell 'n/a' is not a number, nor one of \['.'\]$"),
        (("AAA", "01/02/2026", "1", "1", "2026-01-02"), r"the 'At' cell '2026-01-02' is not a datetime"),
    ],
)
def test_a_cell_that_is_neither_its_shape_nor_missing_fails_the_read(row: tuple[str, ...], named: str) -> None:
    with pytest.raises(UnknownLayoutError, match=r"^line 3: " + named):
        _read(("AAA", "01/02/2026", "1", "1", ""), row)


def test_a_required_field_has_no_missing_token() -> None:
    with pytest.raises(UnknownLayoutError, match=r"^line 2: the 'Day' cell '' is not a date as %m/%d/%Y$"):
        _read(("AAA", "", "1", "1", ""))


def test_a_str_cell_is_kept_exactly_as_written() -> None:
    assert _read(("  a b ", "01/02/2026", "1", "1", ""))["symbol"].to_list() == ["  a b "]


def test_an_optional_column_the_header_leaves_out_reads_none() -> None:
    class Row(msgspec.Struct, frozen=True, gc=False):
        a: Annotated[str, Column("A")]
        b: Annotated[float | None, Column("B", optional=True)]

    frame = read_cells(pl.DataFrame({"a": ["x"]}), Row, pl.Series([2]))

    assert frame.rows() == [("x", None)]


def test_a_column_may_give_its_own_shape() -> None:
    class Row(msgspec.Struct, frozen=True, gc=False):
        day: Annotated[dt.date, Column("Day", format="%m/%d/%Y", pattern=r"[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}")]

    assert read_cells(pl.DataFrame({"day": ["1/2/2026"]}), Row, pl.Series([2]))["day"].to_list() == [
        dt.date(2026, 1, 2)
    ]


class _NoColumn(msgspec.Struct, frozen=True, gc=False):
    a: str


class _MissingOnRequired(msgspec.Struct, frozen=True, gc=False):
    a: Annotated[float, Column("A", missing=("",))]


class _NeverNone(msgspec.Struct, frozen=True, gc=False):
    a: Annotated[float | None, Column("A")]


class _DateWithoutFormat(msgspec.Struct, frozen=True, gc=False):
    a: Annotated[dt.date, Column("A")]


class _Unreadable(msgspec.Struct, frozen=True, gc=False):
    a: Annotated[bytes, Column("A")]


class _BoolWithoutTokens(msgspec.Struct, frozen=True, gc=False):
    a: Annotated[bool, Column("A")]


class _OffsetNotUtc(msgspec.Struct, frozen=True, gc=False):
    a: Annotated[dt.datetime, Column("A", format="%Y-%m-%d %H:%M:%S%#z")]


@pytest.mark.parametrize(
    ("row_type", "error", "named"),
    [
        (_NoColumn, TypeError, "declares no Column"),
        (_MissingOnRequired, ValueError, "cannot be None"),
        (_NeverNone, ValueError, "so it never is"),
        (_DateWithoutFormat, ValueError, "with no format"),
        (_Unreadable, TypeError, "which no cell reads as"),
        (_BoolWithoutTokens, ValueError, "without both its true and false tokens"),
        (_OffsetNotUtc, ValueError, "reads an offset, so it is a UtcDatetime"),
    ],
)
def test_a_declaration_that_cannot_be_read_fails_before_any_cell(
    row_type: type[msgspec.Struct], error: type[Exception], named: str
) -> None:
    with pytest.raises(error, match=named):
        column_specs(row_type)


def _one(row_type: type[msgspec.Struct], *cells: str) -> list[object]:
    (spec,) = column_specs(row_type)
    frame = read_cells(pl.DataFrame({spec.name: list(cells)}), row_type, pl.Series(range(2, 2 + len(cells))))
    return list(frame[spec.name])


class _Flag(msgspec.Struct, frozen=True, gc=False):
    flag: Annotated[bool, Column("Flag", true="Y", false="N")]


def test_a_bool_is_exactly_its_true_or_false_token() -> None:
    assert _one(_Flag, "Y", "N") == [True, False]
    with pytest.raises(UnknownLayoutError, match=r"^line 2: the 'Flag' cell 'y' is not 'Y' or 'N'$"):
        _one(_Flag, "y")


class _TwoFormats(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Column("Day", format=("%Y-%m-%d", "%m/%d/%Y"))]


def test_a_date_in_either_of_its_formats_reads_and_any_other_fails() -> None:
    assert _one(_TwoFormats, "2026-01-02", "01/03/2026") == [dt.date(2026, 1, 2), dt.date(2026, 1, 3)]
    with pytest.raises(UnknownLayoutError, match=r"is not a date as %Y-%m-%d or %m/%d/%Y$"):
        _one(_TwoFormats, "2026/01/02")


class _YearZero(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Column("Day", format="%m/%d/%Y")]


def test_a_year_a_python_date_cannot_hold_is_not_a_date() -> None:
    with pytest.raises(UnknownLayoutError, match=r"the 'Day' cell '01/01/0000'"):
        _one(_YearZero, "01/01/0000")


class _Offset(msgspec.Struct, frozen=True, gc=False):
    at: Annotated[UtcDatetime, Column("At", format="%Y-%m-%d %H:%M:%S%.f%#z")]


def test_a_time_with_an_offset_and_an_optional_fraction_reads_in_utc() -> None:
    assert _one(_Offset, "2026-01-02 03:04:05+00", "2026-01-02 03:04:05.25-05") == [
        dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC),
        dt.datetime(2026, 1, 2, 8, 4, 5, 250000, tzinfo=dt.UTC),
    ]


class _LeadingPadding(msgspec.Struct, frozen=True, gc=False):
    rate: Annotated[float | None, Column("Rate", strip_start=" ", missing=("NA",))]


def test_a_start_strip_takes_padding_off_the_start_alone() -> None:
    assert _one(_LeadingPadding, "  1.5", "NA") == [1.5, None]
    with pytest.raises(UnknownLayoutError, match=r"the 'Rate' cell '1.5 ' is not a number"):
        _one(_LeadingPadding, "1.5 ")


class _Prefixed(msgspec.Struct, frozen=True, gc=False):
    series: Annotated[str, Column("Series", prefix="Z1/Z1/")]


def test_a_prefix_is_checked_and_taken_off() -> None:
    assert _one(_Prefixed, "Z1/Z1/FL1") == ["FL1"]
    with pytest.raises(UnknownLayoutError, match=r"the 'Series' cell 'FL1' is not text beginning 'Z1/Z1/'$"):
        _one(_Prefixed, "FL1")


class _OwnShape(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Column("Day", format=("%Y-%m-%d", Format("%m/%d/%Y", r"[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}")))]


def test_one_format_may_give_its_own_shape_beside_another() -> None:
    assert _one(_OwnShape, "2026-01-02", "3/26/2004", "03/26/2004") == [
        dt.date(2026, 1, 2),
        dt.date(2004, 3, 26),
        dt.date(2004, 3, 26),
    ]
    with pytest.raises(UnknownLayoutError, match=r"is not a date as %Y-%m-%d or %m/%d/%Y$"):
        _one(_OwnShape, "2026-1-02")


def test_a_cell_with_no_text_at_all_is_refused_unless_a_sheet_gave_it() -> None:
    """A null is no cell a file wrote, so it is refused; a sheet's or a table's empty cell is ``""``."""
    frame = pl.DataFrame({"a": [None, "1"]}, schema={"a": pl.String})

    class MayBeEmpty(msgspec.Struct, frozen=True, gc=False):
        a: Annotated[float | None, Column("A", missing=("",))]

    with pytest.raises(UnknownLayoutError, match=r"^line 2: the 'A' cell is absent"):
        read_cells(frame, MayBeEmpty, pl.Series([2, 3]))
    labelled = pl.DataFrame({"A": [None, "1"]}, schema={"A": pl.String})
    assert read_labelled_cells(labelled, MayBeEmpty, pl.Series([2, 3]))["a"].to_list() == [None, 1.0]


@pytest.mark.parametrize(
    ("cell", "named"),
    [
        ("2024-01-01 23:59:61", "a second of 61"),
        ("2024-01-01 24:00:00", "a 24th hour"),
        ("2024-01-01 23:60:00", "a 60th minute"),
        ("2024-13-01 00:00:00", "a 13th month"),
        ("2024-01-32 00:00:00", "a 32nd day"),
    ],
)
def test_a_time_out_of_range_is_refused_not_rolled_over(cell: str, named: str) -> None:
    class At(msgspec.Struct, frozen=True, gc=False):
        at: Annotated[dt.datetime, Column("At", format="%Y-%m-%d %H:%M:%S")]

    del named
    with pytest.raises(UnknownLayoutError, match="is not a datetime"):
        _one(At, cell)


def test_a_leap_second_is_the_next_minutes_first_as_a_stamps_is() -> None:
    """Second 60 reads as :func:`~flatread.delimited.wall_time` reads it: a date is its datetime's day."""

    class At(msgspec.Struct, frozen=True, gc=False):
        at: Annotated[dt.datetime, Column("At", format="%Y-%m-%d %H:%M:%S%.f")]

    class On(msgspec.Struct, frozen=True, gc=False):
        on: Annotated[dt.date, Column("On", format="%Y-%m-%d %H:%M:%S")]

    assert _one(At, "2016-12-31 23:59:60.5") == [dt.datetime(2017, 1, 1, 0, 0, 0, 500000)]
    assert _one(At, "2016-12-31 23:59:60") == [wall_time(2016, 12, 31, 23, 59, 60)]
    assert _one(On, "2016-12-31 23:59:60") == [dt.date(2017, 1, 1)]
    with pytest.raises(UnknownLayoutError, match="is not a datetime"):
        _one(At, "9999-12-31 23:59:60")


def test_an_offset_that_moves_a_time_past_the_years_a_datetime_holds_is_refused() -> None:
    with pytest.raises(UnknownLayoutError, match="is not a datetime"):
        _one(_Offset, "0001-01-01 00:00:00+01")
    with pytest.raises(UnknownLayoutError, match="is not a datetime"):
        _one(_Offset, "9999-12-31 23:00:00-01")


def test_a_fraction_of_a_second_finer_than_a_datetime_keeps_is_refused_not_truncated() -> None:
    assert _one(_Offset, "2026-01-02 03:04:05.123456+00") == [dt.datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=dt.UTC)]
    with pytest.raises(UnknownLayoutError, match="is not a datetime"):
        _one(_Offset, "2026-01-02 03:04:05.1234567+00")


class _Wide(msgspec.Struct, frozen=True, gc=False):
    n: Annotated[int, Column("N")]
    x: Annotated[float, Column("X")]


def test_a_whole_number_that_fits_reads_and_one_too_large_is_refused() -> None:
    frame = pl.DataFrame({"n": ["9223372036854775807"], "x": ["1"]})
    assert read_cells(frame, _Wide, pl.Series([2]))["n"].to_list() == [9223372036854775807]
    with pytest.raises(UnknownLayoutError, match="'N' cell '9223372036854775808' is not a whole number"):
        read_cells(pl.DataFrame({"n": ["9223372036854775808"], "x": ["1"]}), _Wide, pl.Series([2]))


@pytest.mark.parametrize("cell", ["1e400", "-1e400", "1e-400"])
def test_a_number_a_float_cannot_hold_is_refused_not_read_as_infinity_or_zero(cell: str) -> None:
    with pytest.raises(UnknownLayoutError, match="'X' cell"):
        read_cells(pl.DataFrame({"n": ["1"], "x": [cell]}), _Wide, pl.Series([2]))


class _Sheet(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Column(("Business Date", "Date"), format="%Y-%m-%d %H:%M:%S")]
    total: Annotated[float, Column("Total")]
    note: Annotated[str | None, Column("Note", missing=("",), optional=True)]


def test_a_labelled_table_reads_by_its_header_in_field_order() -> None:
    frame = pl.DataFrame({"Date": ["2026-01-02 00:00:00"], "Total": ["1.5"]})

    assert read_labelled_cells(frame, _Sheet, pl.Series([3])).rows() == [(dt.date(2026, 1, 2), 1.5, None)]


@pytest.mark.parametrize("labels", [["Total", "Date"], ["Date"], ["Date", "Total", "Note", "Extra"]])
def test_a_labelled_table_whose_header_is_not_the_columns_raises(labels: list[str]) -> None:
    frame = pl.DataFrame({label: ["x"] for label in labels})

    with pytest.raises(UnknownLayoutError, match="is not the columns"):
        read_labelled_cells(frame, _Sheet, pl.Series([3]))


class _Flow(msgspec.Struct, frozen=True, gc=False):
    flow: Annotated[float | None, Column("Flow", grouping=",", parens_negative=True, missing=("-",))]


def test_an_accounting_number_reads_with_its_grouping_and_its_parenthesized_negative() -> None:
    assert _one(_Flow, "1,234.5", "(95.1)", "12", "-", "(1,000)") == [1234.5, -95.1, 12.0, None, -1000.0]


@pytest.mark.parametrize("cell", ["1234.5", "1,23.5", "(95.1", "12,", "--1"])
def test_an_accounting_number_of_another_shape_fails_the_read(cell: str) -> None:
    with pytest.raises(UnknownLayoutError, match="is not a number"):
        _one(_Flow, cell)
