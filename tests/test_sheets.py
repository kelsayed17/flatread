import datetime as dt
import pathlib
import re
from typing import TYPE_CHECKING, Any, override

import fastexcel
import msgspec
import pytest

from flatread.core import Long
from flatread.errors import CellError, UnknownLayoutError
from flatread.sheets import Sheet, Sheets
from tests.xlsx import workbook

if TYPE_CHECKING:
    import polars as pl

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "workbooks"
ADS = (FIXTURES / "ads_index_sample.xlsx").read_bytes()
ACM = (FIXTURES / "acm_term_premium_sample.xls").read_bytes()
PSW = (FIXTURES / "psw01_sample.xls").read_bytes()


class AdsDay(msgspec.Struct, frozen=True):
    day: dt.date
    ads_index: float
    recession: int


class AdsIndex(Sheet):
    row = AdsDay
    labels = {"Date": "day", "ADS_Index": "ads_index", "RECBARS": "recession"}
    dates = {"day": "%Y:%m:%d"}


def count_loads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    original = fastexcel.ExcelReader.load_sheet

    def counted(self: fastexcel.ExcelReader, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401  # load_sheet's own
        calls.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(fastexcel.ExcelReader, "load_sheet", counted)
    return calls


def test_ads_index_reads_in_one_typed_load(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = count_loads(monkeypatch)
    frame = AdsIndex.frame(ADS)
    assert len(calls) == 2  # the header row, then the table once
    assert frame.height == 6
    # Every digit of the cell's XML (<v>-0.5816963701473981</v>), not calamine's nine-digit text.
    assert frame.row(0) == (dt.date(1960, 3, 1), -0.5816963701473981, 0)


def test_every_digit_survives() -> None:
    body = workbook({"Sheet1": [["Date", "ADS_Index", "RECBARS"], ["2026:01:02", 13442.008862616791, 0]]})
    assert AdsIndex.frame(body)["ads_index"][0] == 13442.008862616791


class Rate(msgspec.Struct, frozen=True):
    day: str
    rate: float | None
    code: str


class Rates(Sheet):
    row = Rate
    labels = {"Date": "day", "Rate": "rate", "Code": "code"}
    missing = frozenset({"", "."})


def test_a_mixed_column_takes_the_two_load_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    body = workbook({"S": [["Date", "Rate", "Code"], ["a", 13442.008862616791, 1001], ["b", ".", 2020.5]]})
    calls = count_loads(monkeypatch)
    frame = Rates.frame(body)
    assert len(calls) == 4  # the header, the refused strict load, then text and numbers
    assert frame["rate"].to_list() == [13442.008862616791, None]
    assert frame["code"].to_list() == ["1001", "2020.5"]


class Whole(msgspec.Struct, frozen=True):
    name: str
    count: int


def test_an_int_field_refuses_a_fraction() -> None:
    class Counts(Sheet):
        row = Whole
        labels = {"Name": "name", "Count": "count"}

    assert Counts.frame(workbook({"S": [["Name", "Count"], ["a", 3]]})).row(0) == ("a", 3)
    with pytest.raises(CellError, match=r"sheet 'S' row 3: the 'Count' cell '3.5' is not a whole number"):
        Counts.frame(workbook({"S": [["Name", "Count"], ["a", 3], ["b", 3.5]]}))


def test_blank_row_end_then_footer() -> None:
    class Footed(Rates):
        footer = r"Note:.*|Source:.*"

    rows = [["Date", "Rate", "Code"], ["a", 1.0, "x"], [None, None, None], ["Note: revised", None, None]]
    assert Footed.frame(workbook({"S": rows})).height == 1
    with pytest.raises(UnknownLayoutError, match=r"sheet 'S' row 5"):
        Footed.frame(workbook({"S": [*rows, ["b", 2.0, "y"]]}))
    with pytest.raises(UnknownLayoutError, match="row 4"):
        Rates.frame(workbook({"S": rows}))


def test_last_row_end_skips_blank_rows() -> None:
    class Gappy(Rates):
        end = "last_row"

    rows = [["Date", "Rate", "Code"], ["a", 1.0, "x"], [None, None, None], ["b", 2.0, "y"]]
    assert Gappy.frame(workbook({"S": rows}))["day"].to_list() == ["a", "b"]


class Yield(msgspec.Struct, frozen=True):
    frequency: str
    day: dt.date
    maturity: str
    value: float


def test_every_sheet_reads_long_with_the_sheet_name() -> None:
    class Acm(Sheet):
        row = Yield
        labels = {"DATE": "day"}
        dates = {"day": "%d-%b-%Y"}
        long = Long(("DATE",), "maturity", "value", r"ACM(Y|TP|RNY)\d{2}")
        every_sheet = Sheets("frequency")

    frame = Acm.frame(ACM)
    assert set(frame["frequency"]) == {"ACM Monthly", "ACM Daily"}
    first = frame.row(0)
    assert first == ("ACM Monthly", dt.date(1961, 6, 30), "ACMY01", 2.9002725007830223)  # the cell's own double


class Stock(msgspec.Struct, frozen=True):
    day: dt.date
    crude: int


def test_an_xls_reads_by_its_bytes_and_native_dates_read() -> None:
    class Psw(Sheet):
        row = Stock
        sheet = "Data 1"
        header_row = 1
        labels = (("Sourcekey", "day"), ("WCRSTUS1", "crude"))
        columns = "A:B"

        @classmethod
        @override
        def cells(cls, frame: pl.DataFrame) -> pl.DataFrame:
            return frame.slice(1)  # EIA's second header row: each series' description

    frame = Psw.frame(PSW)
    assert frame.row(0) == (dt.date(1982, 8, 20), 609219)


def test_the_sheet_is_picked_by_name_or_regex() -> None:
    class Second(Rates):
        sheet = re.compile(r"Data \d+")

    body = workbook({"Contents": [["x"]], "Data 1": [["Date", "Rate", "Code"], ["a", 1.0, "x"]]})
    assert Second.frame(body).height == 1
    with pytest.raises(UnknownLayoutError, match="the header"):
        Rates.frame(body)  # the first sheet is Contents, whose header is not the layout's


def test_a_body_that_is_no_workbook_raises() -> None:
    with pytest.raises(UnknownLayoutError, match="not a workbook"):
        Rates.frame(b"<html>error</html>")
