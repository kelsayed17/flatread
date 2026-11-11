"""Settings three or more providers need, from the trial ports, and the JSON policies the recorded bodies settle."""

import datetime as dt
import zoneinfo

import msgspec
import polars as pl
import pytest

from flatread.cells import EPOCH_MS, EPOCH_S, fields_of, type_frame
from flatread.delimited import Csv
from flatread.errors import CellError, EmptyBodyError, RaggedRowError, UnknownLayoutError
from flatread.records import Json

# --- JSON: a null records list is no records; an absent one is an error page ---


class Bar(msgspec.Struct, frozen=True):
    sym: str
    px: float | None


class Bars(Json):
    row = Bar
    records = "data"
    labels = {"sym": "sym", "px": "px"}
    min_rows = 0


def test_a_null_records_list_is_no_records() -> None:
    assert Bars.frame(b'{"data": null}').height == 0  # OCC's slot for a month it has no data for

    class Some(Bars):
        min_rows = 1

    with pytest.raises(EmptyBodyError):
        Some.frame(b'{"data": null}')


def test_an_absent_records_list_is_an_error_page() -> None:
    with pytest.raises(UnknownLayoutError, match="data"):
        Bars.frame(b'{"error": "rate limited"}')


def test_a_null_list_on_the_way_down_holds_no_records() -> None:
    class Nested(Bars):
        records = "results[].bars"

    body = b'{"results": [{"bars": null}, {"bars": [{"sym": "A", "px": 1}]}, null]}'
    assert Nested.frame(body).rows() == [("A", 1.0)]


def test_a_null_list_body_is_no_records() -> None:
    class Listed(Bars):
        records = ""

    assert Listed.frame(b"null").height == 0


def test_a_key_a_record_leaves_out_reads_as_null_and_a_required_one_raises() -> None:
    # Socrata (CFTC), Cboe, SEC and Yahoo all leave out a key that has no value.
    assert Bars.frame(b'{"data": [{"sym": "A"}, {"sym": "B", "px": 2}]}').rows() == [("A", None), ("B", 2.0)]
    with pytest.raises(UnknownLayoutError, match="sym"):
        Bars.frame(b'{"data": [{"px": 2}]}')


# --- strip, per field ---


class Named(msgspec.Struct, frozen=True):
    code: str
    name: str


def test_strip_may_name_the_fields_it_touches() -> None:
    class Padded(Csv):
        row = Named
        labels = {"C": "code", "N": "name"}
        strip = frozenset({"name"})

    assert Padded.frame(b"C,N\n A , B \n").rows() == [(" A ", "B")]


def test_strip_naming_no_field_is_refused_at_build() -> None:
    with pytest.raises(TypeError, match="'title'"):

        class Typo(Csv):
            row = Named
            labels = {"C": "code", "N": "name"}
            strip = frozenset({"title"})


def test_a_pattern_reads_a_json_number_as_its_text() -> None:
    class Sizes(Json):
        row = Size
        labels = {"n": "n"}
        patterns = {"n": r"\d{3}"}

    assert Sizes.frame(b'[{"n": 100}]')["n"].to_list() == [100]
    with pytest.raises(CellError, match="'10'"):
        Sizes.frame(b'[{"n": 10}]')


class Size(msgspec.Struct, frozen=True):
    n: int


# --- booleans ---


class Flag(msgspec.Struct, frozen=True):
    sym: str
    shown: bool | None


class Flags(Csv):
    row = Flag
    labels = {"S": "sym", "F": "shown"}
    booleans = {"Y": True, "N": False}


def test_booleans_name_a_providers_true_and_false() -> None:
    assert Flags.frame(b"S,F\nA,Y\nB,n\nC,\n")["shown"].to_list() == [True, False, None]
    with pytest.raises(CellError, match=r"one of 'Y' or 'N'"):
        Flags.frame(b"S,F\nA,true\n")


def test_booleans_read_a_json_string_where_native_bools_are_not_theirs() -> None:
    class Listed(Json):
        row = Flag
        labels = {"s": "sym", "f": "shown"}
        booleans = {"Yes": True, "No": False}

    assert Listed.frame(b'[{"s": "A", "f": "Yes"}, {"s": "B", "f": null}]')["shown"].to_list() == [True, None]


def test_booleans_must_hold_true_and_false() -> None:
    with pytest.raises(TypeError, match="booleans"):

        class OnlyTrue(Flags):
            booleans = {"Y": True}


# --- a pattern a field's text matches whole ---


class Security(msgspec.Struct, frozen=True):
    cusip: str
    coupon: float


class Securities(Csv):
    row = Security
    labels = {"CUSIP": "cusip", "Coupon": "coupon"}
    patterns = {"cusip": r"[0-9A-Z]{8}[0-9]"}


def test_a_pattern_refuses_text_it_does_not_match_whole() -> None:
    assert Securities.frame(b"CUSIP,Coupon\n912828ZT0,1.5\n").height == 1
    with pytest.raises(CellError, match=r"line 2.*912828ZT.*matching"):
        Securities.frame(b"CUSIP,Coupon\n912828ZT,1.5\n")
    with pytest.raises(CellError):
        Securities.frame(b"CUSIP,Coupon\n912828ZT0x,1.5\n")


