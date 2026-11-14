"""A PDF's text layer read natively into its row struct's fields: the positioned lines a format reads its rows from.

:class:`PdfFile` is a :class:`~flatread.core.TextFile` whose body is a PDF. It reads the text layer into
:class:`PdfLine` objects, each page's lines in the order the page draws them: pdfminer.six's own line grouper over the
content stream's characters (``extract_text`` would re-sort them), each character with the x its glyph starts at. A
format reads its records' text cells out of those lines (:meth:`~PdfFile._records`), in its row struct's field order,
and :class:`~flatread.core.TextFile` types them as it types any text table's. A PDF has no header to name
its columns, so a field without a ``Label`` is labeled by its name.

:class:`LaidOutPdf` reads the lines as the pages lay them out, for a table found by position: each page top to bottom,
the segments pdfminer groups apart on one baseline joined left to right, and three steps a format reads a table with,
each column's edge on its header line (:meth:`~LaidOutPdf._edges`), rows by the gap between lines
(:meth:`~LaidOutPdf._runs`) and each column's characters (:meth:`~LaidOutPdf._column_cells`). Its class attributes are
a format's measurements: ``_row_gap``, ``_slack`` (how far left of its label a cell may start), ``_baseline``.

The text layer is hardened as the other readers are: an empty body, bytes pdfminer cannot read and a PDF with no text
layer (a scanned paper filing, which would read as a table of no rows) raise ``ValueError``. pdfminer is imported
here, not in :mod:`flatread`, which every CSV reader loads.
"""

import abc
import io
import re
import typing
from collections.abc import Iterable, Iterator, Sequence
from typing import Final, NamedTuple

import msgspec
import polars as pl
from pdfminer.converter import PDFPageAggregator
from pdfminer.layout import LAParams, LTChar, LTContainer, LTLayoutContainer, LTTextLine
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser
from pdfminer.psexceptions import PSException

from flatread.core import _NUL, TextFile

__all__ = ["LaidOutPdf", "PdfFile", "PdfLine", "text_layer"]

# What pdfminer raises on bytes it cannot open or read. PSException is the base of its own tree (PDFSyntaxError, PSEOF,
# PDFEncryptionError, …): every failure on 221 damaged copies of a real filing was one. pdfminer is pure Python and
# lets builtin errors escape from a malformed object graph, so those are caught too, around the extraction alone.
_UNREADABLE: Final = (PSException, ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError)


class PdfLine(NamedTuple):
    """One line of a page's text layer: its page, its top, and each of its characters with the x it starts at."""

    #: The page's number, from 0.
    page: int
    #: The top of the line's tallest glyph, in points up from the page's foot.
    top: float
    #: Each character and the x its glyph starts at; a space the grouper inserts between words sits at the x of the
    #: character before it.
    chars: tuple[tuple[float, str], ...]

    @property
    def text(self) -> str:
        """The line's characters, as the page draws them."""
        return "".join(c for _, c in self.chars)

    @property
    def words(self) -> str:
        """The line's words, each run of whitespace one space."""
        return " ".join(self.text.split())


class PdfFile[RowT: msgspec.Struct](TextFile[RowT]):
    """A table read out of a PDF's text layer: a format reads its records' cells from the lines (:meth:`_records`)."""

    @typing.override
    def _label(self, name: str) -> str | None:
        # No header names a PDF's columns: a format's records hold its fields' cells by position.
        return name

    def _lines(self, body: bytes) -> list[PdfLine]:
        """Return the lines the format reads its records from: :func:`text_layer`'s, unless a format orders them."""
        return text_layer(body)

    @abc.abstractmethod
    def _records(self, lines: Sequence[PdfLine]) -> Sequence[Sequence[str | None]]:
        """Read the records out of a PDF's *lines*: each its fields' text cells, in order, or raise ValueError."""

    @typing.override
    def _cells(self, body: bytes) -> pl.DataFrame:
        records = self._records(self._lines(body))
        for n, cells in enumerate(records, 1):
            if len(cells) != len(self._labels):
                msg = f"record {n} has {len(cells)} cells, not {len(self._labels)}"
                raise ValueError(msg)
        return pl.DataFrame(records, schema=dict.fromkeys(self._labels, pl.String), orient="row")


