"""Tests for :mod:`flatread.pdf`: a PDF's text, line by line in the order its pages draw it.

The PDFs are written out by hand, as no PDF writer is a dependency: the catalog, the page tree, a font, and a page and
its content stream for each page, then the cross-reference table.
"""

import pytest
from flatread.errors import EmptyBodyError, UnknownLayoutError
from flatread.pdf import read_text


def _pdf(*pages: list[tuple[int, str]]) -> bytes:
    """Return a PDF drawing each page's ``(y, text)`` lines in the order given, each at x 72 (Helvetica 11 pt)."""

    def literal(line: str) -> str:
        return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids: list[int] = []
    for lines in pages:
        drawn = (f"BT /F1 11 Tf 72 {y} Td ({literal(text)}) Tj ET" for y, text in lines)
        content = "\n".join(drawn).encode("latin-1")
        kids.append(len(objects) + 1)
        objects.extend(
            (
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % (len(objects) + 2),
                b"<< /Length %d >>\nstream\n%b\nendstream" % (len(content), content),
            )
        )
    objects[1] = b"<< /Type /Pages /Kids [%b] /Count %d >>" % (b" ".join(b"%d 0 R" % kid for kid in kids), len(kids))
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


def test_each_drawn_line_is_one_line_of_text() -> None:
    assert read_text(_pdf([(720, "Name: Jane Doe"), (700, "Owner Asset")])) == "Name: Jane Doe\nOwner Asset\n"


def test_the_lines_keep_the_order_the_page_draws_them_not_their_position() -> None:
    """The bottom line is drawn first, so it reads first: a position sort would put it last."""
    assert read_text(_pdf([(100, "drawn first, at the bottom"), (700, "drawn second, at the top")])) == (
        "drawn first, at the bottom\ndrawn second, at the top\n"
    )


def test_the_pages_follow_one_another() -> None:
    assert read_text(_pdf([(720, "page one")], [(720, "page two")])) == "page one\npage two\n"


def test_a_page_with_no_text_adds_no_line() -> None:
    assert read_text(_pdf([(720, "page one")], [])) == "page one\n"


def test_an_empty_body_raises() -> None:
    with pytest.raises(EmptyBodyError, match=r"^the body is empty$"):
        read_text(b"")


@pytest.mark.parametrize(
    "body", [b"not a pdf at all", b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog"], ids=["text", "cut off"]
)
def test_bytes_that_are_no_readable_pdf_raise(body: bytes) -> None:
    with pytest.raises(UnknownLayoutError, match=r"^the body is not a readable PDF: "):
        read_text(body)


def test_a_pdf_that_draws_no_text_raises() -> None:
    """A page with no characters on it: the shape of a scanned page, an image with no text layer."""
    with pytest.raises(UnknownLayoutError, match=r"^the PDF has no text layer: a scanned document$"):
        read_text(_pdf([]))
