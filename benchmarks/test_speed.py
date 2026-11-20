"""One benchmark per read path, on synthetic bodies, for CodSpeed: ``pytest benchmarks/test_speed.py --codspeed``.

The bodies are made here, at import, so only the read is measured. CodSpeed guards the paths from one change to the
next. Sizes are small: CodSpeed's simulation runs every instruction.
"""

import datetime as dt
import io
import re
import typing
import zipfile
from typing import TYPE_CHECKING, Annotated

import msgspec
import pytest

from flatread import next as fr
from flatread.archives import ZippedFile
from flatread.delimited import CsvFile
from flatread.fields import Format, Label, Span
from flatread.fixed import FixedWidthFile
from flatread.layout import LongFile
from flatread.pdf import LaidOutPdf
from flatread.sdmx import SdmxObservations
from flatread.sheets import SheetFile
from tests.support.pdf import pdf
from tests.support.xlsx import workbook

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from pytest_codspeed import BenchmarkFixture

    from flatread.pdf import PdfLine


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


def _zipped(name: str, data: bytes) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, data)
    return out.getvalue()


class _ZippedHpi(ZippedFile[Hpi], CsvFile[Hpi]):
    _member = re.compile(r"hpi\.csv")


ZIPPED_HPI = _ZippedHpi(Hpi, null_values=["."])
_ZIPPED = _zipped("hpi.csv", _CSV)


class Observation(msgspec.Struct, frozen=True):
    series_name: str
    time_period: Annotated[dt.date, Format("%Y-%m")]
    obs_value: float | None
    obs_status: str


_SDMX_ELEMENTS = {
    "root": "message:MessageGroup",
    "data_set": "x:DataSet",
    "observation": "x:Obs",
    "annotations": "x:Annotations",
}


class _Observations(SdmxObservations[Observation]):
    _prefixes = {"x": "urn:x"}  # noqa: RUF012 (a format's constant)
    _root, _data_set = _SDMX_ELEMENTS["root"], _SDMX_ELEMENTS["data_set"]
    _observation, _annotations = _SDMX_ELEMENTS["observation"], _SDMX_ELEMENTS["annotations"]
    _statuses = ("A", "M")
    _absent = ("",)
    _date_format = "%Y-%m"


OBSERVATIONS = _Observations(Observation, "D")


