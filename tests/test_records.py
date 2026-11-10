import datetime as dt
from decimal import Decimal

import msgspec
import pytest

from flatread.errors import CellError, UnknownLayoutError
from flatread.records import Json


class Bar(msgspec.Struct, frozen=True):
    symbol: str
    t: dt.datetime
    close: float
    volume: int | None


class Bars(Json):
    row = Bar
    records = "results[].bars"
    labels = {"S": "symbol", "t": "t", "c": "close", "v": "volume"}
    dates = {"t": "%Y-%m-%dT%H:%M:%SZ"}


BARS = b"""{"results": [
  {"bars": [{"S": "AAPL", "t": "2026-11-09T14:30:00Z", "c": 230.5, "v": 100},
            {"S": "AAPL", "t": "2026-11-09T14:31:00Z", "c": 231, "v": null}]},
  {"bars": [{"S": "MSFT", "t": "2026-11-09T14:30:00Z", "c": 410.25, "extra": 1}]}
], "next_page_token": "abc"}"""


def test_records_at_a_nested_path() -> None:
    frame = Bars.frame(BARS)
    assert frame.rows() == [
        ("AAPL", dt.datetime(2026, 11, 9, 14, 30), 230.5, 100),
        ("AAPL", dt.datetime(2026, 11, 9, 14, 31), 231.0, None),
        ("MSFT", dt.datetime(2026, 11, 9, 14, 30), 410.25, None),
    ]


def test_a_top_level_list() -> None:
    class Flat(Bars):
        records = ""

    assert Flat.frame(b'[{"S": "A", "t": "2026-11-09T14:30:00Z", "c": 1, "v": 2}]').height == 1


class Quote(msgspec.Struct, frozen=True):
    symbol: str
    price: float
    as_of: dt.date


def test_envelope_values_fill_every_row() -> None:
    class Quotes(Json):
        row = Quote
        records = "data"
        labels = {"sym": "symbol", "px": "price"}
        envelope = {"meta.as_of": "as_of"}

    body = b'{"meta": {"as_of": "2026-11-09"}, "data": [{"sym": "A", "px": 1.5}, {"sym": "B", "px": 2}]}'
    assert Quotes.frame(body)["as_of"].to_list() == [dt.date(2026, 11, 9)] * 2


class Debt(msgspec.Struct, frozen=True):
    record_date: dt.date
    amount: Decimal | None
    count: int


class TreasuryDebt(Json):
    row = Debt
    records = "data"
    labels = {"record_date": "record_date", "debt_held": "amount", "src_line_nbr": "count"}
    missing = frozenset({"", "null"})


def test_treasury_strings_and_null_token_read_in_text_mode() -> None:
    assert TreasuryDebt._native is False
    body = (
        b'{"data": [{"record_date": "2026-11-07", "debt_held": "123.45", "src_line_nbr": "1"},'
        b' {"record_date": "2026-11-06", "debt_held": "null", "src_line_nbr": "2"}]}'
    )
    frame = TreasuryDebt.frame(body)
    assert frame["amount"].to_list() == [Decimal("123.45"), None]
    assert frame["count"].to_list() == [1, 2]


def test_native_mode_is_chosen_when_it_can_be() -> None:
    assert Bars._native is True


def test_a_wrong_type_in_native_mode_names_the_record() -> None:
    bad = BARS.replace(b'"c": 231,', b'"c": "two",')  # a number written as a string reads; this is no number
    with pytest.raises(UnknownLayoutError, match=r"\$\.results\[0\]\.bars\[1\]\.c"):
        Bars.frame(bad)


def test_a_bad_cell_in_text_mode_names_the_record() -> None:
    body = b'{"data": [{"record_date": "2026-11-07", "debt_held": "1", "src_line_nbr": "x"}]}'
    with pytest.raises(CellError, match="record 1: the 'src_line_nbr' cell 'x'"):
        TreasuryDebt.frame(body)


def test_a_missing_required_key_raises() -> None:
    with pytest.raises(UnknownLayoutError, match="missing required field `c`"):
        Bars.frame(b'{"results": [{"bars": [{"S": "A", "t": "2026-11-09T14:30:00Z"}]}]}')


def test_page_returns_frame_and_cursor() -> None:
    class Paged(Bars):
        next = "next_page_token"

    frame, cursor = Paged.page(BARS)
    assert frame.height == 3
    assert cursor == "abc"


@pytest.mark.parametrize("tail", [b'"next_page_token": null', b'"next_page_token": ""', b'"other": 1'])
def test_an_absent_or_empty_cursor_is_none(tail: bytes) -> None:
    class Paged(Bars):
        next = "next_page_token"
        min_rows = 0  # a page may hold no rows, as Alpaca's do behind a live cursor

    _, cursor = Paged.page(b'{"results": [], ' + tail + b"}")
    assert cursor is None


def test_not_json_raises() -> None:
    with pytest.raises(UnknownLayoutError, match="JSON"):
        Bars.frame(b"<html>")
