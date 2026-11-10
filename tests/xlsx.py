"""A minimal ``.xlsx`` writer for tests: sheets of numbers, inline strings and empty cells, no styles."""

import io
import zipfile
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

type Cell = float | int | str | None


def _column(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        name = chr(65 + rest) + name
    return name


def _sheet(rows: Sequence[Sequence[Cell]]) -> str:
    out = []
    for r, row in enumerate(rows, 1):
        cells = []
        for c, value in enumerate(row):
            ref = f"{_column(c)}{r}"
            if value is None:
                continue
            if isinstance(value, str):
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>')
            else:
                cells.append(f'<c r="{ref}"><v>{value!r}</v></c>')
        out.append(f'<row r="{r}">{"".join(cells)}</row>')
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    return f'<?xml version="1.0"?><worksheet xmlns="{ns}"><sheetData>{"".join(out)}</sheetData></worksheet>'


def workbook(sheets: Mapping[str, Sequence[Sequence[Cell]]]) -> bytes:
    """Return an ``.xlsx`` body holding *sheets*, in order, each a list of rows."""
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pkg = "http://schemas.openxmlformats.org/package/2006/relationships"
    names = list(sheets)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        overrides = "".join(
            f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for i in range(1, len(names) + 1)
        )
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            f"{overrides}</Types>",
        )
        zf.writestr(
            "_rels/.rels",
            f'<?xml version="1.0"?><Relationships xmlns="{pkg}"><Relationship Id="rId1" '
            f'Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        )
        entries = "".join(
            f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>' for i, name in enumerate(names, 1)
        )
        zf.writestr(
            "xl/workbook.xml",
            f'<?xml version="1.0"?><workbook xmlns="{main}" xmlns:r="{rel}"><sheets>{entries}</sheets></workbook>',
        )
        links = "".join(
            f'<Relationship Id="rId{i}" Type="{rel}/worksheet" Target="worksheets/sheet{i}.xml"/>'
            for i in range(1, len(names) + 1)
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels", f'<?xml version="1.0"?><Relationships xmlns="{pkg}">{links}</Relationships>'
        )
        for i, name in enumerate(names, 1):
            zf.writestr(f"xl/worksheets/sheet{i}.xml", _sheet(sheets[name]))
    return buffer.getvalue()
