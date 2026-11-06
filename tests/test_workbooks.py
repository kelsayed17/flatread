"""``flatread.workbooks`` — the workbook decoders' one Excel reader, and the errors it maps onto the decoders'."""

import io
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import msgspec
import polars as pl
import pytest
from flatread.errors import CorruptArchiveError, EmptyBodyError, ReadError, UnknownLayoutError
from flatread.workbooks import read_every_sheet, read_sheet, read_sheet_text

if TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

#: Real workbooks, trimmed, as their publishers serve them: Philadelphia Fed's ADS index (.xlsx),
#: the NY Fed's ACM term premia (.xls) and EIA's weekly petroleum status (.xls). The reader is tested on its own files.
_FIXTURES = Path(__file__).parent / "fixtures" / "workbooks"
_ADS = _FIXTURES / "ads_index_sample.xlsx"
_ACM = _FIXTURES / "acm_term_premium_sample.xls"
_PSW01 = _FIXTURES / "psw01_sample.xls"


def test_an_xlsx_sheet_reads_as_a_frame_with_the_files_header(snapshot: SnapshotAssertion) -> None:
    frame = read_sheet(_ADS.read_bytes(), "Sheet1")
    assert frame.columns == ["Date", "ADS_Index", "RECBARS"]
    assert msgspec.to_builtins(frame.rows()) == snapshot


def test_a_legacy_xls_sheet_reads_too(snapshot: SnapshotAssertion) -> None:
    frame = read_sheet(_ACM.read_bytes(), "ACM Monthly")
    assert msgspec.to_builtins({"columns": frame.columns, "rows": frame.rows()}) == snapshot


def test_the_header_can_sit_below_a_title_and_a_description_row_can_be_skipped() -> None:
    # EIA's stocks sheet: a link row, then the series-id header, then a row of descriptions, then the weeks.
    frame = read_sheet(_PSW01.read_bytes(), "Data 1", header_row=1, skip_rows=1)
    assert frame.columns[:3] == ["Sourcekey", "WCRSTUS1", "WCESTUS1"]
    assert frame.height == 4


def test_a_declared_type_is_the_columns_type_whatever_its_early_rows_hold() -> None:
    # The real stocks sheet's fuel-ethanol column is blank for its first 1,000-odd weeks, so calamine infers text and
    # fastexcel logs a warning on every read (measured on the 2026-10-19 download; a four-row fixture cannot reproduce
    # an inference over 1,000 rows). Declared, the column is a number.
    frame = read_sheet(
        _PSW01.read_bytes(), "Data 1", header_row=1, skip_rows=1, dtypes={"W_EPOOXE_SAE_NUS_MBBL": "float"}
    )
    assert frame.schema["W_EPOOXE_SAE_NUS_MBBL"] == pl.Float64
    assert frame["W_EPOOXE_SAE_NUS_MBBL"].to_list() == [None, None, 23743.0, 23942.0]


def test_one_declared_type_is_every_columns_type() -> None:
    frame = read_sheet(_ADS.read_bytes(), "Sheet1", dtypes="string")
    assert frame.schema.dtypes() == [pl.String()] * 3


def test_a_sheet_without_a_header_row_is_all_data() -> None:
    frame = read_sheet(_ADS.read_bytes(), "Sheet1", header_row=None, dtypes="string")
    assert frame.columns == ["__UNNAMED__0", "__UNNAMED__1", "__UNNAMED__2"]
    assert frame.row(0) == ("Date", "ADS_Index", "RECBARS")


def test_every_sheet_reads_in_the_workbooks_order_as_one_sheet_reads() -> None:
    body = _ACM.read_bytes()
    sheets = read_every_sheet(body)
    assert list(sheets) == ["ACM Monthly", "ACM Daily"]
    for name, frame in sheets.items():
        assert frame.equals(read_sheet(body, name))


def test_no_sheet_name_reads_the_workbooks_one_sheet() -> None:
    """Treasury's allotments workbooks name their one sheet after the file's date range."""
    assert read_sheet(_ADS.read_bytes(), None).columns == ["Date", "ADS_Index", "RECBARS"]


def test_no_sheet_name_refuses_a_workbook_of_several() -> None:
    with pytest.raises(UnknownLayoutError, match="the workbook has 2 sheets, not one"):
        read_sheet(_ACM.read_bytes(), None)


@pytest.mark.parametrize("body", [b"<html>nope</html>", bytes(64)])
def test_a_body_that_is_no_workbook_is_an_unknown_layout(body: bytes) -> None:
    with pytest.raises(UnknownLayoutError, match="not a workbook"):
        read_sheet(body, "Sheet1")