def _series(s: int) -> bytes:
    observations = b"".join(
        b'<x:Obs TIME_PERIOD="%d-%02d" OBS_VALUE="%b" OBS_STATUS="%b"/>'
        % (2000 + i // 12, i % 12 + 1, b"" if i % 13 == 0 else b"%d.%d" % (s, i), b"M" if i % 13 == 0 else b"A")
        for i in range(120)
    )
    return b'<k:Series SERIES_NAME="S%d">%b</k:Series>' % (s, observations)


_SDMX = (
    b'<message:MessageGroup xmlns:message="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/message"'
    b' xmlns:common="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/common" xmlns:x="urn:x">'
    b'<x:DataSet id="D" xmlns:k="urn:k">' + b"".join(map(_series, range(200))) + b"</x:DataSet></message:MessageGroup>"
)


class Trade(msgspec.Struct, frozen=True):
    asset: Annotated[str, Label("Asset")]
    shares: Annotated[int, Label("Shares")]


class _Trades(LaidOutPdf[Trade]):
    _row_gap = 14.0

    @typing.override
    def _records(self, lines: Sequence[PdfLine]) -> list[list[str]]:
        return _trades(lines, self._edges, self._column_cells)


def _trades(
    lines: Sequence[PdfLine],
    edges_of: Callable[[PdfLine, Sequence[str]], list[float] | None],
    cells_of: Callable[[Sequence[PdfLine], Sequence[float]], list[str]],
) -> list[list[str]]:
    """Each page's rows under its header line, a row a line: the one table both versions read."""
    records: list[list[str]] = []
    edges: list[float] | None = None
    for line in lines:
        if line.text.startswith("Asset"):
            edges = edges_of(line, ["Asset", "Shares"])
        elif edges is not None:
            records.append(cells_of([line], edges))
    return records


TRADES = _Trades(Trade)


def _page(p: int) -> list[tuple[float, float, str]]:
    """Return page *p* of the trades table: its header line, then 45 rows, a row a line."""
    rows = [((72.0, 720.0 - r * 14, f"Name {p}-{r}"), (300.0, 720.0 - r * 14, str(p * 100 + r))) for r in range(45)]
    return [(72.0, 740.0, "Asset"), (300.0, 740.0, "Shares"), *(cell for row in rows for cell in row)]


_PDF = pdf(*map(_page, range(5)))

# flatread.next, the 8.0 prototype, reading the same bodies into the same structs: its cost beside 7.x's.
NEXT_HPI = fr.Reader(Hpi, fr.Csv(), null_values=["."])
# misquoted reads every body with Csv._whole, the pure-Python csv fallback, which is most of the read's cost.
NEXT_HPI_MISQUOTED = fr.Reader(Hpi, fr.Csv(misquoted=True), null_values=["."])
NEXT_TENORS = fr.Reader(Tenor, fr.Csv(), shape=fr.Long(keys=re.compile(r"Y\d{2}")))
NEXT_RATES = fr.Reader(Rate, fr.Sheet(sheet="S"))
NEXT_RATES_WITH_TOKENS = fr.Reader(Rate, fr.Sheet(sheet="S"), null_values=["."])
NEXT_ZIPPED_HPI = fr.Reader(Hpi, fr.Csv(), null_values=["."], source=fr.Zip(re.compile(r"hpi\.csv")))
NEXT_OBSERVATIONS = fr.Reader(
    Observation,
    fr.Sdmx(
        data_set="D",
        root=_SDMX_ELEMENTS["root"],
        data_set_element=_SDMX_ELEMENTS["data_set"],
        observation=_SDMX_ELEMENTS["observation"],
        annotations=_SDMX_ELEMENTS["annotations"],
        prefixes={"x": "urn:x"},
    ),
    shape=fr.Observations(statuses=("A", "M"), absent=("",), date_format="%Y-%m"),
)


class _NextTrades:
    def records(
        self, lines: Sequence[PdfLine], layout: fr.Layout | None, read: fr.ReadContext
    ) -> list[Mapping[str, str]]:
        del read
        assert layout is not None  # noqa: S101 (the reader's)
        return [{"asset": asset, "shares": shares} for asset, shares in _trades(lines, layout.edges, layout.cells)]


NEXT_TRADES = fr.Reader(Trade, fr.Pdf(_NextTrades(), layout=fr.Layout(row_gap=14.0)))

_CSV_TEXT = _CSV.decode()


def next_whole(body: bytes) -> fr.Lines:
    """Csv._whole's parse=True fallback alone, on text decoded at import: a private-path micro-benchmark."""
    return fr.Csv()._whole(_CSV_TEXT, body)  # noqa: SLF001


CASES: dict[str, tuple[Callable[[bytes], object], bytes]] = {
    "csv": (HPI.frame, _CSV),
    "csv_quoted": (HPI.frame, _QUOTED),
    "csv_long": (TENORS.frame, _WIDE),
    "sheet": (RATES.frame, _SHEET),
    "sheet_tokens": (RATES_WITH_TOKENS.frame, _SHEET_TOKENS),
    "fixed_width": (STATIONS.frame, _FIXED),
    "csv_rows": (HPI.rows, _CSV),
    "zip_csv": (ZIPPED_HPI.frame, _ZIPPED),
    "sdmx": (OBSERVATIONS.frame, _SDMX),
    "pdf": (TRADES.frame, _PDF),
    "next_csv": (NEXT_HPI.frame, _CSV),
    "next_csv_quoted": (NEXT_HPI.frame, _QUOTED),
    "next_csv_long": (NEXT_TENORS.frame, _WIDE),
    "next_sheet": (NEXT_RATES.frame, _SHEET),
    "next_sheet_tokens": (NEXT_RATES_WITH_TOKENS.frame, _SHEET_TOKENS),
    "next_csv_rows": (NEXT_HPI.rows, _CSV),
    "next_zip_csv": (NEXT_ZIPPED_HPI.frame, _ZIPPED),
    "next_sdmx": (NEXT_OBSERVATIONS.frame, _SDMX),
    "next_pdf": (NEXT_TRADES.frame, _PDF),
    "next_csv_misquoted": (NEXT_HPI_MISQUOTED.frame, _CSV),
    "next_csv_quoted_misquoted": (NEXT_HPI_MISQUOTED.frame, _QUOTED),
    "next_csv_whole": (next_whole, _CSV),
}


@pytest.mark.parametrize("case", CASES)
def test_read(benchmark: BenchmarkFixture, case: str) -> None:
    read, body = CASES[case]
    read(body)  # the body reads at all, and the first call's one-time costs stay out of the measure
    benchmark(read, body)
