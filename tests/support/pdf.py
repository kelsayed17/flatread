"""A PDF drawn in memory for a reader's tests: no PDF writer is a dependency."""


def pdf(*pages: list[str] | list[tuple[float, float, str]]) -> bytes:
    """Return a PDF drawing each page's lines, one text line each (Helvetica 11 pt, 14 pt leading).

    A page's line may instead be a text and the point it starts at, ``(x, y, text)``: each drawn there, in the order
    given.

    Written out by hand because no PDF writer is a dependency: the catalog, the page tree, a font, and a page and its
    content stream for each page, then the cross-reference table.
    """

    def literal(line: str) -> str:
        return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids: list[int] = []
    for lines in pages:
        flowed = [f"({literal(line)}) Tj T*" for line in lines if isinstance(line, str)]
        placed = [
            f"BT /F1 11 Tf {line[0]} {line[1]} Td ({literal(line[2])}) Tj ET"
            for line in lines
            if not isinstance(line, str)
        ]
        content = "\n".join(["BT /F1 11 Tf 14 TL 72 720 Td", *flowed, "ET", *placed]).encode("latin-1")
        kids.append(len(objects) + 1)
        objects.extend(
            (
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % (len(objects) + 2),
                b"<< /Length %d >>\nstream\n%b\nendstream" % (len(content), content),
            )
        )
    objects[1] = b"<< /Type /Pages /Kids [%b] /Count %d >>" % (b" ".join(b"%d 0 R" % kid for kid in kids), len(kids))
    return document(objects)


def document(objects: list[bytes]) -> bytes:
    """Return a PDF of *objects*, numbered from 1 (the first the catalog), with its cross-reference table."""
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n%b\nendobj\n" % (number, body)
    xref = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    pdf += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(pdf)
