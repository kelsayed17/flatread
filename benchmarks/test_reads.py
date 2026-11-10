"""CodSpeed benchmarks: each layout base reading a synthetic body of a realistic size, plus the utilities.

Run with ``uv run python -m pytest benchmarks --codspeed``; without ``--codspeed`` each benchmark runs once as a test.
The bodies are generated here, deterministically, so no recording or network is needed.
"""

import datetime as dt
import io
import json
import pathlib
import random
import zipfile
from decimal import Decimal
from typing import TYPE_CHECKING

import msgspec
import polars as pl
import pytest

import flatread
from flatread import Csv, FixedWidth, Json, Line, Long, Pipe

if TYPE_CHECKING:
    from pytest_codspeed import BenchmarkFixture

ROWS = 20_000
MISSING_SHARE = 0.05  # of the generated cells written as a missing token
_START = dt.date(2000, 1, 1)


# --- delimited ------------------------------------------------------------------------------------------------------


class HpiRow(msgspec.Struct, frozen=True):
    year: int
    month: int
    geo_type: str
    geo_name: str
    geo_code: int | None
    index_nsa: float
    index_sa: float | None


class Fmhpi(Csv):
    row = HpiRow
    missing = frozenset({"", "."})
    labels = {
        "Year": "year", "Month": "month", "GEO_Type": "geo_type", "GEO_Name": "geo_name", "GEO_Code": "geo_code",
        "Index_NSA": "index_nsa", "Index_SA": "index_sa",
    }  # fmt: skip
    key = ("year", "month", "geo_name")


class FmhpiCbsa(Fmhpi):
    where = pl.col("geo_type") == "CBSA"


def _hpi(rows: int, seed: int = 0) -> bytes:
    rng = random.Random(seed)
    lines = ["Year,Month,GEO_Type,GEO_Name,GEO_Code,Index_NSA,Index_SA"]
    geos = [("US", "USA", "")] + [("State", f"S{i:02d}", "") for i in range(50)]
    geos += [("CBSA", f"Metro {i}", str(10000 + i)) for i in range(150)]
    for i in range(rows):
        geo_type, name, code = geos[i % len(geos)]
        month = i // len(geos)
        sa = "." if rng.random() < MISSING_SHARE else f"{rng.uniform(50, 400):.6f}"
        lines.append(f"{1975 + month // 12},{month % 12 + 1},{geo_type},{name},{code},{rng.uniform(50, 400):.6f},{sa}")
    return ("\n".join(lines) + "\n").encode()


HPI = _hpi(ROWS)


def test_csv_frame(benchmark: BenchmarkFixture) -> None:
    assert benchmark(Fmhpi.frame, HPI).height == ROWS


def test_csv_frame_where(benchmark: BenchmarkFixture) -> None:
    assert benchmark(FmhpiCbsa.frame, HPI).height > 0


def test_csv_rows(benchmark: BenchmarkFixture) -> None:
    assert len(benchmark(Fmhpi.rows, HPI)) == ROWS


def test_csv_multiline_quoted_fallback(benchmark: BenchmarkFixture) -> None:
    # A quoted field spanning lines sends the body through the stdlib csv module instead of pl.read_csv.
    body = HPI.replace(b",Metro 7,", b',"Metro\n7",', 1)
    assert benchmark(Fmhpi.frame, body).height == ROWS


class Trade(msgspec.Struct, frozen=True):
    trade_date: dt.date
    symbol: str
    price: Decimal
    shares: int
    venue: str | None
    report_date: dt.date


class FinraTrades(Pipe):
    row = Trade
    labels = {"Date": "trade_date", "Symbol": "symbol", "Price": "price", "Shares": "shares", "Venue": "venue"}
    dates = {"trade_date": "%Y%m%d"}
    strip = True
    preamble = Line(r"Report date: (?P<report>\d{4}-\d{2}-\d{2})", carry={"report": "report_date"})
    trailer = Line(r"Total records: (?P<count>\d+)", count="count")


def _trades(rows: int, seed: int = 1) -> bytes:
    rng = random.Random(seed)
    lines = ["Report date: 2026-11-09", "Date|Symbol|Price|Shares|Venue"]
    for i in range(rows):
        day = (_START + dt.timedelta(days=i % 5000)).strftime("%Y%m%d")
        venue = "" if i % 7 == 0 else f" V{i % 13} "
        lines.append(f"{day}|SYM{i % 500}|{rng.uniform(1, 500):.4f}|{rng.randint(1, 10_000)}|{venue}")
    lines.append(f"Total records: {rows}")
    return ("\n".join(lines) + "\n").encode()


TRADES = _trades(ROWS)


def test_pipe_preamble_trailer_dates_decimal(benchmark: BenchmarkFixture) -> None:
    assert benchmark(FinraTrades.frame, TRADES).height == ROWS


class Point(msgspec.Struct, frozen=True):
    day: dt.date
    series: str
    value: float


class WideSeries(Csv):
    row = Point
    labels = {"Date": "day"}
    long = Long(name="series", value="value", names=r"[A-Z]+\d*")
    missing = frozenset({"", "."})


