"""Tests for ``flatread.cells``: a column declared once, and every cell read as its value, as missing, or raising.

What is pinned here is the rule no single wire shows whole: what each type reads (polars' leniencies included), the
three ends a cell may come to, the cell an error names, and the declarations that fail before any cell is read.
"""

import datetime as dt
import gc
import math
import re
import weakref
from typing import Annotated, Any, Literal

import msgspec
import polars as pl
import pytest

from flatread.cells import DECIMAL, NONFINITE, CellError, Column, UtcDatetime, column_specs, read_table, wire_names
from flatread.errors import UnknownLayoutError


def _one(row_type: type[msgspec.Struct], *cells: str) -> list[Any]:
    """Read *cells* as *row_type*'s one column, under its label."""
    (spec,) = column_specs(row_type)
    label = spec.labels[0]
    return read_table(pl.DataFrame({label: list(cells)}, schema={label: pl.String}), row_type)[label].to_list()


def _refused(row_type: type[msgspec.Struct], cell: str) -> None:
    with pytest.raises(CellError, match=f"^row 1: the '[^']+' cell {re.escape(repr(cell))} is not "):
        _one(row_type, cell)


class _Int(msgspec.Struct, frozen=True, gc=False):
    n: Annotated[int, Column("N")]


class _Float(msgspec.Struct, frozen=True, gc=False):
    x: Annotated[float, Column("X")]


def test_an_int_is_what_polars_reads_as_a_whole_number_that_fits() -> None:
    assert _one(_Int, "1", "-1", "+1", "007", "9223372036854775807") == [1, -1, 1, 7, 9223372036854775807]


@pytest.mark.parametrize("cell", ["", " 1", "1.0", "1e3", "0x1", "1_000", "9223372036854775808"])
def test_an_int_refuses_any_other_text(cell: str) -> None:
    _refused(_Int, cell)


def test_a_float_is_what_polars_casts() -> None:
    assert _one(_Float, "1", "1.5", ".5", "1.", "+4", "1e3", "1e-400") == [1.0, 1.5, 0.5, 1.0, 4.0, 1000.0, 0.0]


@pytest.mark.parametrize("cell", ["", " 1", "1,5", "0x1", "1.2.3", "1e400", "-1e400"])
def test_a_float_refuses_any_other_text(cell: str) -> None:
    _refused(_Float, cell)


@pytest.mark.parametrize("cell", ["NaN", "nan", "-nan", "inf", "-inf", "+Infinity", "INF"])
def test_a_float_refuses_a_nan_or_an_infinity_by_default(cell: str) -> None:
    _refused(_Float, cell)


class _Nonfinite(msgspec.Struct, frozen=True, gc=False):
    x: Annotated[float, Column("X", pattern=f"{DECIMAL}|{NONFINITE}")]


def test_a_float_whose_pattern_admits_them_reads_a_nan_or_an_infinity() -> None:
    nan, inf, minus_inf, one = _one(_Nonfinite, "NaN", "Infinity", "-inf", "1.5")
    assert math.isnan(nan)
    assert (inf, minus_inf, one) == (math.inf, -math.inf, 1.5)


def test_a_float_too_large_is_refused_even_where_infinities_are_admitted() -> None:
    _refused(_Nonfinite, "1e400")


class _Flag(msgspec.Struct, frozen=True, gc=False):
    flag: Annotated[bool, Column("Flag", true="Y", false="N")]


def test_a_bool_is_exactly_its_true_or_false_token() -> None:
    assert _one(_Flag, "Y", "N") == [True, False]
    for cell in ("y", "", "true"):
        _refused(_Flag, cell)


class _Side(msgspec.Struct, frozen=True, gc=False):
    side: Annotated[Literal["S", "B"], Column("Side")]


def test_a_literal_is_an_enum_of_its_values_and_refuses_any_other() -> None:
    frame = read_table(pl.DataFrame({"Side": ["B", "S"]}), _Side)
    assert frame.schema == pl.Schema({"Side": pl.Enum(["B", "S"])})
    assert frame["Side"].to_list() == ["B", "S"]
    for cell in ("C", "b"):
        _refused(_Side, cell)


