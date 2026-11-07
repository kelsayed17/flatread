"""``flatread.workbooks`` — the workbook decoders' one Excel reader, and the errors it maps onto the decoders'."""

import io
import re
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import msgspec
import polars as pl
import pytest

from flatread.cells import CellError, Column
from flatread.errors import CorruptArchiveError, EmptyBodyError, ReadError, UnknownLayoutError
from flatread.workbooks import (
    _refuse_error_cells,  # its damaged-part branch, which no read reaches
    read_every_sheet,
    read_every_sheet_text,
    read_sheet,
    read_sheet_rows,
    read_sheet_table,
    read_sheet_text,
)

if TYPE_CHECKING:
    from collections.abc import Callable

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
    # The real stocks sheet's fuel-ethanol column is blank for its first 1,000-odd weeks, so inference gives text
    # (measured on the 2026-10-19 download; a four-row fixture cannot reproduce an inference over 1,000 rows).
    # Declared, the column is a number: polars narrows a float column of whole numbers to Int64.
    frame = read_sheet(
        _PSW01.read_bytes(), "Data 1", header_row=1, skip_rows=1, dtypes={"W_EPOOXE_SAE_NUS_MBBL": "float"}
    )
    assert frame.schema["W_EPOOXE_SAE_NUS_MBBL"] == pl.Int64
    assert frame["W_EPOOXE_SAE_NUS_MBBL"].to_list() == [None, None, 23743, 23942]


def test_one_declared_type_is_every_columns_type() -> None:
    # The dates are text (``1960:03:01``), so as floats they are null; polars narrows the whole numbers to Int64.
    frame = read_sheet(_ADS.read_bytes(), "Sheet1", dtypes="float")
    assert frame.schema.dtypes() == [pl.Int64(), pl.Float64(), pl.Int64()]
    assert frame["Date"].null_count() == frame.height


@pytest.mark.parametrize("dtypes", ["string", {"ADS_Index": "string"}], ids=["every column", "one column"])
def test_text_is_no_declared_type(dtypes: object) -> None:
    """The engine's text of a number cell is rounded to nine decimals, so the sheet's text is read_sheet_text's."""
    with pytest.raises(ValueError, match=r"not \['string'\]: a sheet's text is read_sheet_text's"):
        read_sheet(_ADS.read_bytes(), "Sheet1", dtypes=dtypes)  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="read_sheet_text"):
        read_every_sheet(_ADS.read_bytes(), dtypes=dtypes)  # ty: ignore[invalid-argument-type]


def test_a_sheet_without_a_header_row_is_all_data() -> None:
    frame = read_sheet_text(_ADS.read_bytes(), "Sheet1", header_row=None)
    assert frame.columns == ["column_0", "column_1", "column_2"]
    assert frame.row(0) == ("Date", "ADS_Index", "RECBARS")


def test_every_sheet_reads_in_the_workbooks_order_as_one_sheet_reads() -> None:
    body = _ACM.read_bytes()
    sheets = read_every_sheet(body)
    assert list(sheets) == ["ACM Monthly", "ACM Daily"]
    for name, frame in sheets.items():
        assert frame.equals(read_sheet(body, name))


