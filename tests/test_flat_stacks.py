"""The readers' pieces composed as providers compose them, and the defensive branches only a provider ran.

Each stack is a provider's own (its class attributes and base order), on a small body built here: the pieces are each
tested alone in ``test_flat.py``, and here which one's ``_read``, ``_table`` or ``_clean`` runs first.
"""

import datetime as dt
import io
import re
import typing
import zipfile
from typing import Annotated

import msgspec
import pytest

from flatread.archives import Transcoded, ZippedFile
from flatread.core import Carried, Thousands, _Column
from flatread.delimited import (
    CsvFile,
    HeadersCsv,
    NamedCsv,
    PaddedCsv,
    PreambleCsv,
    SectionedCsv,
    SpilledCsv,
)
from flatread.errors import LayoutError, ReadError
from flatread.fields import Format, Label, Span
from flatread.fixed import FixedWidthFile
from flatread.found import FoundCsv
from flatread.layout import LongFile
from flatread.reports import LongReport
from flatread.sheets import BesideSheet, DatedSheet, SheetFile, SheetText
from tests.support.xlsx import workbook

if typing.TYPE_CHECKING:
    import polars as pl

_D1, _D2, _D3 = dt.date(2026, 1, 2), dt.date(2026, 1, 9), dt.date(2026, 1, 16)


# --- LongFile + DatedSheet: EIA's weekly history sheets ---


class _Regional(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Label("Date")]
    region: str
    value: float


class _RegionalSheet(LongFile[_Regional], DatedSheet[_Regional]):
    """eia's ``_Regional``: dates down, a column a region, notes under the dates, blank-looking cells empty."""

    _empty_rows = False
    _rows_required = False
    _bare_header_refused = True
    _spacers = True
    _trim = True
    _strip = True
    _keys = re.compile(r".+", re.DOTALL)


_REGIONAL = _RegionalSheet(_Regional, sheet="S")


class TestALongDatedSheet:
    def test_a_row_per_filled_cell_the_dates_ending_the_table_the_notes_dropped(self) -> None:
        body = workbook(
            {
                "S": [
                    ["Date", " East ", None, "West"],
                    [_D1, 1, None, " "],
                    [_D2, None, None, 3],
                    [_D3, " ", None, None],
                    ["Source: a note"],
                    ["Released weekly"],
                ]
            }
        )
        assert _REGIONAL.rows(body) == [_Regional(_D1, "East", 1.0), _Regional(_D2, "West", 3.0)]

    def test_a_cell_beside_the_notes_is_a_row_the_table_lost(self) -> None:
        body = workbook({"S": [["Date", "East"], [_D1, 1], ["Notes"], ["late", 2]]})
        with pytest.raises(ReadError, match=r"'S' sheet's row 4 holds a cell under its table"):
            _REGIONAL.rows(body)

    def test_dates_with_no_value_yet_are_an_empty_answer_but_a_bare_header_is_refused(self) -> None:
        assert _REGIONAL.rows(workbook({"S": [["Date", "East"], [_D1, None]]})) == []
        with pytest.raises(ReadError, match="the body is a header and no rows"):
            _REGIONAL.rows(workbook({"S": [["Date", "East"], ["Notes"]]}))


# --- ZippedFile + LongFile + PreambleCsv: the Fed's zipped wide CSVs ---


class _Series(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Label("Series Description"), Format("%Y-%m-%d")]
    series: str
    value: float | None


class _Long(LongFile[_Series], PreambleCsv[_Series]):
    """federal_reserve's ``_Long``: past any preamble, series by series, every line ended."""

    _keys = re.compile(r".+")
    _by_key = True

    def __init__(self, row: type[_Series]) -> None:
        super().__init__(row, null_values=("ND",), final_line_ending=True)


class _ZippedLong(ZippedFile[_Series], _Long):
    _member = re.compile(r"(?:.*/)?data\.csv")


def _zip(**members: bytes) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return out.getvalue()


