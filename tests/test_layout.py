import datetime as dt
from typing import TYPE_CHECKING, override

import msgspec
import polars as pl
import pytest

from flatread.core import Erratum, Long, Text, run
from flatread.errors import (
    CellError,
    CheckError,
    EmptyBodyError,
    ErratumGoneError,
    NotPublishedError,
    UnknownLayoutError,
)
from flatread.layout import Layout

if TYPE_CHECKING:
    from collections.abc import Sequence


class Rate(msgspec.Struct, frozen=True):
    day: dt.date
    rate: float | None


class Rates(Layout):
    row = Rate
    labels = {"Date": "day", "Rate": "rate"}
    missing = frozenset({"", "."})


def text(**columns: Sequence[str | None]) -> pl.DataFrame:
    return pl.DataFrame(columns, schema=dict.fromkeys(columns, pl.String))


def test_labels_rename_to_fields_in_struct_order() -> None:
    frame = Rates.from_text(text(Date=["2026-11-09", "2026-11-10"], Rate=["4.5", "."]))
    assert frame.columns == ["day", "rate"]
    assert frame.rows() == [(dt.date(2026, 11, 9), 4.5), (dt.date(2026, 11, 10), None)]


def test_a_label_mapped_to_none_is_checked_then_dropped() -> None:
    class WithNotes(Rates):
        labels = {"Date": "day", "Rate": "rate", "Notes": None}

    frame = WithNotes.from_text(text(Date=["2026-11-09"], Rate=["4.5"], Notes=["revised"]))
    assert frame.columns == ["day", "rate"]
    with pytest.raises(UnknownLayoutError, match="Notes"):
        WithNotes.from_text(text(Date=["2026-11-09"], Rate=["4.5"]))


class Threshold(msgspec.Struct):
    symbol: str
    filler_1: str
    filler_2: str


def test_a_repeated_label_maps_by_order() -> None:
    class Thresholds(Layout):
        row = Threshold
        labels = (("Symbol", "symbol"), ("Filler", "filler_1"), ("Filler", "filler_2"))

    frame = Thresholds.from_text(pl.DataFrame({"Symbol": ["AAPL"], "Filler": ["a"], "Filler#2": ["b"]}))
    assert frame.row(0) == ("AAPL", "a", "b")


def test_variants_try_in_order_and_name_what_was_found() -> None:
    class Renamed(Rates):
        variants = ({"Observation date": "day", "Rate": "rate"},)

    assert Renamed.from_text(text(**{"Observation date": ["2026-11-09"], "Rate": ["1"]})).height == 1
    with pytest.raises(UnknownLayoutError, match=r"\['Day', 'Rate'\]"):
        Renamed.from_text(text(Day=["2026-11-09"], Rate=["1"]))


def test_settings_are_inherited_and_overridden_whole() -> None:
    class Strict(Rates):
        missing = frozenset({"."})

    assert Rates.from_text(text(Date=["2026-11-09"], Rate=[""]))["rate"][0] is None
    with pytest.raises(CellError):
        Strict.from_text(text(Date=["2026-11-09"], Rate=[""]))


def test_an_unknown_setting_or_label_raises_at_class_build() -> None:
    with pytest.raises(TypeError, match="separatr"):

        class Typo(Rates):
            separatr = "|"

    with pytest.raises(TypeError, match="no field 'when'"):

        class BadLabel(Rates):
            labels = {"Date": "when", "Rate": "rate"}

    with pytest.raises(TypeError, match="'rate'"):

        class Unfilled(Rates):
            labels = {"Date": "day"}


def test_a_defaults_class_is_not_read() -> None:
    class Provider(Layout):
        missing = frozenset({"."})

    with pytest.raises(TypeError, match="no row"):
        Provider.frame(b"x")


def test_errata_fix_text_before_typing_and_raise_when_gone() -> None:
    class Fixed(Rates):
        errata = (Erratum("Rate", "4,5", "4.5", "a comma for a point", where={"Date": "2026-11-09"}),)

    frame = Fixed.from_text(text(Date=["2026-11-09", "2026-11-10"], Rate=["4,5", "1"]))
    assert frame["rate"].to_list() == [4.5, 1.0]
    with pytest.raises(ErratumGoneError, match="a comma for a point"):
        Fixed.from_text(text(Date=["2026-11-09"], Rate=["4.5"]))


class Point(msgspec.Struct, frozen=True):
    day: dt.date
    series: str
    value: float


class Wide(Layout):
    row = Point
    labels = {"Date": "day"}
    long = Long(index=("Date",), name="series", value="value", names=r"[A-Z]+")
    missing = frozenset({"", "."})


def test_long_melts_checks_names_and_drops_missing_values() -> None:
    frame = Wide.from_text(text(Date=["2026-11-09", "2026-11-10"], GDP=["1", "."], CPI=["2", "3"]))
    assert frame.rows() == [
        (dt.date(2026, 11, 9), "GDP", 1.0),
        (dt.date(2026, 11, 9), "CPI", 2.0),
        (dt.date(2026, 11, 10), "CPI", 3.0),
    ]
    with pytest.raises(UnknownLayoutError, match="gdp_2"):
        Wide.from_text(text(Date=["2026-11-09"], gdp_2=["1"]))


