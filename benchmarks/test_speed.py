"""One benchmark per read path, on synthetic bodies, for CodSpeed: ``pytest benchmarks/test_speed.py --codspeed``.

The bodies are made here, at import, so only the read is measured. ``bench.py`` times the same paths against
prism-clients' hand-written readers on recorded bodies; those stay out of this public repository, so CodSpeed guards
the paths from one change to the next instead. Sizes are small: CodSpeed's simulation runs every instruction.
"""

import datetime as dt
from decimal import Decimal
from typing import TYPE_CHECKING

import msgspec
import pytest

import flatread
from tests.xlsx import workbook

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_codspeed import BenchmarkFixture


class Hpi(msgspec.Struct, frozen=True):
    year: int
    month: int
    geo_name: str
    geo_code: int | None
    index_nsa: float


class HpiCsv(flatread.Csv):
    row = Hpi
    labels = {
        "Year": "year",
        "Month": "month",
        "GEO_Name": "geo_name",
        "GEO_Code": "geo_code",
        "Index_NSA": "index_nsa",
    }
    missing = frozenset({"", "."})


def _hpi(i: int) -> bytes:
    code = "." if i % 7 == 0 else 10000 + i % 400
    return f"{1975 + i // 12 % 50},{i % 12 + 1},Place {i % 400},{code},{100 + i / 997:.4f}\n".encode()


_CSV = b"Year,Month,GEO_Name,GEO_Code,Index_NSA\n" + b"".join(map(_hpi, range(100_000)))
_QUOTED = b"Year,Month,GEO_Name,GEO_Code,Index_NSA\n" + b"".join(
    f'{1975 + i % 50},{i % 12 + 1},"Place {i}\nline two",{i},{i / 7:.4f}\n'.encode() for i in range(10_000)
)


class Tenor(msgspec.Struct, frozen=True):
    day: dt.date
    tenor: str
    value: float


class TenorsCsv(flatread.Csv):
    row = Tenor
    labels = {"DATE": "day"}
    long = flatread.Long(name="tenor", value="value", names=r"Y\d{2}")


_WIDE = (
    b"DATE,"
    + b",".join(f"Y{n:02}".encode() for n in range(1, 41))
    + b"\n"
    + b"".join(
        (dt.date(2000, 1, 1) + dt.timedelta(days=i)).isoformat().encode()
        + b"".join(f",{n + i / 1000:.3f}".encode() for n in range(1, 41))
        + b"\n"
        for i in range(2_500)
    )
)


class Rate(msgspec.Struct, frozen=True):
    name: str
    rate: float | None
    count: int


class RateSheet(flatread.Sheet):
    row = Rate
    labels = {"Name": "name", "Rate": "rate", "Count": "count"}


class RateSheetMixed(RateSheet):
    missing = frozenset({"", "."})  # a token in a number column: the two-load fallback


_SHEET = workbook({"S": [["Name", "Rate", "Count"], *([f"n{i}", i / 7, i] for i in range(10_000))]})
_SHEET_MIXED = workbook({
    "S": [["Name", "Rate", "Count"], *([f"n{i}", "." if i % 9 == 0 else i / 7, i] for i in range(10_000))]
})  # fmt: skip


class Bar(msgspec.Struct, frozen=True):
    symbol: str
    close: float
    volume: int
    amount: Decimal | None


class BarsJson(flatread.Json):
    row = Bar
    records = "data"
    labels = {"s": "symbol", "c": "close", "v": "volume", "a": "amount"}


class BarsText(BarsJson):
    missing = frozenset({"", "null"})  # every value a string, as Treasury writes them: text mode


_JSON = msgspec.json.encode({"data": [{"s": f"S{i % 500}", "c": i / 3, "v": i, "a": i / 8} for i in range(30_000)]})
_JSON_TEXT = msgspec.json.encode({
    "data": [{"s": f"S{i % 500}", "c": f"{i / 3:.6f}", "v": str(i), "a": "null" if i % 5 == 0 else f"{i / 8}"}
             for i in range(30_000)]
})  # fmt: skip


class Station(msgspec.Struct, frozen=True):
    station: str
    year: int
    value: float | None


class StationsFixed(flatread.FixedWidth):
    row = Station
    columns = {"station": (1, 11), "year": (12, 15), "value": (16, 23)}
    missing_by_field = {"value": frozenset({" -9999.0"})}
    strip = True


_FIXED = b"".join(
    f"USC{i % 10_000:08}{1900 + i % 120}{-9999.0 if i % 11 == 0 else i / 10:8.1f}\n".encode() for i in range(50_000)
)

CASES: dict[str, tuple[Callable[[bytes], object], bytes]] = {
    "csv": (HpiCsv.frame, _CSV),
    "csv_quoted_fallback": (HpiCsv.frame, _QUOTED),
    "csv_long": (TenorsCsv.frame, _WIDE),
    "sheet": (RateSheet.frame, _SHEET),
    "sheet_fallback": (RateSheetMixed.frame, _SHEET_MIXED),
    "json_native": (BarsJson.frame, _JSON),
    "json_text": (BarsText.frame, _JSON_TEXT),
    "fixed_width": (StationsFixed.frame, _FIXED),
    "csv_rows": (HpiCsv.rows, _CSV),
}


@pytest.mark.parametrize("case", CASES)
def test_read(benchmark: BenchmarkFixture, case: str) -> None:
    read, body = CASES[case]
    read(body)  # the body reads at all, and the first call's one-time costs stay out of the measure
    benchmark(read, body)
