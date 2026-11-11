"""JSON shapes the trial ports met, each on one path grammar.

Records ``a[]``, ``a{}``, ``a.b``; labels ``x.y``, ``[i]``, ``../x`` and ``{}``. The bodies are cut down from recorded
ones (OCC, Yahoo, Alpaca, SEC, Congress, EIA, Cboe).
"""

import datetime as dt

import msgspec
import pytest

from flatread.cells import EPOCH_MS, EPOCH_S
from flatread.errors import CellError, UnknownLayoutError
from flatread.records import Json

# --- one object as one row ---


class MonthStats(msgspec.Struct, frozen=True):
    total: int
    average: float | None


class OccMonth(Json):
    row = MonthStats
    records = "entity.dailyVolumeStatistics"
    labels = {"totalVolume": "total", "averageDailyVolume": "average"}
    min_rows = 0


def test_a_path_ending_in_a_key_reads_the_one_object_there() -> None:
    body = b'{"entity": {"dailyVolumeStatistics": {"totalVolume": 7, "averageDailyVolume": 1.5}, "other": null}}'
    assert OccMonth.frame(body).rows() == [(7, 1.5)]
    assert OccMonth.frame(b'{"entity": {"dailyVolumeStatistics": null}}').height == 0  # OCC: null for an old month


def test_an_empty_path_is_a_body_that_is_the_object() -> None:
    class Whole(OccMonth):
        records = ""

    assert Whole.frame(b'{"totalVolume": 7, "averageDailyVolume": null}').rows() == [(7, None)]


# --- nested keys ---


class Quote(msgspec.Struct, frozen=True):
    symbol: str
    bid: float | None
    ask: float


class Nested(Json):
    row = Quote
    records = "data[]"
    labels = {"symbol": "symbol", "quote.bid": "bid", "quote.ask": "ask"}


def test_a_dotted_label_reads_a_nested_key() -> None:
    body = b'{"data": [{"symbol": "A", "quote": {"bid": 1, "ask": 2}}, {"symbol": "B", "quote": {"ask": 3}}]}'
    assert Nested.frame(body).rows() == [("A", 1.0, 2.0), ("B", None, 3.0)]


def test_a_required_nested_key_under_a_null_object_is_a_cell_error() -> None:
    with pytest.raises(CellError, match=r"record 1: the 'quote\.ask' cell"):
        Nested.frame(b'{"data": [{"symbol": "A", "quote": null}]}')


# --- child lists carrying their parents' fields ---


class Option(msgspec.Struct, frozen=True):
    underlying: str
    expiry: dt.datetime
    strike: float
    volume: int | None


class Calls(Json):
    row = Option
    records = "optionChain.result[].options[].calls[]"
    labels = {"../../underlyingSymbol": "underlying", "../expirationDate": "expiry", "strike": "strike",
              "volume": "volume"}  # fmt: skip
    dates = {"expiry": EPOCH_S}


def test_dotdot_reads_a_field_of_the_item_each_walk_before_read() -> None:
    body = b"""{"optionChain": {"result": [{"underlyingSymbol": "AAPL", "options": [
        {"expirationDate": 0, "calls": [{"strike": 100, "volume": 5}, {"strike": 105}]},
        {"expirationDate": 86400, "calls": [{"strike": 110, "volume": 1}]}]}]}}"""
    assert Calls.frame(body).rows() == [
        ("AAPL", dt.datetime(1970, 1, 1), 100.0, 5),
        ("AAPL", dt.datetime(1970, 1, 1), 105.0, None),
        ("AAPL", dt.datetime(1970, 1, 2), 110.0, 1),
    ]


class Stamped(msgspec.Struct, frozen=True):
    sym: str
    as_of: dt.date | None


def test_dotdot_up_to_the_body_reads_its_envelope() -> None:
    class Quotes(Json):
        row = Stamped
        records = "data[]"
        labels = {"sym": "sym", "../meta.as_of": "as_of"}

    body = b'{"meta": {"as_of": "2026-11-09"}, "data": [{"sym": "A"}, {"sym": "B"}]}'
    assert Quotes.frame(body)["as_of"].to_list() == [dt.date(2026, 11, 9)] * 2
    assert Quotes.frame(b'{"data": [{"sym": "A"}]}')["as_of"].to_list() == [None]


def test_an_object_beneath_each_item_steps_out_to_that_item() -> None:
    class Summaries(Json):  # one stats object per result, its symbol on the result
        row = Stamped
        records = "results[].stats"
        labels = {"../sym": "sym", "as_of": "as_of"}

    body = b'{"results": [{"sym": "A", "stats": {"as_of": "2026-11-09"}}, {"sym": "B", "stats": null}]}'
    assert Summaries.frame(body).rows() == [("A", dt.date(2026, 11, 9))]


# --- records keyed by a field ---


class Bar(msgspec.Struct, frozen=True):
    symbol: str
    t: dt.datetime
    close: float


class AlpacaBars(Json):
    row = Bar
    records = "bars{}[]"
    labels = {"../{}": "symbol", "t": "t", "c": "close"}
    dates = {"t": "%Y-%m-%dT%H:%M:%SZ"}


def test_a_map_walk_reads_each_value_and_braces_name_its_key() -> None:
    body = b"""{"bars": {"AAPL": [{"t": "2026-11-09T14:30:00Z", "c": 230.5}],
                         "MSFT": [{"t": "2026-11-09T14:30:00Z", "c": 410}, {"t": "2026-11-09T14:31:00Z", "c": 411}]},
                "next_page_token": null}"""
    assert AlpacaBars.frame(body)["symbol"].to_list() == ["AAPL", "MSFT", "MSFT"]