def test_every_sheets_text_reads_as_each_sheets_text_reads() -> None:
    body = _ACM.read_bytes()
    sheets = read_every_sheet_text(body, header_row=None)
    assert list(sheets) == ["ACM Monthly", "ACM Daily"]
    for name, frame in sheets.items():
        assert frame.equals(read_sheet_text(body, name, header_row=None))
    assert sheets["ACM Monthly"]["column_1"][1] == "2.9002725007830223"


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
    ("body", "sheet"),
    [
        (b"PK" + bytes([3, 4]) + b"not a zip", "Sheet1"),
        (_ADS.read_bytes()[: len(_ADS.read_bytes()) // 2], "Sheet1"),
        (_ACM.read_bytes()[: len(_ACM.read_bytes()) // 2], "ACM Monthly"),
        (_ACM.read_bytes()[:-100], "ACM Monthly"),
    ],
    ids=["a ZIP signature alone", "an xlsx cut in half", "an xls cut in half", "an xls short of its end"],
)
def test_a_zip_calamine_cannot_read_is_a_corrupt_archive(body: bytes, sheet: str) -> None:
    """An xlsx is a ZIP and an xls a compound file, so a body that opens as one and still cannot be read was damaged."""
    with pytest.raises(CorruptArchiveError, match="the workbook cannot be read"):
        read_sheet(body, sheet)


def test_a_damaged_worksheet_inside_a_whole_zip_is_a_corrupt_archive() -> None:
    """Calamine reads a worksheet's part only when the sheet is loaded, so this one fails past the open."""
    out = io.BytesIO()
    with zipfile.ZipFile(_ADS) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item)
            dst.writestr(item, data[: len(data) // 2] if item.filename == "xl/worksheets/sheet1.xml" else data)
    with pytest.raises(CorruptArchiveError, match="the workbook cannot be read"):
        read_sheet(out.getvalue(), "Sheet1")


def test_a_missing_sheet_names_the_sheet_asked_for() -> None:
    with pytest.raises(UnknownLayoutError, match=r"^the workbook has no sheet 'Nope'$"):
        read_sheet(_ADS.read_bytes(), "Nope")


def test_an_empty_body_is_an_empty_body_error() -> None:
    with pytest.raises(EmptyBodyError):
        read_sheet(b"", "Sheet1")


def test_every_failure_is_a_decode_error() -> None:
    # An engine error escaping would be no ReadError, so no caller treating a ReadError as a bad body would see one.
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

    assert _engine_text(body, "ACM Monthly")["ACMY01"][0] == "2.900272501"
    assert exact["ACMY01"][0] == "2.9002725007830223"
    assert exact["DATE"][0] == _engine_text(body, "ACM Monthly")["DATE"][0]


def _engine_text(body: bytes, sheet: str) -> pl.DataFrame:
    """Return the sheet as the calamine engine writes its text, which read_sheet does not offer: rounded numbers."""
    return pl.read_excel(io.BytesIO(body), engine="calamine", sheet_name=sheet, read_options={"dtypes": "string"})


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

    assert _engine_text(body, "Sheet1")["ADS_Index"].head(4).to_list() == ["12", "3", "0", "12"]
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


def test_an_excel_error_cell_is_refused_not_read_as_empty() -> None:
    """The engine reads ``#N/A`` as an empty cell, which a column whose missing token is ``""`` would take as None."""
    body = _with_cells('<c r="@"><v>1.5</v></c>', '<c r="@" t="e"><v>#N/A</v></c>')
    for read in (read_sheet, read_sheet_text):
        with pytest.raises(UnknownLayoutError, match=r"^the 'Sheet1' sheet's cell B3 is the Excel error '#N/A'"):
            read(body, "Sheet1")
    for every in (read_every_sheet, read_every_sheet_text):
        with pytest.raises(UnknownLayoutError, match="#N/A"):
            every(body)


def test_a_self_closing_error_cell_reports_its_own_error_not_the_next_cells_value() -> None:
    body = _with_cells('<c r="@" t="e"/>', '<c r="@"><v>1.5</v></c>')
    with pytest.raises(UnknownLayoutError, match=r"^the 'Sheet1' sheet's cell B2 is the Excel error '', not a value$"):
        read_sheet(body, "Sheet1")


def _with_parts(parts: dict[str, bytes | None]) -> bytes:
    """Return the ADS fixture with each named part replaced, or left out when ``None``."""
    with zipfile.ZipFile(_ADS) as source:
        written = {name: source.read(name) for name in source.namelist()}
    for name, data in parts.items():
        if data is None:
            del written[name]
        else:
            written[name] = data
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for name, data in written.items():
            target.writestr(name, data)
    return out.getvalue()


def _workbook_xml(transform: Callable[[bytes], bytes]) -> bytes:
    with zipfile.ZipFile(_ADS) as source:
        return transform(source.read("xl/workbook.xml"))


def test_a_sheet_the_workbook_part_maps_to_no_relationship_is_a_corrupt_archive() -> None:
    """The error-cell scan finds each sheet's part by its relationship; one that does not resolve is damage.

    calamine refuses both damaged workbooks first, so a read never reaches the scan with one: it is called alone.
    """
    body = _with_parts(
        {"xl/workbook.xml": _workbook_xml(lambda xml: re.sub(rb'(:id=")[^"]*"', rb'\1rIdNowhere"', xml))}
    )
    with pytest.raises(CorruptArchiveError, match=r"^the workbook cannot be read: "):
        _refuse_error_cells(body, ["Sheet1"])


def test_a_workbook_without_its_relationships_part_is_a_corrupt_archive() -> None:
    with pytest.raises(CorruptArchiveError, match=r"^the workbook cannot be read: "):
        _refuse_error_cells(_with_parts({"xl/_rels/workbook.xml.rels": None}), ["Sheet1"])


class _AdsDay(msgspec.Struct, frozen=True, gc=False):
    date: Annotated[str, Column("Date")]
    index: Annotated[float, Column("ADS_Index")]
    recession: Annotated[int, Column("RECBARS")]


def test_a_sheet_table_reads_the_sheets_text_into_its_row_type() -> None:
    table = read_sheet_table(_ADS.read_bytes(), "Sheet1", _AdsDay)
    assert table.columns == ["Date", "ADS_Index", "RECBARS"]
    assert table.row(0) == ("1960:03:01", -0.5816963701473981, 0)


def test_a_sheet_tables_error_names_the_cells_row_in_the_sheet() -> None:
    body = _with_cells('<c r="@"><v>1.5</v></c>', '<c r="@" t="inlineStr"><is><t>n/a</t></is></c>')
    with pytest.raises(CellError, match=r"^row 3: the 'ADS_Index' cell 'n/a'"):
        read_sheet_table(body, "Sheet1", _AdsDay)


def test_a_sheet_tables_rows_are_numbered_below_its_header_and_skipped_rows() -> None:
    # The ADS sheet's sheet row 2 taken as its header, and sheet row 3 skipped: sheet row 4 is the first read.
    class _Shifted(msgspec.Struct, frozen=True, gc=False):
        date: Annotated[str, Column("1960:03:01")]
        index: Annotated[int, Column("-0.5816963701473981")]
        recession: Annotated[str, Column("0")]

    with pytest.raises(CellError, match=r"^row 4: the '-0.5816963701473981' cell '-0.6772670161696065'"):
        read_sheet_table(_ADS.read_bytes(), "Sheet1", _Shifted, header_row=1, skip_rows=1)


def test_a_sheet_table_of_a_header_and_no_rows_is_an_empty_body() -> None:
    with zipfile.ZipFile(_ADS) as source:
        parts = {name: source.read(name) for name in source.namelist()}
    parts["xl/worksheets/sheet1.xml"] = re.sub(rb'<row r="[2-9]".*?</row>', b"", parts["xl/worksheets/sheet1.xml"])
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for name, data in parts.items():
            target.writestr(name, data)
    with pytest.raises(EmptyBodyError, match=r"^the 'Sheet1' sheet has a header and no rows$"):
        read_sheet_table(out.getvalue(), "Sheet1", _AdsDay)


def test_sheet_rows_read_a_tables_rows_out_of_its_grid_named_by_their_rows_in_the_sheet() -> None:
    grid = read_sheet_text(_ADS.read_bytes(), "Sheet1", header_row=None)
    table = read_sheet_rows(grid, [2, 3], ["Date", "ADS_Index", "RECBARS"], _AdsDay)
    assert table.columns == ["Date", "ADS_Index", "RECBARS"]
    assert table["Date"].to_list() == grid["column_0"].to_list()[2:4]
    with pytest.raises(CellError, match=r"^row 1: the 'ADS_Index' cell 'ADS_Index'"):
        read_sheet_rows(grid, [2, 0], ["Date", "ADS_Index", "RECBARS"], _AdsDay)


def test_sheet_rows_refuse_a_cell_past_the_tables_columns_and_no_rows() -> None:
    grid = pl.DataFrame({"column_0": ["A", "B"], "column_1": ["1", "2"], "column_2": [None, "x"]})

    class _Pair(msgspec.Struct, frozen=True, gc=False):
        name: Annotated[str, Column("Name")]
        count: Annotated[int, Column("Count")]

    assert read_sheet_rows(grid, [0], ["Name", "Count"], _Pair).rows() == [("A", 1)]
    with pytest.raises(UnknownLayoutError, match=r"^row 2 has a cell past the table's 2 columns: \('B', '2', 'x'\)$"):
        read_sheet_rows(grid, [0, 1], ["Name", "Count"], _Pair)
    with pytest.raises(EmptyBodyError, match=r"^the sheet has a header and no rows$"):
        read_sheet_rows(grid, [], ["Name", "Count"], _Pair)