class _Missing(msgspec.Struct, frozen=True, gc=False):
    x: Annotated[float | None, Column("X", missing=("", "NA", "."))]


def test_a_missing_token_is_none_and_only_a_declared_one_is() -> None:
    assert _one(_Missing, "1", "", "NA", ".") == [1.0, None, None, None]
    with pytest.raises(CellError, match=r"the 'X' cell 'na' is not a number, nor one of \['', 'NA', '\.'\]$"):
        _one(_Missing, "na")


def test_a_null_cell_is_an_empty_one() -> None:
    frame = pl.DataFrame({"X": [None, "1"]}, schema={"X": pl.String})
    assert read_table(frame, _Missing)["X"].to_list() == [None, 1.0]


def test_a_required_field_has_no_missing_token() -> None:
    _refused(_Int, "")


class _Stripped(msgspec.Struct, frozen=True, gc=False):
    both: Annotated[float, Column("Both", strip=" ")]
    start: Annotated[float, Column("Start", strip_start=" ")]


def test_strip_takes_padding_off_both_ends_and_strip_start_off_the_start_alone() -> None:
    frame = pl.DataFrame({"Both": [" 1.5 "], "Start": ["  2"]})
    assert read_table(frame, _Stripped).rows() == [(1.5, 2.0)]
    with pytest.raises(CellError, match=r"the 'Start' cell '2 ' is not a number"):
        read_table(pl.DataFrame({"Both": ["1"], "Start": ["2 "]}), _Stripped)


class _Prefixed(msgspec.Struct, frozen=True, gc=False):
    series: Annotated[str, Column("Series", prefix="Z1/Z1/")]


def test_a_prefix_is_checked_and_taken_off() -> None:
    assert _one(_Prefixed, "Z1/Z1/FL1") == ["FL1"]
    _refused(_Prefixed, "FL1")


class _Flow(msgspec.Struct, frozen=True, gc=False):
    flow: Annotated[float | None, Column("Flow", grouping=",", parens_negative=True, missing=("-",))]


class _Count(msgspec.Struct, frozen=True, gc=False):
    count: Annotated[int, Column("Count", grouping=",")]


def test_an_accounting_number_reads_with_its_grouping_and_its_parenthesized_negative() -> None:
    assert _one(_Flow, "1,234.5", "(95.1)", "12", "-", "(1,000)", "-5") == [1234.5, -95.1, 12.0, None, -1000.0, -5.0]
    assert _one(_Count, "1,234", "12") == [1234, 12]


@pytest.mark.parametrize("cell", ["1234.5", "1,23.5", "12,", "1,0451.0", "(95.1", "(-1)", "--1"])
def test_an_accounting_number_groups_its_thousands_where_it_has_them(cell: str) -> None:
    _refused(_Flow, cell)


class _Shaped(msgspec.Struct, frozen=True, gc=False):
    code: Annotated[str, Column("Code", pattern=r"[A-Z]{3}")]
    x: Annotated[float, Column("X", pattern=r"[0-9.]+|" + NONFINITE)]


def test_a_pattern_is_matched_whole() -> None:
    assert read_table(pl.DataFrame({"Code": ["ABC"], "X": ["NaN"]}), _Shaped)["Code"].to_list() == ["ABC"]
    for code, x in (("ABCD", "1"), ("AB", "1"), ("ABC", "-1")):
        with pytest.raises(CellError, match="is not"):
            read_table(pl.DataFrame({"Code": [code], "X": [x]}), _Shaped)


class _Limited(msgspec.Struct, frozen=True, gc=False):
    n: Annotated[int, msgspec.Meta(ge=0, lt=10), Column("N")]
    x: Annotated[Annotated[float, msgspec.Meta(gt=0, multiple_of=0.5)] | None, Column("X", missing=("",))]
    s: Annotated[str, msgspec.Meta(min_length=2, max_length=3, pattern="^[A-Z]+$"), Column("S")]