_PREAMBLE = b'"A release, quoted",,\nUnit:,Percent,Percent\n\nSeries Description,A,B\n'
_WIDE = _PREAMBLE + b"2026-01-02,1.5,ND\n2026-01-09,2.5,3.5\n"


class TestAZippedLongPreambleCsv:
    def test_the_member_is_read_past_its_preamble_series_by_series(self) -> None:
        rows = _ZippedLong(_Series).rows(_zip(**{"README.txt": b"x", "FRB/data.csv": _WIDE}))
        assert rows == [
            _Series(_D1, "A", 1.5),
            _Series(_D2, "A", 2.5),
            _Series(_D1, "B", None),
            _Series(_D2, "B", 3.5),
        ]

    def test_the_line_ending_check_is_the_members_not_the_zips(self) -> None:
        with pytest.raises(ReadError, match="always ends its last line with a line ending: it was cut"):
            _ZippedLong(_Series).rows(_zip(**{"data.csv": _WIDE.removesuffix(b"\n")}))

    def test_a_member_with_no_header_below_its_preamble_raises(self) -> None:
        with pytest.raises(ReadError, match="no line opens with the header's first label 'Series Description'"):
            _ZippedLong(_Series).rows(_zip(**{"data.csv": b"title\nA,B\n"}))


# --- LongFile._by_key ---


class TestByKey:
    def test_on_a_sheet_each_keys_run_comes_whole(self) -> None:
        class ByKey(LongFile[_Regional], SheetFile[_Regional]):
            _keys = re.compile(r".+")
            _by_key = True

        body = workbook({"S": [["Date", "East", "West"], [_D1, 1, 2], [_D2, 3, 4]]})
        assert [(row.region, row.day) for row in ByKey(_Regional, sheet="S").rows(body)] == [
            ("East", _D1),
            ("East", _D2),
            ("West", _D1),
            ("West", _D2),
        ]

    def test_an_empty_cell_dropped_leaves_each_keys_order(self) -> None:
        class Sparse(LongFile[_Series], CsvFile[_Series]):
            _keys = re.compile(r".+")
            _by_key = True
            _empty_rows = False

        body = b"Series Description,A,B\n2026-01-02,,1\n2026-01-09,2,\n2026-01-16,3,4\n"
        assert [(row.series, row.day) for row in Sparse(_Series).rows(body)] == [
            ("A", _D2),
            ("A", _D3),
            ("B", _D1),
            ("B", _D3),
        ]


# --- Thousands + LongReport: the Fed's currency tables ---


class _Amount(msgspec.Struct, frozen=True, gc=False):
    holder: str
    year: int
    value: float


class _Amounts(Thousands[_Amount], LongReport[_Amount]):
    """A block of years over each holder's amounts, grouped in thousands, read long."""

    _key = "year"
    _line = re.compile(r"(?P<holder>[A-Z][a-z]+) +(?P<value>[-0-9., ]+?) *")
    _framing = (re.compile(r"HOLDER +(?P<year>\S.*?) *"),)


_AMOUNTS = b"""HOLDER  2026  2025
Japan   1,782,319.5  12
Peru    -4,000  999
"""


class TestAThousandsLongReport:
    def test_each_cell_is_its_number_its_groups_joined(self) -> None:
        assert _Amounts(_Amount).rows(_AMOUNTS) == [
            _Amount("Japan", 2026, 1782319.5),
            _Amount("Japan", 2025, 12.0),
            _Amount("Peru", 2026, -4000.0),
            _Amount("Peru", 2025, 999.0),
        ]

    def test_a_cell_grouped_otherwise_raises_naming_its_row(self) -> None:
        with pytest.raises(ReadError, match=r"the 'value' cell '12,00' is not a number grouped in thousands: row 2"):
            _Amounts(_Amount).rows(_AMOUNTS.replace(b"  12\n", b"  12,00\n"))


# --- Carried + BesideSheet: the Fed's shock map ---


class _Factor(msgspec.Struct, frozen=True, gc=False):
    asset_class: Annotated[str, Label("Asset Class")]
    product: Annotated[str, Label("Product")]
    level: Annotated[float, Label("Level")]


