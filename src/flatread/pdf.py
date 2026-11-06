"""A PDF's text, line by line in the order its pages draw it, for a reader that walks a generated form's lines.

Needs the ``pdf`` extra (pdfminer.six). :func:`read_text` returns every page's text, one grouped line per LF.

**Why the lines keep the drawing order.** A reader of a generated PDF (a filing, a statement, a report a program
writes) walks it as the program drew it: one table cell, or one wrapped line of a cell, per line. pdfminer's high-level
``extract_text`` does not keep that order: it groups lines into boxes and re-sorts them by position. So
:func:`read_text` takes each page's characters in content-stream order and runs pdfminer's own character-to-line
grouper (``LTLayoutContainer.group_objects`` with default ``LAParams``) over them, skipping the box grouping.
``group_objects`` is public but not in pdfminer's API reference, so the tests pin what it does. It is not fast: pdfminer
interprets the content stream in pure Python, some 200 ms for a three-page form.

**A NUL is deleted.** A font may map glyphs to U+0000 in its ToUnicode table (a small-capital label set in an embedded
CID font, measured), and pdfminer reports what the PDF says. No page shows a NUL, so every U+0000 is deleted as each
line is built and none reaches the caller.
"""

import io
from typing import TYPE_CHECKING, Final

from pdfminer.converter import PDFPageAggregator
from pdfminer.layout import LAParams, LTChar, LTContainer, LTLayoutContainer
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser
from pdfminer.psexceptions import PSException

from flatread.errors import EmptyBodyError, UnknownLayoutError

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

__all__ = ["read_text"]

#: U+0000, which no page shows (see the module docstring).
_NUL: Final = chr(0)

# What pdfminer raises on bytes it cannot open or read. PSException is the base of its own tree (PDFSyntaxError, PSEOF,
# PDFEncryptionError, …): every failure on 221 damaged copies of a real filing was one. pdfminer is pure Python and
# lets builtin errors escape from a malformed object graph, so those are caught too, around the extraction alone.
_UNREADABLE: Final = (PSException, ValueError, TypeError, KeyError, IndexError, AttributeError)


def read_text(body: bytes) -> str:
    """Return every page's text, one grouped line per LF, in the order the pages draw it.

    An empty body raises :class:`~flatread.errors.EmptyBodyError`. Bytes pdfminer cannot open or read raise
    :class:`~flatread.errors.UnknownLayoutError`, and so does a PDF with no text layer, such as a scanned page, which
    would otherwise read as a document with no lines.

    Characters are collected in the order each page draws them, descending into form XObjects, and handed to pdfminer's
    own line grouper. ``PDFPage.create_pages`` rather than ``get_pages``, which logs a warning for every PDF that
    carries the "do not extract" permission flag, as many generated forms do.
    """
    if not body:
        msg = "the body is empty"
        raise EmptyBodyError(msg)
    try:
        text = _text(body)
    except _UNREADABLE as exc:
        msg = f"the body is not a readable PDF: {exc}"
        raise UnknownLayoutError(msg) from exc
    if not text.strip():
        msg = "the PDF has no text layer: a scanned document"
        raise UnknownLayoutError(msg)
    return text


def _text(body: bytes) -> str:
    """Return the pages' grouped lines, each ended by an LF, with every NUL deleted."""
    laparams = LAParams()
    resources = PDFResourceManager()
    device = PDFPageAggregator(resources, laparams=None)  # None: no layout analysis, the characters as drawn
    interpreter = PDFPageInterpreter(resources, device)
    lines: list[str] = []
    for page in PDFPage.create_pages(PDFDocument(PDFParser(io.BytesIO(body)))):
        interpreter.process_page(page)
        layout = device.get_result()
        drawn = list(_chars(layout))
        if not drawn:
            continue
        for line in LTLayoutContainer(layout.bbox).group_objects(laparams, drawn):
            line.analyze(laparams)  # inserts the inter-word spaces (word_margin)
            lines.append(line.get_text().replace(_NUL, ""))
    return "".join(lines)


def _chars(items: Iterable[object]) -> Iterator[LTChar]:
    """Yield the characters of a page's layout in the order the page draws them, descending into its containers."""
    for item in items:
        if isinstance(item, LTChar):
            yield item
        elif isinstance(item, LTContainer):
            yield from _chars(item)