def test_a_fields_meta_limits_hold() -> None:
    frame = pl.DataFrame({"N": ["0", "9"], "X": ["1.5", ""], "S": ["AB", "ABC"]})
    assert read_table(frame, _Limited).rows() == [(0, 1.5, "AB"), (9, None, "ABC")]


@pytest.mark.parametrize(
    ("n", "x", "s", "named"),
    [("10", "1", "AB", "N"), ("-1", "1", "AB", "N"), ("1", "0", "AB", "X"), ("1", "0.7", "AB", "X")]
    + [("1", "1", s, "S") for s in ("A", "ABCD", "ab")],
)
def test_a_cell_outside_its_meta_limits_is_refused(n: str, x: str, s: str, named: str) -> None:
    with pytest.raises(CellError, match=f"the {named!r} cell .* is not within its constraints"):
        read_table(pl.DataFrame({"N": [n], "X": [x], "S": [s]}), _Limited)


@pytest.mark.parametrize(
    ("n", "s", "named"),
    [
        ("-1", "AB", r"the 'N' cell '-1' is not within its constraints \(ge=0, lt=10\)$"),
        (
            "1",
            "A",
            r"the 'S' cell 'A' is not within its constraints \(min_length=2, max_length=3, pattern='\^\[A-Z\]\+\$'\)$",
        ),
        ("x", "AB", r"the 'N' cell 'x' is not a whole number$"),
    ],
    ids=["ge", "min_length", "not its type at all"],
)
def test_a_cell_of_its_type_outside_its_meta_limits_is_named_so(n: str, s: str, named: str) -> None:
    with pytest.raises(CellError, match=f"^row 1: {named}"):
        read_table(pl.DataFrame({"N": [n], "X": ["1"], "S": [s]}), _Limited)


class _LookAhead(msgspec.Struct, frozen=True, gc=False):
    code: Annotated[str, Column("Code", pattern=r"(?=A)[A-Z]+")]


class _LookBehind(msgspec.Struct, frozen=True, gc=False):
    code: Annotated[str, msgspec.Meta(pattern=r"(?<=a)b"), Column("Code")]


@pytest.mark.parametrize("row_type", [_LookAhead, _LookBehind], ids=["a column's", "a Meta's"])
def test_a_pattern_polars_cannot_compile_is_refused_with_its_declaration(row_type: type[msgspec.Struct]) -> None:
    with pytest.raises(
        TypeError,
        match=rf"^{row_type.__name__}\.code's pattern .* is not one polars' regex reads: look-around, .* supported$",
    ):
        column_specs(row_type)


class _Day(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Column("Day", format="%m/%d/%Y")]


class _IsoDay(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Column("Day", format="%Y-%m-%d")]


class _EitherDay(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Column("Day", format=("%Y-%m-%d", "%m/%d/%Y"))]


def test_a_date_reads_in_its_format() -> None:
    assert _one(_Day, "12/31/2025") == [dt.date(2025, 12, 31)]
    assert _one(_IsoDay, "2026-01-02") == [dt.date(2026, 1, 2)]


@pytest.mark.parametrize(
    ("row_type", "cell"),
    [
        (_Day, "12/31/25"),  # polars' %Y reads 25 as the year 25: the value does not write back so
        (_IsoDay, "26-01-02"),
        (_Day, "02/30/2026"),
        (_IsoDay, "2026-02-30"),
        (_Day, "1/2/2026"),  # polars' %m and %d read one digit too
        (_IsoDay, "2026-01-02 "),
        (_IsoDay, "0000-01-01"),  # a year a Python date cannot hold
    ],
    ids=["a two-digit year", "a two-digit ISO year", "Feb 30", "ISO Feb 30", "unpadded", "padded", "year 0"],
)
def test_a_date_that_does_not_write_back_in_its_format_is_refused(row_type: type[msgspec.Struct], cell: str) -> None:
    _refused(row_type, cell)


