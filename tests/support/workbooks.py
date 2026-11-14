"""Edit a recorded workbook's XML, so a reader's edge cases run on the real file with one thing changed."""

import io
import re
import zipfile

#: An xlsx workbook's first sheet.
FIRST_SHEET = "xl/worksheets/sheet1.xml"


def swapped(body: bytes, member: str, old: bytes, new: bytes) -> bytes:
    """Return the workbook *body* with *old* swapped for *new* once in its *member* XML: a drifted workbook."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(body)) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == member:
                assert old in data, f"{old!r} is not in {member}"
                data = data.replace(old, new, 1)
            zout.writestr(zipfile.ZipInfo(info.filename), data, compress_type=zipfile.ZIP_DEFLATED)  # fixed timestamp
    return out.getvalue()


def cell(body: bytes, ref: str, value: bytes, sheet: str = FIRST_SHEET) -> bytes:
    """Return the workbook *body* with *sheet*'s cell *ref* holding *value*, its own text, instead.

    A cell the sheet does not write is added at the end of its row, which holds it in order only past its last cell.
    """
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        xml = archive.read(sheet)
    new = f'<c r="{ref}" t="inlineStr"><is><t>'.encode() + value + b"</t></is></c>"
    if (start := xml.find(f'<c r="{ref}"'.encode())) < 0:
        row = re.search(rb'<row r="%s"[^>]*>.*?</row>' % re.sub(r"\D", "", ref).encode(), xml, re.DOTALL)
        assert row is not None, f"{sheet} has no row for {ref}"
        return swapped(body, sheet, row[0], row[0].removesuffix(b"</row>") + new + b"</row>")
    end = xml.index(b">", start)
    end = end + 1 if xml[end - 1 : end] == b"/" else xml.index(b"</c>", start) + 4
    return swapped(body, sheet, xml[start:end], new)


def header_only(body: bytes, keep: int, sheet: str = FIRST_SHEET) -> bytes:
    """Return the workbook *body* with *sheet* cut to its first *keep* rows: a sheet with no records."""
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        xml = archive.read(sheet)
    later = (row for row in re.finditer(rb'<row r="(\d+)"', xml) if int(row[1]) > keep)
    start = next(later).start()
    return swapped(body, sheet, xml[start : xml.index(b"</sheetData>")], b"")