def _wide(rows: int, series: int, seed: int = 2) -> bytes:
    rng = random.Random(seed)
    names = [f"S{i}" for i in range(series)]
    lines = [",".join(["Date", *names])]
    for i in range(rows):
        cells = ["." if rng.random() < MISSING_SHARE else f"{rng.gauss(0, 10):.4f}" for _ in names]
        lines.append(",".join([(_START + dt.timedelta(days=i)).isoformat(), *cells]))
    return ("\n".join(lines) + "\n").encode()


WIDE = _wide(2_000, 40)


def test_csv_long_melt(benchmark: BenchmarkFixture) -> None:
    assert benchmark(WideSeries.frame, WIDE).height > 0


# --- records and fixed width ----------------------------------------------------------------------------------------


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
    next = "next_page_token"


def _bars(rows: int, seed: int = 3) -> bytes:
    rng = random.Random(seed)
    start = dt.datetime(2026, 11, 9, 14, 30)  # noqa: DTZ001  # the provider writes UTC with a Z
    groups = []
    for g in range(rows // 500):
        bars = [
            {
                "S": f"SYM{g}",
                "t": (start + dt.timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "c": round(rng.uniform(1, 500), 4),
                "v": None if i % 11 == 0 else rng.randint(1, 100_000),
            }
            for i in range(500)
        ]
        groups.append({"bars": bars})
    return json.dumps({"results": groups, "next_page_token": "abc"}).encode()


BARS = _bars(ROWS)


def test_json_native_page(benchmark: BenchmarkFixture) -> None:
    frame, cursor = benchmark(Bars.page, BARS)
    assert frame.height == ROWS
    assert cursor == "abc"


class Debt(msgspec.Struct, frozen=True):
    record_date: dt.date
    amount: Decimal | None
    count: int


class TreasuryDebt(Json):
    row = Debt
    records = "data"
    labels = {"record_date": "record_date", "debt_held": "amount", "src_line_nbr": "count"}
    missing = frozenset({"", "null"})


def _debt(rows: int, seed: int = 4) -> bytes:
    rng = random.Random(seed)
    data = [
        {
            "record_date": (_START + dt.timedelta(days=i)).isoformat(),
            "debt_held": "null" if i % 9 == 0 else f"{rng.uniform(1e6, 1e9):.2f}",
            "src_line_nbr": str(i % 40 + 1),
        }
        for i in range(rows)
    ]
    return json.dumps({"data": data}).encode()


DEBT = _debt(ROWS)


def test_json_text_mode(benchmark: BenchmarkFixture) -> None:
    assert benchmark(TreasuryDebt.frame, DEBT).height == ROWS


class Volume(msgspec.Struct, frozen=True):
    symbol: str
    kind: str
    contracts: int


class OccVolume(FixedWidth):
    row = Volume
    columns = {"symbol": (2, 7), "kind": (8, 8), "contracts": (9, 16)}
    record = r"D.{15}"
    skip = r"H.*"
    strip = True


def _occ(rows: int, seed: int = 5) -> bytes:
    rng = random.Random(seed)
    lines = ["H20261109 header"]
    lines += [f"D{f'S{i % 900}':<6}{'CP'[i % 2]}{rng.randint(0, 99_999_999):08d}" for i in range(rows)]
    return ("\n".join(lines) + "\n").encode()


OCC = _occ(ROWS)


def test_fixed_width(benchmark: BenchmarkFixture) -> None:
    assert benchmark(OccVolume.frame, OCC).height == ROWS


# --- archives and sheets --------------------------------------------------------------------------------------------


def _zip(**members: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buffer.getvalue()


class ZippedHpi(Fmhpi):
    member = "fmhpi_master_file.csv"


ARCHIVE = _zip(**{"readme.txt": b"notes", "fmhpi_master_file.csv": HPI})


def test_zip_member(benchmark: BenchmarkFixture) -> None:
    assert benchmark(ZippedHpi.frame, ARCHIVE).height == ROWS


class AdsDay(msgspec.Struct, frozen=True):
    day: dt.date
    ads_index: float
    recession: int


ADS = (pathlib.Path(__file__).parents[1] / "tests" / "fixtures" / "workbooks" / "ads_index_sample.xlsx").read_bytes()


def test_sheet_xlsx(benchmark: BenchmarkFixture) -> None:
    class AdsIndex(flatread.Sheet):
        row = AdsDay
        labels = {"Date": "day", "ADS_Index": "ads_index", "RECBARS": "recession"}
        dates = {"day": "%Y:%m:%d"}

    assert benchmark(AdsIndex.frame, ADS).height > 0


# --- utilities ------------------------------------------------------------------------------------------------------


def test_report_clean_body(benchmark: BenchmarkFixture) -> None:
    assert benchmark(flatread.report, Fmhpi, HPI) is not None


def test_diff(benchmark: BenchmarkFixture) -> None:
    new = _hpi(ROWS, seed=7)
    assert benchmark(flatread.diff, Fmhpi, HPI, new) is not None


@pytest.mark.parametrize("rows", [100, 100_000])
def test_csv_frame_scaling(benchmark: BenchmarkFixture, rows: int) -> None:
    body = _hpi(rows)
    assert benchmark(Fmhpi.frame, body).height == rows