class _Factors(Carried[_Factor], BesideSheet[_Factor]):
    """federal_reserve's ``_RiskFactors``: right of a title column, a group written once, gaps between groups."""

    _columns_at = (1, 2, 3)
    _gaps = True
    _carried = ("asset_class",)


class TestACarriedBesideSheet:
    def test_the_tables_columns_alone_each_group_carried_down_past_a_gap(self) -> None:
        body = workbook(
            {
                "S": [
                    ["Risk factor levels"],
                    [None, "Asset Class", "Product", "Level", None, "Note"],
                    [None, "Rates", "2Y", 1.5, None, "beside"],
                    [None, None, "10Y", 2],
                    [],
                    [None, "Credit", "IG", 3],
                    [None, None, "HY", 4],
                ]
            }
        )
        rows = _Factors(_Factor, sheet="S", header_row=2).rows(body)
        assert rows == [
            _Factor("Rates", "2Y", 1.5),
            _Factor("Rates", "10Y", 2.0),
            _Factor("Credit", "IG", 3.0),
            _Factor("Credit", "HY", 4.0),
        ]

    def test_a_first_row_without_its_group_is_refused_as_an_empty_cell(self) -> None:
        body = workbook({"S": [[None, "Asset Class", "Product", "Level"], [None, None, "2Y", 1]]})
        with pytest.raises(ReadError, match=r"\['asset_class'\] columns have an empty cell.*row 2"):
            _Factors(_Factor, sheet="S").rows(body)


# --- LongFile + Thousands + Transcoded + SectionedCsv + PaddedCsv: EIA's WPSR tables ---


class _Stub(msgspec.Struct, frozen=True, gc=False):
    stub: Annotated[str, Label("STUB_1")]
    column: int
    heading: str
    value: float | None


class _Wpsr(LongFile[_Stub], Thousands[_Stub], Transcoded[_Stub], SectionedCsv[_Stub], PaddedCsv[_Stub]):
    """eia's ``_Table``: cp1252, a DOS end-of-file byte, two tables, a repeated heading, padded grouped numbers."""

    _encodings = ("cp1252",)
    _eof = bytes([0x1A])
    _section = re.compile(rb'^"?STUB_1"?,', re.MULTILINE)
    _empty_rows = False
    _rows_required = False
    _bare_header_refused = True
    _repeats = True
    _key_count = 2
    _keys = re.compile(r"(?!STUB_)\s*(?P<heading>.*?)\s*", re.DOTALL)

    @typing.override
    def _clean(self, cells: pl.Series, column: _Column) -> pl.Series:
        return super()._clean(cells.str.strip_chars(), column)


_DASH = chr(0x2013)  # an en dash, the WPSR's no value
_WPSR = (
    f'"STUB_1","Week"," Difference ","Difference"\r\n'
    f'"Crude"," 1,234.5 ","{_DASH}","-12"\r\n'
    f'"Café","7","{_DASH} {_DASH}"," 1,000 "\r\n'
    f'"STUB_1","Year"\r\n"Total","5"\r\n'
).encode("cp1252") + bytes([0x1A])


def _wpsr(section: int) -> _Wpsr:
    return _Wpsr(_Stub, section=section, sections=2, null_values=(_DASH, f"{_DASH} {_DASH}"), final_line_ending=True)


class TestAWpsrStack:
    def test_a_table_is_decoded_cut_stripped_ungrouped_and_read_long(self) -> None:
        assert _wpsr(0).rows(_WPSR) == [
            _Stub("Crude", 1, "Week", 1234.5),
            _Stub("Crude", 3, "Difference", -12.0),
            _Stub("Café", 1, "Week", 7.0),
            _Stub("Café", 3, "Difference", 1000.0),
        ]
        assert _wpsr(1).rows(_WPSR) == [_Stub("Total", 1, "Year", 5.0)]

    def test_the_body_is_decoded_before_its_tables_are_found(self) -> None:
        with pytest.raises(ReadError, match="the body is not cp1252"):
            _wpsr(0).rows(_WPSR.replace(b"7", b"\x81"))
        with pytest.raises(ReadError, match=r"the body has 1 tables"):
            _wpsr(0).rows(_WPSR.split(b'"STUB_1","Year"')[0])