def test_a_date_in_either_of_its_formats_reads_and_any_other_fails() -> None:
    assert _one(_EitherDay, "2026-01-02", "01/03/2026") == [dt.date(2026, 1, 2), dt.date(2026, 1, 3)]
    with pytest.raises(CellError, match=r"'2026/01/02' is not a date in %Y-%m-%d or %m/%d/%Y$"):
        _one(_EitherDay, "2026/01/02")


class _Named(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Column("Day", format="%d-%b-%Y")]


def test_a_month_name_reads_in_any_case() -> None:
    assert _one(_Named, "02-Jan-2026", "02-JAN-2026") == [dt.date(2026, 1, 2)] * 2


class _At(msgspec.Struct, frozen=True, gc=False):
    at: Annotated[dt.datetime, Column("At", format="%Y-%m-%d %H:%M:%S%.f")]


class _Stamped(msgspec.Struct, frozen=True, gc=False):
    at: Annotated[UtcDatetime, Column("At", format="%Y-%m-%d %H:%M:%S%z")]


class _Utc(msgspec.Struct, frozen=True, gc=False):
    at: Annotated[UtcDatetime, Column("At", format="%Y-%m-%d %H:%M:%S")]


class _Tape(msgspec.Struct, frozen=True, gc=False):
    at: Annotated[UtcDatetime, Column("At", format="%Y-%m-%d %H:%M:%S%.f%#z")]


class _Zulu(msgspec.Struct, frozen=True, gc=False):
    at: Annotated[dt.datetime, Column("At", format="%Y-%m-%dT%H:%M:%S%.fZ")]


def test_a_datetime_reads_with_or_without_its_fraction() -> None:
    assert _one(_At, "2026-01-02 03:04:05.25", "2026-01-02 03:04:05") == [
        dt.datetime(2026, 1, 2, 3, 4, 5, 250000),
        dt.datetime(2026, 1, 2, 3, 4, 5),
    ]


@pytest.mark.parametrize(
    "cell", ["2024-01-01 23:59:61", "2024-01-01 24:00:00", "2024-01-01 23:60:00", "2024-13-01 00:00:00"]
)
def test_a_time_out_of_range_is_refused_not_rolled_over(cell: str) -> None:
    _refused(_At, cell)


def test_a_leap_second_is_the_next_minutes_first() -> None:
    assert _one(_At, "2016-12-31 23:59:60", "2015-06-30 23:59:60.5") == [
        dt.datetime(2017, 1, 1),
        dt.datetime(2015, 7, 1, 0, 0, 0, 500000),
    ]


@pytest.mark.parametrize(
    "cell", ["2025-01-05 01:02:60", "2025-01-05 01:02:60.5", "2025-01-05 23:59:60", "2016-12-31 22:59:60"]
)
def test_a_leap_second_anywhere_but_the_end_of_june_or_december_is_refused(cell: str) -> None:
    _refused(_At, cell)


def test_a_leap_second_with_an_offset_is_one_in_utc() -> None:
    cells = ("2016-12-31 23:59:60+0000", "2016-12-31 18:59:60-0500", "2017-01-01 00:59:60+0100")
    frame = read_table(pl.DataFrame({"At": list(cells)}), _Stamped)
    assert frame["At"].to_list() == [dt.datetime(2017, 1, 1, tzinfo=dt.UTC)] * 3


@pytest.mark.parametrize("cell", ["2016-12-31 23:59:60-0500", "2017-01-01 00:59:60+0000"])
def test_a_leap_second_at_local_midnight_but_not_at_utc_midnight_is_refused(cell: str) -> None:
    _refused(_Stamped, cell)


def test_a_utc_datetime_reads_its_offset_into_utc() -> None:
    frame = read_table(pl.DataFrame({"At": ["2026-01-02 03:04:05+0000", "2026-01-02 03:04:05-0500"]}), _Stamped)
    assert frame.schema == pl.Schema({"At": pl.Datetime("us", "UTC")})
    assert frame["At"].to_list() == [
        dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC),
        dt.datetime(2026, 1, 2, 8, 4, 5, tzinfo=dt.UTC),
    ]
    for cell in ("2026-01-02 03:04:05", "2026-02-30 03:04:05+0000"):
        _refused(_Stamped, cell)


