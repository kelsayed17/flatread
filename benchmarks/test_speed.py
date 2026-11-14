"""One benchmark per read path, on synthetic bodies, for CodSpeed: ``pytest benchmarks/test_speed.py --codspeed``.

The bodies are made here, at import, so only the read is measured. CodSpeed guards the paths from one change to the
next. Sizes are small: CodSpeed's simulation runs every instruction.
"""

import datetime as dt
import re
from typing import TYPE_CHECKING, Annotated

import msgspec
import pytest

from flatread.delimited import CsvFile
from flatread.fields import Format, Label, Span
from flatread.fixed import FixedWidthFile
from flatread.layout import LongFile
from flatread.sheets import SheetFile
from tests.support.xlsx import workbook

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_codspeed import BenchmarkFixture


class Hpi(msgspec.Struct, frozen=True):
    year: Annotated[int, Label("Year")]
    month: Annotated[int, Label("Month")]
    geo_name: Annotated[str, Label("GEO_Name")]
    geo_code: Annotated[int | None, Label("GEO_Code")]
    index_nsa: Annotated[float, Label("Index_NSA")]


HPI = CsvFile(Hpi, null_values=["."])


def _hpi(i: int) -> bytes:
    code = "." if i % 7 == 0 else 10000 + i % 400
    return f"{1975 + i // 12 % 50},{i % 12 + 1},Place {i % 400},{code},{100 + i / 997:.4f}\n".encode()


_CSV = b"Year,Month,GEO_Name,GEO_Code,Index_NSA\n" + b"".join(map(_hpi, range(100_000)))
_QUOTED = b"Year,Month,GEO_Name,GEO_Code,Index_NSA\n" + b"".join(
    f'{1975 + i % 50},{i % 12 + 1},"Place {i}\nline two",{i},{i / 7:.4f}\n'.encode() for i in range(10_000)
)


class Tenor(msgspec.Struct, frozen=True):
    day: Annotated[dt.date, Label("DATE"), Format("%Y-%m-%d")]
    tenor: str
    value: float


class _TenorsCsv(LongFile[Tenor], CsvFile[Tenor]):
    _keys = re.compile(r"Y\d{2}")


TENORS = _TenorsCsv(Tenor)

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
    name: Annotated[str, Label("Name")]
    rate: Annotated[float | None, Label("Rate")]
    count: Annotated[int, Label("Count")]


RATES = SheetFile(Rate, sheet="S")
RATES_WITH_TOKENS = SheetFile(Rate, sheet="S", null_values=["."])  # a token in a number column

_SHEET = workbook({"S": [["Name", "Rate", "Count"], *([f"n{i}", i / 7, i] for i in range(10_000))]})
_SHEET_TOKENS = workbook({
    "S": [["Name", "Rate", "Count"], *([f"n{i}", "." if i % 9 == 0 else i / 7, i] for i in range(10_000))]
})  # fmt: skip


class Station(msgspec.Struct, frozen=True):
    station: Annotated[str, Span(0, 11)]
    year: Annotated[int, Span(11, 15)]
    value: Annotated[float | None, Span(15, 23)]


class _StationsFixed(FixedWidthFile[Station]):
    _header = False


STATIONS = _StationsFixed(Station)

_FIXED = b"".join(
    f"USC{i % 10_000:08}{1900 + i % 120}{'' if i % 11 == 0 else f'{i / 10:.1f}':>8}\n".encode() for i in range(50_000)
)

CASES: dict[str, tuple[Callable[[bytes], object], bytes]] = {
    "csv": (HPI.frame, _CSV),
    "csv_quoted": (HPI.frame, _QUOTED),
    "csv_long": (TENORS.frame, _WIDE),
    "sheet": (RATES.frame, _SHEET),
    "sheet_tokens": (RATES_WITH_TOKENS.frame, _SHEET_TOKENS),
    "fixed_width": (STATIONS.frame, _FIXED),
    "csv_rows": (HPI.rows, _CSV),
}


@pytest.mark.parametrize("case", CASES)
def test_read(benchmark: BenchmarkFixture, case: str) -> None:
    read, body = CASES[case]
    read(body)  # the body reads at all, and the first call's one-time costs stay out of the measure
    benchmark(read, body)
