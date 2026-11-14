"""Each reader on a real file its publisher wrote, as recorded: what a file built in a test does not have.

The files in ``tests/fixtures`` are whole response bodies from prism-clients' cassettes (``tests/fixtures/README.md``
names each one's source). Every value asserted here was checked against the file's raw cells read without flatread
(xlrd, ``zipfile`` and ``ElementTree``) when the file was added. Each file's SHA-256 is pinned, so a file stays as its
publisher wrote it: a newer copy is a new fixture, not an edit.
"""

import datetime as dt
import hashlib
import re
from pathlib import Path
from typing import Annotated

import msgspec
import polars as pl
import pytest

from flatread.archives import ZippedFile
from flatread.fields import Format, Label
from flatread.layout import LongFile
from flatread.markup import XmlFile
from flatread.pdf import text_layer
from flatread.sheets import BiffSheet, GroupedSheet, NotedSheet, SheetsFile

_FIXTURES = Path(__file__).parent / "fixtures"

_SHA256 = {
    "eia_ngsstats.xls": "048b07b1953af214021cb705231f46e14bda429f0af3d1ffde1316e088af30ab",
    "eia_wpsr_errata.xlsx": "4592e46f7c450f4e481405f01e732c6f07691158262aec0bda825b1bbdb8649f",
    "house_gift_travel_2018.zip": "36488d90f1c7a8141fb4ca537e6af27759eeb91e12d20b61efba9374363e9452",
    "house_ptr_20035492.pdf": "733d2c9c244f1c4a075dfa67412b12eafbf13b59ace93ac0d4d7dd87686dcefb",
    "philadelphia_fed_spf_error_statistics_cpi.xls": "71f02242b0192f948cce27fbb22c38dff746fdb594455ef950d7680445158433",
}