class LaidOutPdf[RowT: msgspec.Struct](PdfFile[RowT]):
    """A PDF read as its pages lay it out: lines top to bottom, a table's cells found by their columns' places.

    Each page's lines are sorted top to bottom, then left to right, and segments pdfminer groups apart but the page
    draws on one baseline (tops less than ``_baseline`` apart) are one line, their characters left to right. A format
    reads a table with three steps: :meth:`_edges` finds each column's left edge on its header line, :meth:`_runs`
    splits lines into rows by the gap between them, and :meth:`_column_cells` gives each column its characters.
    """

    #: Points two segments' tops may differ by and still be one line.
    _baseline: typing.ClassVar[float] = 1.0
    #: Points a cell's character may sit left of its column's edge: a format whose cells are drawn a little left of
    #: their labels sets it.
    _slack: typing.ClassVar[float] = 0.0
    #: The most points between two lines of one row; each format measures its own.
    _row_gap: typing.ClassVar[float]

    @typing.override
    def _lines(self, body: bytes) -> list[PdfLine]:
        drawn = sorted(
            (line for line in super()._lines(body) if line.text.strip()),
            key=lambda line: (line.page, -line.top, line.chars[0][0]),
        )
        merged: list[list[PdfLine]] = []
        for line in drawn:
            if merged and merged[-1][0].page == line.page and merged[-1][0].top - line.top < self._baseline:
                merged[-1].append(line)
            else:
                merged.append([line])
        return [_joined(segments) for segments in merged]

    def _edges(self, line: PdfLine, labels: Sequence[str]) -> list[float] | None:
        """Return each column's left edge, where its label's first word starts on header *line*; ``None`` if one is not.

        The labels are found in order, each a whole word in any case.
        """
        edges: list[float] = []
        at = 0
        for label in labels:
            found = re.compile(rf"(?<!\S){re.escape(label.split()[0])}(?!\S)", re.IGNORECASE).search(line.text, at)
            if found is None:
                return None
            edges.append(line.chars[found.start()][0])
            at = found.end()
        return edges

    def _column_cells(self, lines: Sequence[PdfLine], edges: Sequence[float]) -> list[str]:
        """Return each column's cell: every character at or right of its edge (less ``_slack``), lines joined.

        A character left of the first edge is the first column's; each cell's runs of whitespace are one space.
        """
        cells: list[list[str]] = [[] for _ in edges]
        for line in lines:
            parts: list[list[str]] = [[] for _ in edges]
            for x, c in line.chars:
                parts[max((i for i, edge in enumerate(edges) if edge <= x + self._slack), default=0)].append(c)
            for cell, part in zip(cells, parts, strict=True):
                if "".join(part).strip():
                    cell.append("".join(part))
        return [" ".join(" ".join(cell).split()) for cell in cells]

    def _runs(self, lines: Sequence[PdfLine]) -> list[list[PdfLine]]:
        """Split *lines* into rows: runs no more than ``_row_gap`` points apart, a page break ending one."""
        runs: list[list[PdfLine]] = []
        for before, line in zip([None, *lines], lines, strict=False):
            if before is None or line.page != before.page or before.top - line.top > self._row_gap:
                runs.append([])
            runs[-1].append(line)
        return runs


def _joined(segments: Sequence[PdfLine]) -> PdfLine:
    """Return one line of *segments* on one baseline: left to right, a space before each, at the first's top."""
    chars: list[tuple[float, str]] = []
    for segment in sorted(segments, key=lambda segment: segment.chars[0][0]):
        chars.extend([(segment.chars[0][0], " ")] if chars else [])
        chars.extend(segment.chars)
    # sorted() is stable: each segment's characters, and the space opening it, keep their order
    return PdfLine(segments[0].page, segments[0].top, tuple(sorted(chars, key=lambda c: c[0])))


def text_layer(body: bytes) -> list[PdfLine]:
    """Return each page's lines, page by page, in the order each page draws them.

    An empty body, bytes pdfminer cannot read, and a PDF with no text layer raise ``ValueError``. ``create_pages``, not
    ``get_pages``, which logs a warning for every PDF carrying the "do not extract" flag, as the House's do.
    """
    if not body:
        msg = "the body is empty"
        raise ValueError(msg)
    try:
        lines = [PdfLine(n, line.y1, tuple(_chars(line))) for n, line in _grouped_lines(body)]
    except _UNREADABLE as exc:
        msg = f"the body is not a readable PDF: {exc}"
        raise ValueError(msg) from exc
    if not any(line.text.strip() for line in lines):
        msg = "the PDF has no text layer: a scanned document"
        raise ValueError(msg)
    return lines


def _grouped_lines(body: bytes) -> Iterator[tuple[int, LTTextLine]]:
    """Yield each page's number and its lines: pdfminer's grouper over the page's characters as drawn."""
    laparams = LAParams()
    resources = PDFResourceManager()
    device = PDFPageAggregator(resources, laparams=None)  # None: no layout analysis, the characters as drawn
    interpreter = PDFPageInterpreter(resources, device)
    for n, page in enumerate(PDFPage.create_pages(PDFDocument(PDFParser(io.BytesIO(body))))):
        interpreter.process_page(page)
        layout = device.get_result()
        if drawn := list(_drawn(layout)):
            for line in LTLayoutContainer(layout.bbox).group_objects(laparams, drawn):
                line.analyze(laparams)  # inserts the inter-word spaces (word_margin) and the line's end
                yield n, line


def _drawn(items: Iterable[object]) -> Iterator[LTChar]:
    """Yield the characters of a page's layout in the order the page draws them, descending into its containers."""
    for item in items:
        if isinstance(item, LTChar):
            yield item
        elif isinstance(item, LTContainer):
            yield from _drawn(item)


def _chars(line: LTTextLine) -> Iterator[tuple[float, str]]:
    """Yield each character of *line* and its x, a NUL deleted; an inserted space at its left neighbor's x."""
    x = 0.0
    for item in line:
        if isinstance(item, LTChar):
            x = item.x0
        elif item.get_text() == "\n":
            continue  # the line's end, the LTAnno analyze() appends
        # No page shows a NUL: a font may map small-capital glyphs to it in its ToUnicode table (the House's forms,
        # measured), and pdfminer reports what the PDF says, so every one is deleted as each line is built.
        yield from ((x, c) for c in item.get_text().replace(_NUL, ""))