# --- Defensive branches no piece's test ran ---


class _Row(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("NAME")]
    value: Annotated[float | None, Label("VALUE")]
    note: Annotated[str | None, Label("NOTE")]


class _Headed(HeadersCsv[_Row]):
    _headers = (("NAME", "VALUE", "NOTE"),)


class TestACsvsRefusals:
    def test_a_body_opening_with_a_blank_line_is_refused(self) -> None:
        with pytest.raises(ReadError, match="the body opens with a blank line, not its header"):
            CsvFile(_Row).rows(b"\nNAME,VALUE,NOTE\na,1,x\n")

    @pytest.mark.xfail(
        strict=True,
        reason="_full counts a separator inside a quoted null token (null_values=('n,a',)) as a cell's: the rows are"
        " each as wide as the header, but the read is refused",
    )
    @pytest.mark.parametrize("reader", [CsvFile, NamedCsv, _Headed])
    def test_a_quoted_null_token_holding_the_separator_reads_as_none(self, reader: type[CsvFile[_Row]]) -> None:
        assert reader(_Row, null_values=("n,a",)).rows(b'NAME,VALUE,NOTE\na,"n,a",x\n') == [_Row("a", None, "x")]

    def test_a_quoted_cell_that_never_closes_is_refused(self) -> None:
        with pytest.raises(ReadError, match="a quoted cell does not close: unexpected end of data"):
            CsvFile(_Row).rows(b'NAME,VALUE,NOTE\na,1,"x\n')

    def test_a_spilled_body_holding_every_carrier_is_refused(self) -> None:
        class Spilled(SpilledCsv[_Row]):
            _quote_char = None
            _spilled = "note"

        with pytest.raises(ReadError, match="holds every character this reader carries the separator in"):
            Spilled(_Row).rows(b"NAME,VALUE,NOTE\na,1,\x1f\x1e\x1d\x1c\n")


class _Code(msgspec.Struct, frozen=True, gc=False):
    code: Annotated[str, Span(0, 3)]


class TestAFixedWidthFilesRefusals:
    def test_a_body_that_is_not_utf8_is_refused(self) -> None:
        class Headless(FixedWidthFile[_Code]):
            _header = False

        with pytest.raises(ReadError, match="the body is not UTF-8"):
            Headless(_Code).rows(b"ab\xff\n")

    def test_a_format_whose_table_states_other_fields_is_a_bug(self) -> None:
        class Stating(FixedWidthFile[_Code]):
            _header = False

            @typing.override
            def _table(self, lines: pl.Series) -> tuple[pl.Series, dict[str, str]]:
                return lines, {"as_of": "x"}

        with pytest.raises(LayoutError, match=r"the format states \['as_of'\], not the fields \[\]"):
            Stating(_Code).rows(b"abc\n")


class TestAFoundTablesLayout:
    def test_two_fields_sharing_a_label_are_a_bug(self) -> None:
        class Twice(msgspec.Struct, frozen=True, gc=False):
            year: Annotated[int, Label("year")]
            kind: Annotated[str, Label("type", alternatives=("year",))]

        with pytest.raises(LayoutError, match="two fields of Twice share a label"):
            FoundCsv(Twice, key="year")


class _Named(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("Name")]


class TestASheetTextsFirstColumn:
    def test_it_is_the_sheet_column_of_the_first_filled_one_or_a_for_an_empty_sheet(self) -> None:
        reader = SheetText._workbook(workbook({"S": [[None, None, "x", 1]], "E": []}))
        sheet = SheetText(_Named, sheet="S")
        assert sheet._sheet_grid(reader, "S")[0] == 2
        assert sheet._sheet_grid(reader, "E")[0] == 0