def _body(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


def test_every_fixture_is_the_file_as_recorded() -> None:
    files = sorted(path.name for path in _FIXTURES.iterdir() if path.name != "README.md")
    assert files == sorted(_SHA256)
    assert {name: hashlib.sha256(_body(name)).hexdigest() for name in files} == _SHA256


# --- An xls (BIFF8) workbook of a sheet a year, each grouped under headings, notes below the older ones ---------------


class _Week(msgspec.Struct, frozen=True, gc=False):
    report_date: Annotated[dt.date, Label("Report Date")]
    date_last_year: Annotated[dt.date | None, Label("Date Last Year")]
    statistic: str
    region: str
    value: float


class _Statistic(msgspec.Struct, frozen=True, gc=False):
    year: int
    report_date: Annotated[dt.date, Label("Report Date")]
    date_last_year: Annotated[dt.date | None, Label("Date Last Year")]
    statistic: str
    region: str
    value: float


class _StatsSheet(GroupedSheet[_Week], NotedSheet[_Week]):
    _groups_row = 2
    _beside = ("date_last_year",)


_STATS = SheetsFile(
    _Statistic,
    sheets=re.compile(r"ngsstats (?P<year>\d{4}) \(\d{4}-\d{4}\) ?"),  # the 2015 sheet's name ends with a space
    each=lambda sheet: _StatsSheet(_Week, sheet=sheet, header_row=3),
)


def test_eia_natural_gas_statistics_read_every_sheet_long() -> None:
    frame = _STATS.frame(_body("eia_ngsstats.xls"))
    # 52 weeks of 4 statistics of 8 regions a year; 2016 and 2021 report 53 weeks.
    assert dict(frame.group_by("year").len().sort("year").rows()) == {
        year: 53 * 32 if year in {2016, 2021} else 52 * 32 for year in range(2015, 2027)
    }
    assert frame.row(0) == (
        2026, dt.date(2026, 1, 2), dt.date(2025, 1, 2), "5-Year Average (2021-2025)", " East", 745.0
    )  # fmt: skip
    assert frame.row(-1) == (
        2015, dt.date(2015, 12, 25), dt.date(2014, 12, 25),
        "Working Gas Stock Last Year (2014)", "Total Lower 48", 3224.0,
    )  # fmt: skip
    # The headings and labels as EIA writes them: a label's leading space, and a heading misspelled some years.
    assert frame["region"].unique(maintain_order=True).to_list()[0] == " East"
    statistics = frame.filter(pl.col("region") == " East").unique("statistic", maintain_order=True)
    assert statistics.filter(pl.col("year") == 2026)["statistic"].to_list()[1] == "5-Year Maxium (2021-2025)"
    assert statistics.filter(pl.col("year") == 2024)["statistic"].to_list()[1] == "5-Year Maximum (2019-2023)"


# --- An Excel-saved xlsx: a table under titles, notes below it --------------------------------------------------------


class _Erratum(msgspec.Struct, frozen=True, gc=False):
    tables: Annotated[str, Label("Tables")]
    region: Annotated[str, Label("Region")]
    petroleum_product: Annotated[str, Label("Petroleum product")]
    supply_type: Annotated[str, Label("Supply type")]
    units: Annotated[str, Label("Units")]
    week_ending: Annotated[dt.date, Label("Week ending")]
    published: Annotated[float, Label("Published")]
    corrected: Annotated[float, Label("Corrected")]
    difference: Annotated[float, Label("Difference")]


def test_eia_errata_read_the_table_and_drop_its_notes() -> None:
    rows = NotedSheet(_Erratum, sheet="Appendix E", header_row=5).rows(_body("eia_wpsr_errata.xlsx"))
    assert len(rows) == 18  # sheet rows 6 to 23; the source and note lines under them are not rows
    assert rows[0] == _Erratum(
        "6, 9", "Midwest (PADD 2)", "Propane/Propylene", "Stocks", "Million barrels", dt.date(2023, 11, 10),
        26.947, 26.419, -0.528,
    )  # fmt: skip
    assert rows[-1] == _Erratum(
        "1, 9", "U.S.", "Propane/Propylene", "Product Supplied", "Thousand barrels per day", dt.date(2023, 12, 15),
        1231.0, 1231.0, 0.0,
    )  # fmt: skip


# --- An Excel 3 (BIFF3) workbook, which calamine cannot open, its gaps error cells ------------------------------------


class _Forecast(msgspec.Struct, frozen=True, gc=False):
    quarter: Annotated[str, Label("Date")]
    series: str
    value: float | None


class _Forecasts(LongFile[_Forecast], BiffSheet[_Forecast]):
    _keys = re.compile(r"(?:SPF|IAR|NC|DAR|DARM)for_Step[1-5]|Realiz[1-5]")


def test_philadelphia_fed_forecast_errors_read_an_excel_3_workbook() -> None:
    frame = _Forecasts(_Forecast, sheet="Sheet 1", null_values=["#N/A"]).frame(
        _body("philadelphia_fed_spf_error_statistics_cpi.xls")
    )
    assert frame.shape == (232 * 30, 3)  # 1968:04 to 2026:03, 30 series each
    assert frame["value"].null_count() == 3473  # an #N/A error cell is no value
    assert frame.row(0) == ("1968:04", "SPFfor_Step1", None)
    quarter = dict(frame.filter(pl.col("quarter") == "2016:01").select("series", "value").rows())
    # Every digit the file holds, as xlrd reads its number cells.
    assert (quarter["SPFfor_Step1"], quarter["IARfor_Step1"], quarter["Realiz1"]) == (
        0.4, 1.7416730931509579, -0.3121112832365269
    )  # fmt: skip
    assert frame.row(-1) == ("2026:03", "Realiz5", None)


# --- A zip of an XML table beside a text copy of it -------------------------------------------------------------------

_MONTH_DAY_YEAR = Format("%-m/%-d/%Y")


class _Travel(msgspec.Struct, frozen=True, gc=False):
    doc_id: Annotated[str, Label("DocID")]
    filer_name: Annotated[str | None, Label("FilerName")]
    member_name: Annotated[str, Label("MemberName")]
    state: Annotated[str | None, Label("State")]
    district: Annotated[str | None, Label("District")]
    year: Annotated[int, Label("Year")]
    destination: Annotated[str | None, Label("Destination")]
    filing_type: Annotated[str, Label("FilingType")]
    departure_date: Annotated[dt.date | None, Label("DepartureDate"), _MONTH_DAY_YEAR]
    return_date: Annotated[dt.date | None, Label("ReturnDate"), _MONTH_DAY_YEAR]
    travel_sponsor: Annotated[str | None, Label("TravelSponsor")]


class _HouseZip(ZippedFile[_Travel], XmlFile[_Travel]):
    _member = re.compile(r"[^/]*\.xml")
    _members = 2


def test_house_gift_travel_reads_the_zips_xml_member() -> None:
    rows = _HouseZip(_Travel, root="GiftTravel", record="Travel").rows(_body("house_gift_travel_2018.zip"))
    assert [row.doc_id for row in rows] == ["500019230", "500019337", "500023054", "500023063", "500023066"]
    assert rows[0] == _Travel(
        "500019230", "Lindley Kratovil Sherer", "Stefanik, Elise M.", "NY", "21", 2018, "Cambridge, MD", "Amendment",
        dt.date(2018, 3, 8), dt.date(2018, 3, 9), "Congressional Institute",
    )  # fmt: skip
    # 2018's zip holds filings the House dates 2020, as written.
    assert [row.year for row in rows] == [2018, 2018, 2020, 2020, 2020]
    assert rows[-1].travel_sponsor == "US Association of Former Members of Congress"


# --- A House periodic transaction report: its text layer as the page draws it -----------------------------------------


@pytest.fixture(scope="module")
def ptr_lines() -> list[tuple[int, float, str]]:
    return [(line.page, round(line.top, 2), line.text) for line in text_layer(_body("house_ptr_20035492.pdf"))]


def test_a_house_ptr_reads_its_lines_as_drawn(ptr_lines: list[tuple[int, float, str]]) -> None:
    assert len(ptr_lines) == 60
    assert {page for page, _, _ in ptr_lines} == {0, 1}
    # The first transaction, each cell a segment of its own on the row's baseline, the asset's ticker under it.
    assert ptr_lines[20:26] == [
        (0, 466.03, "SP"),
        (0, 466.03, "Broadcom Inc. - Common Stock"),
        (0, 455.53, "(AVGO) [ST]"),
        (0, 466.03, "P"),
        (0, 466.03, "08/12/2026 09/15/2026"),
        (0, 466.03, "$1,001 - $15,000"),
    ]
    # The filing's id is drawn last on its page, though it sits at the page's top.
    assert ptr_lines[55] == (0, 718.78, "Filing ID #20035492")


def test_a_house_ptr_drops_the_nul_its_small_capitals_map_to(ptr_lines: list[tuple[int, float, str]]) -> None:
    # The House's fonts map their small-capital glyphs to NUL in ToUnicode: "Periodic Transaction Report" keeps its
    # capitals, "Filer Information" and "Filing Status" theirs.
    assert [text for _, _, text in ptr_lines[:3:2]] == ["P T R", "F I"]
    assert ptr_lines[26] == (0, 437.93, "F S: New")
    assert not any("\x00" in text for _, _, text in ptr_lines)