@pytest.mark.parametrize(
    "body", [b"PK" + bytes([3, 4]) + b"not a zip", _ADS.read_bytes()[: len(_ADS.read_bytes()) // 2]]
)
def test_a_zip_calamine_cannot_read_is_a_corrupt_archive(body: bytes) -> None:
    """An xlsx is a ZIP, so a body that opens as one and still cannot be read was damaged or cut short."""
    with pytest.raises(CorruptArchiveError, match="the workbook cannot be read"):
        read_sheet(body, "Sheet1")


def test_a_damaged_worksheet_inside_a_whole_zip_is_a_corrupt_archive() -> None:
    """Calamine reads a worksheet's part only when the sheet is loaded, so this one fails past the open."""
    out = io.BytesIO()
    with zipfile.ZipFile(_ADS) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item)
            dst.writestr(item, data[: len(data) // 2] if item.filename == "xl/worksheets/sheet1.xml" else data)
    with pytest.raises(CorruptArchiveError, match="the workbook cannot be read"):
        read_sheet(out.getvalue(), "Sheet1")


def test_a_missing_sheet_names_the_sheets_the_workbook_has() -> None:
    with pytest.raises(UnknownLayoutError, match=r"no sheet 'Nope'; it has \['Sheet1'\]"):
        read_sheet(_ADS.read_bytes(), "Nope")


def test_an_empty_body_is_an_empty_body_error() -> None:
    with pytest.raises(EmptyBodyError):
        read_sheet(b"", "Sheet1")


def test_every_failure_is_a_decode_error() -> None:
    # A calamine error escaping would be no ReadError, so no caller treating a ReadError as a bad body would see one.
    for body in (b"", b"junk", b"PK" + bytes([3, 4]) + b"junk"):
        with pytest.raises(ReadError):
            read_sheet(body, "Sheet1")
        with pytest.raises(ReadError):
            read_every_sheet(body)


def test_read_sheet_text_gives_a_number_cell_its_exact_text_not_calamines_rounded_one() -> None:
    """A number cell's calamine text has nine decimals; the exact reader gives the float's shortest exact text."""
    fixture = _FIXTURES / "acm_term_premium_sample.xls"
    body = fixture.read_bytes()

    exact = read_sheet_text(body, "ACM Monthly")

    assert read_sheet(body, "ACM Monthly", dtypes="string")["ACMY01"][0] == "2.900272501"
    assert exact["ACMY01"][0] == "2.9002725007830223"
    assert exact["DATE"][0] == read_sheet(body, "ACM Monthly", dtypes="string")["DATE"][0]


def _with_cells(*cells: str) -> bytes:
    """Return the ADS fixture with column B's first cells written as *cells*: each a cell's raw XML ``<c>`` body."""
    fixture = _FIXTURES / "ads_index_sample.xlsx"
    with zipfile.ZipFile(fixture) as source:
        parts = {name: source.read(name) for name in source.namelist()}
    sheet = parts["xl/worksheets/sheet1.xml"].decode()
    for row, cell in enumerate(cells, start=2):
        start = sheet.index(f'<c r="B{row}"')
        sheet = sheet[:start] + cell.replace("@", f"B{row}") + sheet[sheet.index("</c>", start) + len("</c>") :]
    parts["xl/worksheets/sheet1.xml"] = sheet.encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for name, data in parts.items():
            target.writestr(name, data)
    return out.getvalue()


def test_read_sheet_text_restores_a_number_calamine_rounded_to_a_whole_or_to_zero() -> None:
    body = _with_cells(
        '<c r="@"><v>12.0000000001</v></c>',
        '<c r="@"><v>2.9999999999</v></c>',
        '<c r="@"><v>1E-10</v></c>',
        '<c r="@"><v>12</v></c>',
    )

    assert read_sheet(body, None, dtypes="string")["ADS_Index"].head(4).to_list() == ["12", "3", "0", "12"]
    assert read_sheet_text(body, None)["ADS_Index"].head(4).to_list() == [
        "12.0000000001",
        "2.9999999999",
        "1e-10",
        "12",
    ]


def test_read_sheet_text_keeps_a_text_cell_exactly_as_written() -> None:
    body = _with_cells(
        '<c r="@" t="inlineStr"><is><t>1.50</t></is></c>',
        '<c r="@" t="inlineStr"><is><t>007.5</t></is></c>',
        '<c r="@" t="inlineStr"><is><t>0.10000000000000000001</t></is></c>',
    )

    assert read_sheet_text(body, None)["ADS_Index"].head(3).to_list() == ["1.50", "007.5", "0.10000000000000000001"]
