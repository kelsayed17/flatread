"""A small workbook built in a test: a ZIP of the XML parts calamine reads.

A cell is an inline string, a number, a boolean (``t="b"``), a date or a time of day: its serial number styled with
Excel's built-in date or time format, as Excel saves one.
"""

import datetime as dt
import io
import zipfile
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

type Cell = str | int | float | bool | dt.date | dt.time | None
type Grid = list[list[Cell]]

#: Excel's day 0, so a date's serial number is its days since it (1900's leap-year bug is past by 1900-03-01).
_EPOCH = dt.date(1899, 12, 30)


def _column(at: int) -> str:
    return chr(ord("A") + at)


def _sheet_xml(grid: Sequence[Sequence[Cell]]) -> str:
    rows = []
    for r, row in enumerate(grid, 1):
        cells = []
        for c, value in enumerate(row):
            ref = f"{_column(c)}{r}"
            if isinstance(value, bool):
                cells.append(f'<c r="{ref}" t="b"><v>{int(value)}</v></c>')
            elif isinstance(value, dt.time):
                seconds = value.hour * 3600 + value.minute * 60 + value.second
                cells.append(f'<c r="{ref}" s="2"><v>{seconds / 86400!r}</v></c>')
            elif isinstance(value, dt.date):
                cells.append(f'<c r="{ref}" s="1"><v>{(value - _EPOCH).days}</v></c>')
            elif isinstance(value, int | float):
                cells.append(f'<c r="{ref}"><v>{value!r}</v></c>')
            elif value is not None:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(value)}</t></is></c>')
        rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    return f'<?xml version="1.0"?><worksheet xmlns="{ns}"><sheetData>{"".join(rows)}</sheetData></worksheet>'


def workbook(sheets: Mapping[str, Sequence[Sequence[Cell]]]) -> bytes:
    """Return an xlsx holding *sheets*, in order, each a grid of cells (see the module) and empty ones."""
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pkg = "http://schemas.openxmlformats.org/package/2006/relationships"
    names = "".join(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>' for i, name in enumerate(sheets, 1))
    links = "".join(
        f'<Relationship Id="rId{i}" Type="{rel}/worksheet" Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, len(sheets) + 1)
    )
    sheet_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="{sheet_type}"/>'
        for i in range(1, len(sheets) + 1)
    )
    book_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            f'<Override PartName="/xl/workbook.xml" ContentType="{book_type}"/>{overrides}'
            '<Override PartName="/xl/styles.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>'
        ),
        "_rels/.rels": (
            f'<?xml version="1.0"?><Relationships xmlns="{pkg}">'
            f'<Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>'
        ),
        "xl/workbook.xml": (
            f'<?xml version="1.0"?><workbook xmlns="{main}" xmlns:r="{rel}"><sheets>{names}</sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            f'<?xml version="1.0"?><Relationships xmlns="{pkg}">{links}'
            f'<Relationship Id="rIdStyles" Type="{rel}/styles" Target="styles.xml"/></Relationships>'
        ),
        # Style 0 is General, style 1 Excel's built-in date format (number format 14), style 2 its time (21).
        "xl/styles.xml": (
            f'<?xml version="1.0"?><styleSheet xmlns="{main}"><cellXfs count="3">'
            '<xf numFmtId="0"/><xf numFmtId="14" applyNumberFormat="1"/><xf numFmtId="21" applyNumberFormat="1"/>'
            "</cellXfs></styleSheet>"
        ),
    }
    parts |= {f"xl/worksheets/sheet{i}.xml": _sheet_xml(grid) for i, grid in enumerate(sheets.values(), 1)}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, xml in parts.items():
            # A fixed timestamp: these bytes are parametrize ids, and the clock would give each xdist worker its own.
            archive.writestr(zipfile.ZipInfo(name), xml, compress_type=zipfile.ZIP_DEFLATED)
    return buffer.getvalue()