def test_a_utc_datetime_reads_any_offset_its_format_does() -> None:
    """Unusual Whales writes ``+00``, an offset of hours alone, which ``%#z`` reads, as it reads ``Z``."""
    assert _one(
        _Tape,
        "2024-08-21 13:50:52.278302+00",
        "2024-08-21 13:50:52Z",
        "2024-08-21 13:50:52+0000",
        "2024-08-21 08:50:52-05:00",
    ) == [
        dt.datetime(2024, 8, 21, 13, 50, 52, 278302, tzinfo=dt.UTC),
        dt.datetime(2024, 8, 21, 13, 50, 52, tzinfo=dt.UTC),
        dt.datetime(2024, 8, 21, 13, 50, 52, tzinfo=dt.UTC),
        dt.datetime(2024, 8, 21, 13, 50, 52, tzinfo=dt.UTC),
    ]


@pytest.mark.parametrize(
    ("row_type", "cell"),
    [
        (_Stamped, "26-01-02 03:04:05+0000"),
        (_Stamped, "2026-01-02 3:4:5+0000"),
        (_Tape, "24-08-21 13:50:52.278302+00"),
        (_Tape, "2024-08-21 13:50:52.278302+0"),
    ],
    ids=["a two-digit year", "unpadded", "a two-digit year before an hours offset", "a one-digit offset"],
)
def test_a_moment_with_an_offset_that_does_not_write_back_is_refused(row_type: type[msgspec.Struct], cell: str) -> None:
    _refused(row_type, cell)


def test_a_fraction_reads_with_text_after_it() -> None:
    assert _one(_Zulu, "2025-01-01T00:00:00Z", "2025-01-01T00:00:00.123Z") == [
        dt.datetime(2025, 1, 1),
        dt.datetime(2025, 1, 1, 0, 0, 0, 123000),
    ]


def test_a_utc_datetime_whose_format_has_no_offset_is_read_as_utc() -> None:
    assert _one(_Utc, "2026-01-02 03:04:05") == [dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)]


class _Row(msgspec.Struct, frozen=True, gc=False):
    symbol: Annotated[str, Column("Symbol")]
    total: Annotated[float, Column(("Total", "Sum"))]
    note: Annotated[str | None, Column("Note", missing=("",), optional=True)]


def test_a_table_reads_under_its_own_labels() -> None:
    frame = read_table(pl.DataFrame({"Symbol": [" a "], "Sum": ["1.5"]}), _Row)
    assert frame.columns == ["Symbol", "Sum"]
    assert frame.rows() == [(" a ", 1.5)]


def test_an_any_order_table_reads_each_column_by_its_label() -> None:
    frame = read_table(pl.DataFrame({"Note": [""], "Total": ["1"], "Symbol": ["a"]}), _Row, ordered=False)
    assert frame.select("Note", "Total", "Symbol").rows() == [(None, 1.0, "a")]


@pytest.mark.parametrize(
    "labels",
    [["Total", "Symbol"], ["Symbol"], ["Symbol", "Total", "Note", "Extra"], ["Symbol", "Total", "Sum"]],
    ids=["out of order", "a column short", "a column over", "both labels"],
)
def test_a_table_whose_header_is_not_the_columns_raises(labels: list[str]) -> None:
    with pytest.raises(UnknownLayoutError, match="is not the columns"):
        read_table(pl.DataFrame({label: ["1"] for label in labels}), _Row)


class _Fillers(msgspec.Struct, frozen=True, gc=False):
    a: Annotated[str, Column("Filler")]
    b: Annotated[str, Column("Filler")]
    c: Annotated[str, Column("Filler")]