def test_a_pattern_polars_cannot_compile_is_refused_at_build() -> None:
    with pytest.raises(TypeError, match="regex"):

        class Broken(Securities):
            patterns = {"cusip": "("}


# --- epoch dates and a provider's time zone ---


class Stamp(msgspec.Struct, frozen=True):
    at: dt.datetime


class Day(msgspec.Struct, frozen=True):
    day: dt.date


_CHICAGO_MIDNIGHT = 1793599200000  # OCC's activityDate for 2026-11-02, midnight in Chicago


def test_epoch_milliseconds_read_as_a_utc_time_from_a_json_number_or_text() -> None:
    class Stamps(Json):
        row = Stamp
        labels = {"t": "at"}
        dates = {"at": EPOCH_MS}

    utc = dt.datetime.fromtimestamp(_CHICAGO_MIDNIGHT / 1000, dt.UTC).replace(tzinfo=None)
    body = f'[{{"t": {_CHICAGO_MIDNIGHT}}}, {{"t": "{_CHICAGO_MIDNIGHT}"}}]'.encode()
    assert Stamps.frame(body)["at"].to_list() == [utc, utc]


def test_epoch_seconds_read_in_a_csv() -> None:
    class Seconds(Csv):
        row = Stamp
        labels = {"T": "at"}
        dates = {"at": EPOCH_S}

    assert Seconds.frame(b"T\n0\n")["at"].to_list() == [dt.datetime(1970, 1, 1)]
    with pytest.raises(CellError, match="seconds since"):
        Seconds.frame(b"T\n1.5\n")


class OccDays(Json):
    row = Day
    labels = {"d": "day"}
    dates = {"day": EPOCH_MS}
    time_zone = "America/Chicago"


def test_an_epoch_date_is_midnight_in_the_providers_zone() -> None:
    assert OccDays.frame(f'[{{"d": {_CHICAGO_MIDNIGHT}}}]'.encode())["day"].to_list() == [dt.date(2026, 11, 2)]
    with pytest.raises(CellError, match="record 1"):
        OccDays.frame(f'[{{"d": {_CHICAGO_MIDNIGHT + 3_600_000}}}]'.encode())


class Quotes(Csv):
    row = Stamp
    labels = {"T": "at"}
    dates = {"at": ("%Y-%m-%dT%H:%M:%S%:z", "%Y-%m-%d %H:%M:%S")}
    time_zone = "America/New_York"


def test_a_time_zone_makes_datetimes_aware_in_it() -> None:
    frame = Quotes.frame(b"T\n2026-06-01T16:00:00+00:00\n2026-06-01 12:00:00\n")
    assert frame.schema["at"] == pl.Datetime("us", "America/New_York")
    noon = dt.datetime(2026, 6, 1, 12, tzinfo=zoneinfo.ZoneInfo("America/New_York"))
    assert frame["at"].to_list() == [noon, noon]


def test_a_local_time_the_zone_skips_or_repeats_is_a_cell_error() -> None:
    for skipped_or_repeated in (b"2026-03-08 02:30:00", b"2026-11-01 01:30:00"):
        with pytest.raises(CellError, match="line 2"):
            Quotes.frame(b"T\n" + skipped_or_repeated + b"\n")


def test_a_typed_datetime_cell_reads_in_the_zone() -> None:
    # a sheet's datetime cell arrives typed; the test workbook writes no styles, so the column is built here
    fields = fields_of(Stamp, zone="America/New_York")
    typed, bad = type_frame(pl.DataFrame({"at": [dt.datetime(2026, 6, 1, 12), dt.datetime(2026, 3, 8, 2, 30)]}), fields)
    assert typed["at"][0] == dt.datetime(2026, 6, 1, 12, tzinfo=zoneinfo.ZoneInfo("America/New_York"))
    assert bad["at"].to_list() == [False, True]  # a time the zone skips


class Clock(msgspec.Struct, frozen=True):
    at: dt.time


def test_an_instant_format_on_a_time_of_day_is_refused_at_build() -> None:
    with pytest.raises(TypeError, match="time of day"):

        class Times(Csv):
            row = Clock
            labels = {"T": "at"}
            dates = {"at": EPOCH_MS}


def test_an_unknown_time_zone_is_refused_at_build() -> None:
    with pytest.raises(TypeError, match="Mars/Olympus"):

        class Nowhere(Quotes):
            time_zone = "Mars/Olympus"


# --- short rows ---


class Triple(msgspec.Struct, frozen=True):
    a: str
    b: str
    c: str | None


class Short(Csv):
    row = Triple
    labels = {"A": "a", "B": "b", "C": "c"}
    pad_short_rows = True


@pytest.mark.parametrize("body", [b"A,B,C\n1,2\n3,4,5\n", b'A,B,C\n1,2\n"3\nx",4,5\n'])  # fast, then slow path
def test_pad_short_rows_reads_a_short_row_as_empty_cells(body: bytes) -> None:
    assert Short.frame(body).rows()[0] == ("1", "2", None)


def test_pad_short_rows_still_refuses_a_long_one() -> None:
    with pytest.raises(RaggedRowError, match="line 2"):
        Short.frame(b"A,B,C\n1,2,3,4\n")