class Grouped(msgspec.Struct, frozen=True):
    day: dt.date
    group: str
    series: str
    value: float


def test_headings_carry_rightwards_into_long_rows() -> None:
    class Groups(Layout):
        row = Grouped
        labels = {"Date": "day"}
        long = Long(index=("Date",), name="series", value="value", names=r"[A-Z]+")
        headings = "group"

    frame = run(
        Groups._plan,
        Text(
            text(Date=["2026-11-09"], A=["1"], B=["2"], **{"A#2": ["3"]}),
            lambda i: f"row {i}",
            headings=(None, "Prices", None, "Rates"),
        ),
    )
    assert frame.rows() == [
        (dt.date(2026, 11, 9), "Prices", "A", 1.0),
        (dt.date(2026, 11, 9), "Prices", "B", 2.0),
        (dt.date(2026, 11, 9), "Rates", "A", 3.0),
    ]


def test_cells_and_check_hooks_run_when_overridden() -> None:
    class Hooked(Rates):
        @classmethod
        @override
        def cells(cls, frame: pl.DataFrame) -> pl.DataFrame:
            return frame.with_columns(pl.col("Rate").str.replace("%", ""))

        @classmethod
        @override
        def check(cls, frame: pl.DataFrame) -> None:
            if (frame["rate"] > 10).any():
                msg = "a rate over 10"
                raise CheckError(msg)

    assert Hooked.from_text(text(Date=["2026-11-09"], Rate=["4%"]))["rate"][0] == 4.0
    with pytest.raises(CheckError):
        Hooked.from_text(text(Date=["2026-11-09"], Rate=["40%"]))


def test_key_must_be_unique() -> None:
    class Keyed(Rates):
        key = ("day",)

    with pytest.raises(CheckError, match="2026-11-09"):
        Keyed.from_text(text(Date=["2026-11-09", "2026-11-09"], Rate=["1", "2"]))


def test_where_filters_after_check() -> None:
    class Some(Rates):
        where = pl.col("rate") > 1

        @classmethod
        @override
        def check(cls, frame: pl.DataFrame) -> None:
            assert frame.height == 2

    assert Some.from_text(text(Date=["2026-11-09", "2026-11-10"], Rate=["1", "2"])).height == 1


def test_min_rows() -> None:
    with pytest.raises(EmptyBodyError):
        Rates.from_text(text(Date=[], Rate=[]))

    class MaybeEmpty(Rates):
        min_rows = 0

    assert MaybeEmpty.from_text(text(Date=[], Rate=[])).schema == pl.Schema({"day": pl.Date, "rate": pl.Float64})


class Stamped(msgspec.Struct, frozen=True):
    symbol: str
    generated_at: dt.date


def test_carry_fills_every_row() -> None:
    class Stamps(Layout):
        row = Stamped
        labels = {"Symbol": "symbol"}

        @classmethod
        @override
        def _carried_fields(cls) -> frozenset[str]:
            return frozenset({"generated_at"})

    frame = run(Stamps._plan, Text(text(Symbol=["A", "B"]), str, carried={"generated_at": "2026-11-09"}))
    assert frame["generated_at"].to_list() == [dt.date(2026, 11, 9)] * 2


class Body(Rates):
    refusals = {b"Page Not Available": NotPublishedError}

    @classmethod
    @override
    def read_text(cls, body: bytes) -> Text:
        lines = body.decode().splitlines()
        labels, *rows = (line.split(",") for line in lines)
        return Text(pl.DataFrame(rows, schema=labels, orient="row"), lambda i: f"line {i + 2}")


def test_refusals_raise_before_parsing() -> None:
    with pytest.raises(NotPublishedError):
        Body.frame(b"<html>Page Not Available</html>")


def test_frame_reads_through_read_text_and_names_the_line() -> None:
    assert Body.frame(b"Date,Rate\n2026-11-09,1\n").height == 1
    with pytest.raises(CellError, match="line 3: the 'Rate' cell 'x'"):
        Body.frame(b"Date,Rate\n2026-11-09,1\n2026-11-10,x\n")


def test_an_empty_body_with_min_rows_zero_is_an_empty_frame() -> None:
    with pytest.raises(EmptyBodyError):
        Body.frame(b"  \n")

    class Empty(Body):
        min_rows = 0

    assert Empty.frame(b"").height == 0
    assert Empty.rows(b"") == []


def test_rows_equal_frame() -> None:
    assert Body.rows(b"Date,Rate\n2026-11-09,1\n2026-11-10,.\n") == [
        Rate(dt.date(2026, 11, 9), 1.0),
        Rate(dt.date(2026, 11, 10), None),
    ]