class Dividend(msgspec.Struct, frozen=True):
    paid: dt.datetime
    amount: float


def test_a_map_key_is_typed_as_its_field() -> None:
    class Dividends(Json):  # Yahoo keys its dividends by their epoch seconds
        row = Dividend
        records = "chart.result[].events.dividends{}"
        labels = {"{}": "paid", "amount": "amount"}
        dates = {"paid": EPOCH_S}

    body = b'{"chart": {"result": [{"events": {"dividends": {"86400": {"amount": 0.25}}}}]}}'
    assert Dividends.frame(body).rows() == [(dt.datetime(1970, 1, 2), 0.25)]


class Fact(msgspec.Struct, frozen=True):
    concept: str
    unit: str
    end: dt.date
    value: float
    frame: str | None


def test_sec_company_facts_read_through_three_maps() -> None:
    class Facts(Json):
        row = Fact
        records = "facts.us-gaap{}.units{}[]"
        labels = {"../../{}": "concept", "../{}": "unit", "end": "end", "val": "value", "frame": "frame"}

    body = b"""{"cik": 1, "facts": {"us-gaap": {"Assets": {"label": "Assets", "units": {"USD": [
        {"end": "2025-12-31", "val": 10, "frame": "CY2025Q4I"}, {"end": "2026-03-31", "val": 11}]}}}}}"""
    assert Facts.frame(body).rows() == [
        ("Assets", "USD", dt.date(2025, 12, 31), 10.0, "CY2025Q4I"),
        ("Assets", "USD", dt.date(2026, 3, 31), 11.0, None),
    ]


# --- positional records ---


class Ticker(msgspec.Struct, frozen=True):
    cik: int
    name: str
    ticker: str
    exchange: str | None


class SecTickers(Json):
    row = Ticker
    records = "data[]"
    labels = {"[0]": "cik", "[1]": "name", "[2]": "ticker", "[3]": "exchange"}


def test_an_array_record_reads_by_position() -> None:
    body = (
        b'{"fields": ["cik", "name", "ticker", "exchange"],'
        b' "data": [[320193, "Apple Inc.", "AAPL", "Nasdaq"], [2110, "Fund", "LACAX", null]]}'
    )
    assert SecTickers.frame(body).rows() == [(320193, "Apple Inc.", "AAPL", "Nasdaq"), (2110, "Fund", "LACAX", None)]


@pytest.mark.parametrize("record", [b'[1, "A", "X"]', b'[1, "A", "X", "N", "extra"]'])
def test_an_array_record_of_another_width_raises(record: bytes) -> None:
    with pytest.raises(UnknownLayoutError, match="length"):
        SecTickers.frame(b'{"data": [' + record + b"]}")


class Point(msgspec.Struct, frozen=True):
    series: str
    period: dt.date
    value: float | None


def test_positional_pairs_under_their_series() -> None:
    class EiaSeries(Json):  # EIA's v1 shape: each series' data a list of [period, value] pairs
        row = Point
        records = "series[].data[]"
        labels = {"../series_id": "series", "[0]": "period", "[1]": "value"}
        dates = {"period": "%Y%m%d"}

    body = b'{"series": [{"series_id": "PET.W", "data": [["20261030", 1111], ["20261023", null]]}]}'
    assert EiaSeries.frame(body).rows() == [
        ("PET.W", dt.date(2026, 10, 30), 1111.0),
        ("PET.W", dt.date(2026, 10, 23), None),
    ]


def test_an_unnamed_position_needs_no_label() -> None:
    class Kline(msgspec.Struct, frozen=True):
        opened: dt.datetime
        close: float

    class Klines(Json):  # a crypto exchange's candles: twelve positions, two read
        row = Kline
        records = "[]"
        labels = {"[0]": "opened", "[4]": "close", "[11]": None}
        dates = {"opened": EPOCH_MS}

    body = b'[[0, "1", "2", "0.5", "1.5", "9", 59999, "1", 3, "1", "1", "0"]]'
    assert Klines.frame(body).rows() == [(dt.datetime(1970, 1, 1), 1.5)]


# --- what a layout cannot say ---


@pytest.mark.parametrize(
    ("records", "labels", "says"),
    [
        ("data[]", {"../../x": "symbol", "quote.bid": "bid", "quote.ask": "ask"}, "steps out past the body"),
        ("data[]", {"{}": "symbol", "quote.bid": "bid", "quote.ask": "ask"}, "no map walk"),
        ("data[]", {"../data": "symbol", "quote.bid": "bid", "quote.ask": "ask"}, "both"),
        ("data..x[]", {"symbol": "symbol", "quote.bid": "bid", "quote.ask": "ask"}, "empty key"),
        ("data[]", {"symbol": "symbol", "quote.bid": "bid", "quote[0]": "ask"}, "both"),
        ("data[]", {"symbol": "symbol", "quote..bid": "bid", "quote.ask": "ask"}, "empty key"),
    ],
)
def test_a_path_the_body_cannot_have_is_refused_at_build(records: str, labels: dict[str, str], says: str) -> None:
    with pytest.raises(TypeError, match=says):
        type("Bad", (Json,), {"row": Quote, "records": records, "labels": labels})