@pytest.mark.parametrize(
    "names", [["Filler", "Filler_duplicated_0", "Filler_duplicated_1"], ["Filler", "Filler_1", "Filler_2"]]
)
def test_a_repeated_label_takes_polars_names_for_its_repeats(names: list[str]) -> None:
    assert wire_names(["Filler"] * 3) == ["Filler", "Filler_duplicated_0", "Filler_duplicated_1"]
    frame = read_table(pl.DataFrame({name: [name] for name in names}), _Fillers)
    assert (frame.columns, frame.rows()) == (names, [tuple(names)])


def test_an_error_names_the_row_its_caller_numbers() -> None:
    with pytest.raises(CellError, match=r"^row 11: the 'N' cell 'z'") as raised:
        read_table(pl.DataFrame({"N": ["1", "z"]}), _Int, numbers=pl.Series([10, 11]))
    assert raised.value.index == 1


class _NoColumn(msgspec.Struct, frozen=True, gc=False):
    a: str


class _MissingOnRequired(msgspec.Struct, frozen=True, gc=False):
    a: Annotated[float, Column("A", missing=("",))]


class _NeverNone(msgspec.Struct, frozen=True, gc=False):
    a: Annotated[float | None, Column("A")]


class _DateWithoutFormat(msgspec.Struct, frozen=True, gc=False):
    a: Annotated[dt.date, Column("A")]


class _FormatWithoutDate(msgspec.Struct, frozen=True, gc=False):
    a: Annotated[str, Column("A", format="%Y")]


class _Unreadable(msgspec.Struct, frozen=True, gc=False):
    a: Annotated[bytes, Column("A")]


class _BoolWithoutTokens(msgspec.Struct, frozen=True, gc=False):
    a: Annotated[bool, Column("A", true="Y")]


class _NumberLiteral(msgspec.Struct, frozen=True, gc=False):
    a: Annotated[Literal[1, 2], Column("A")]


class _OffsetFirst(msgspec.Struct, frozen=True, gc=False):
    a: Annotated[UtcDatetime, Column("A", format="%z %Y-%m-%d %H:%M:%S")]


class _NaiveOffset(msgspec.Struct, frozen=True, gc=False):
    a: Annotated[dt.datetime, Column("A", format="%Y-%m-%d %H:%M:%S%z")]


class _Either(msgspec.Struct, frozen=True, gc=False):
    a: Annotated[int | str, Column("A")]


@pytest.mark.parametrize(
    ("row_type", "error", "named"),
    [
        (_NoColumn, TypeError, "declares 0 Columns, not one"),
        (_MissingOnRequired, ValueError, "cannot be None"),
        (_NeverNone, ValueError, "declares no missing token and is not optional"),
        (_DateWithoutFormat, ValueError, "a date or datetime gives its format, and only it does"),
        (_FormatWithoutDate, ValueError, "a date or datetime gives its format, and only it does"),
        (_Unreadable, TypeError, "which no cell reads as"),
        (_BoolWithoutTokens, ValueError, "without both its true and false tokens"),
        (_NumberLiteral, TypeError, "only text values read from a cell"),
        (_OffsetFirst, ValueError, r"an offset \(%z or %#z\) ends its format"),
        (_NaiveOffset, ValueError, r"^_NaiveOffset\.a reads an offset, so it is a UtcDatetime$"),
        (_Either, TypeError, r"^_Either\.a is a union of "),
    ],
)
def test_a_declaration_that_cannot_be_read_fails_before_any_cell(
    row_type: type[msgspec.Struct], error: type[Exception], named: str
) -> None:
    with pytest.raises(error, match=named):
        column_specs(row_type)


def test_a_row_structs_columns_are_checked_once_and_held_only_while_it_lives() -> None:
    """A struct built per header is freed when its builder drops it: the columns' cache holds it weakly."""
    row_type = msgspec.defstruct("Built", [("a", Annotated[str, Column("A")])])
    assert column_specs(row_type) is column_specs(row_type)
    alive = weakref.ref(row_type)

    del row_type
    gc.collect()

    assert alive() is None
