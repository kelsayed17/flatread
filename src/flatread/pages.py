"""HTML pages read into their row struct's fields: a page's table, or records that are elements.

**The readers** are :class:`HtmlFile`, a page's one table read into its row struct, and :class:`HtmlRecords`,
records that are elements a CSS selector finds (a calendar's meeting rows, a list of links), each field the one
element its ``Label`` selects, under headings that may state fields (a panel's year). A format that reads a page its
own way overrides a hook (:meth:`HtmlFile._grid`, and either reader's ``_value``: :meth:`HtmlFile._value`,
:meth:`HtmlRecords._value`) with the helpers below: :func:`parse_html`, :func:`table_header`, :func:`table_cells`,
:func:`table_frame`, :func:`cell_text`, :func:`text`, :func:`href`.

Both read a cell as its text, or as its link's ``href`` for a :class:`~flatread.fields.Link` field, and a format reads
one its own way through ``_value``; fields that share a cell (SEC's ``File`` cell: its link's text, its notes and its
``href``) each key their own column. Both type the cells as :class:`~flatread.core.TextFile` does. The cell of a
field that is no text (a number, a date or time, a boolean) is one text run: one an element or a comment splits into
two (``5.3<sup>1</sup>``, ``1<br>25``, a hidden ``<span>``) raises, rather than reading ``5.31`` or ``125``.

**The parser.** Scrapling runs over lxml, and only this module loads it, the one place that builds a
:class:`scrapling.parser.Selector`, with the settings below.

**The errors**: a table whose header names a column twice, or that holds another table, raises
:class:`~flatread.errors.ReadError`, and a row with more or fewer cells than its header
:class:`~flatread.errors.RaggedRowError` naming the row (:func:`table_frame`). A row is numbered as the typing numbers
it, among the rows with cells, from 1 under the header; a ``<tfoot>``'s rows (totals) are no rows. A ``bytes`` page
whose bytes are not its charset raises (:func:`parse_html`), and :class:`HtmlFile` refuses a page cut short: one in
which a ``<table>`` has no ``</table>``, the end tag HTML never leaves out.

Scrapling parses with libxml2's lenient HTML parser (through lxml), which recovers from the malformed markup these
pages actually serve: unclosed ``<td>`` / ``<tr>``, unquoted attributes, stray text. An XML document is never parsed
that way: Scrapling has no XML parser, and its HTML parse of an XML index lowercases the tags, drops the namespace and
silently keeps half of a truncated body (measured 2026-10-05). :func:`~flatread.markup.parse_xml`
parses one strictly with ``lxml.etree`` (the House Clerk's FD index, the ECB's dataflow list).

**Five Scrapling defaults would change what these readers return**, so this module sets each one explicitly. Measured
2026-10-05 against the lxml code this replaced; every expected value in ``tests/test_pages.py`` pins that code's
output. The first three are the parse's, which :func:`parse_html` does itself with Scrapling's lxml settings and hands
to ``Selector`` as its root:

* ``huge_tree=True`` switches off libxml2's depth and expansion limits. ``False`` keeps them: a document nested past
  256 levels is cut, as it always was.
* ``encoding="utf-8"`` overrides the page's ``<meta charset>``. ``None`` lets lxml detect it, which ``Selector``'s
  ``str``-typed ``encoding`` does not take. A ``bytes`` page that declares nothing (no BOM, no ``<meta>`` charset in
  its head) is read as UTF-8, strictly, rather than left to libxml2's Latin-1 fallback, which turns UTF-8 text into
  mojibake with no error; one that declares a charset is first decoded in it, strictly, since libxml2 reads a byte
  that is not that charset as U+FFFD, again with no error.
* An empty body parses to ``<html/>``, and a body holding markup but no element (a doctype, a comment, a processing
  instruction, CDATA, a stray end tag) to no root at all. :func:`parse_html` returns ``None`` for both: nothing to
  parse.
* ``keep_comments=False`` removes comments and CDATA before the text is read, which merges the text runs on either side
  of one: ``<td>12<!-- c --> 34</td>`` reads ``"12 34"``. ``True`` keeps them, as lxml did (``"1234"``); a comment's own
  text is still never a cell's text.
* ``get_all_text`` skips ``<script>``/``<style>`` text and joins runs with newlines. The text helpers pass
  ``ignore_tags=()`` and ``separator=""``: a cell's text is ALL of its descendant text.

Two differences are accepted rather than overridden, since both are fixed inside ``Selector``'s constructor:

* Scrapling deletes NUL bytes before parsing, where lxml turned each into U+FFFD. On ``bytes`` it strips EVERY 0x00
  byte, so a UTF-16 or UTF-32 body becomes unreadable (it parses to no table). No page read has carried a NUL and no
  provider serves UTF-16.
* Scrapling hardcodes ``remove_blank_text=True`` in its parser arguments (no ``Selector`` keyword turns it off), so
  libxml2 deletes some whitespace-only text nodes at parse time, after ``<br>``, ``<sup>``, ``<sub>`` and ``<wbr>``:
  ``<p>A<br> <b>B</b></p>`` reads ``"A B"`` through lxml and ``"AB"`` here. Only :func:`text` can observe it, because
  :func:`cell_text` drops whitespace-only runs anyway.

**On the text helpers.** :func:`cell_text` and :func:`text` are NOT interchangeable. They mirror two BeautifulSoup
calls:

===================================  ===============================
BeautifulSoup                        here
===================================  ===============================
``el.get_text(strip=True)``          :func:`cell_text`
``el.get_text().strip()``            :func:`text`
===================================  ===============================

For ``<td>A <b>B</b> C</td>`` the first yields ``"ABC"`` (each text run stripped before concatenation) and the second
``"A B C"``. Cell values are kept verbatim, so reaching for the wrong one changes data rather than raising.
"""

import codecs
import re
import typing
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from typing import ClassVar, Final

import msgspec
import polars as pl
from lxml.etree import fromstring, iterwalk
from lxml.html import HtmlElement, HTMLParser
from scrapling.parser import Selector

from flatread.core import Column, TextFile
from flatread.errors import EmptyBodyError, RaggedRowError, ReadError
from flatread.fields import Link

__all__ = [
    "Element",
    "Grid",
    "HtmlFile",
    "HtmlRecords",
    "anchor",
    "cell_text",
    "cells",
    "href",
    "parse_html",
    "rows",
    "table_cells",
    "table_frame",
    "table_header",
    "text",
]

#: The element type every page decoder walks. A provider imports this alias, never scrapling.
type Element = Selector


def parse_html(markup: str | bytes) -> Element | None:
    """Parse *markup* leniently, returning ``None`` when there is nothing to parse.

    Accepts a whole page or a bare fragment. Scrapling wraps a fragment in ``<html><body>``, so the root is always
    ``<html>`` and a caller selects what it wants with ``css`` (``doc.css("table")`` finds a bare-table fragment as
    readily as a full page). An empty or whitespace-only body is returned as ``None`` BEFORE Scrapling sees it, because
    Scrapling would parse it as an empty ``<html/>`` and the caller could not tell "the page is empty" from "the page
    has no table".

    A body with markup but no element (a doctype, a comment, a processing instruction, CDATA, a stray end tag) is
    ``None`` as well: lxml parses it to no root.

    The parse is Scrapling's own, done here and handed to it as ``root``, because one of its settings is not
    Scrapling's to give: ``encoding=None`` lets lxml honor the page's ``<meta charset>``, where Scrapling's
    ``encoding`` is typed ``str`` and its ``"utf-8"`` overrides the page. The rest are Scrapling's: ``recover``,
    ``remove_blank_text`` and ``compact``, with NUL deleted first and a ``str`` body stripped. ``huge_tree=False``
    keeps libxml2's limits, and comments are kept (``keep_comments``) so the text runs around one are not merged.
    ``adaptive`` stays at its default, off, so no fingerprint store is built and Scrapling logs nothing.

    A ``bytes`` page that declares no encoding, neither a byte-order mark nor a ``<meta>`` charset in its head
    (libxml2 honors a ``<meta charset>`` past the WHATWG prescan's first 1024 bytes, after a long ``<style>`` or
    ``<script>``), is UTF-8, which every provider read serves: libxml2 would fall back to Latin-1 and read UTF-8 text
    as mojibake. A declared charset is honored as declared, and libxml2 decodes the page in it, but first Python does,
    strictly: a byte that is not that charset (libxml2 reads one as U+FFFD), or a charset Python does not know, raises
    :class:`~flatread.errors.ReadError` naming it, as a byte that is not UTF-8 does on a page that declares nothing.
    """
    if not markup.strip():
        return None
    if isinstance(markup, str):
        body: str | bytes = markup.strip().replace(chr(0), "")
        encoding = None
    else:
        body = markup.replace(bytes(1), b"")
        encoding = _declared_or_utf8(markup)
    parser = HTMLParser(
        recover=True,
        remove_blank_text=True,
        remove_comments=False,
        encoding=encoding,
        compact=True,
        huge_tree=False,
    )
    root = fromstring(body, parser=parser, base_url="")
    if not isinstance(root, HtmlElement):  # nothing to parse; an HTMLParser builds every element as an HtmlElement
        return None
    return Selector(root=root, keep_comments=True)


#: The byte-order marks libxml2 reads an encoding from, each the encoding it declares.
_BOMS: Final = {codecs.BOM_UTF8: "utf-8-sig", codecs.BOM_UTF16_LE: "utf-16-le", codecs.BOM_UTF16_BE: "utf-16-be"}
#: An encoding a page declares in its markup, its name the group: a ``<meta>`` tag's ``charset``, either form
#: (``<meta charset>`` or an ``http-equiv`` Content-Type's ``charset=``). Text that only mentions ``charset=``
#: declares nothing. An XML declaration's ``encoding`` is none, as in WHATWG's prescan: libxml2's HTML parser does not
#: read it. ``[^<>]``, not ``[^>]``: a search from each ``<meta`` stops at the next tag, so the scan is linear.
_DECLARED: Final = re.compile(rb"<meta\b[^<>]*\bcharset\s*=\s*[\"']?\s*([^\s\"'<>;]*)", re.IGNORECASE)
#: Where the head ends: a declaration is read before it, as libxml2 reads one anywhere in ``<head>``.
_HEAD_END: Final = re.compile(rb"</head\b|<body\b", re.IGNORECASE)
#: How far a page without a ``</head>`` or ``<body>`` is searched, as WHATWG's prescan reads the first 1024 bytes.
_PRESCAN: Final = 2048


def _declared_or_utf8(body: bytes) -> str | None:
    """Return ``None`` when *body* declares its encoding (lxml reads it), else ``"utf-8"``, having checked it is.

    The declaration is a byte-order mark, or a ``<meta>`` tag's in the head: before ``</head>`` or ``<body>``, or in
    the first :data:`_PRESCAN` bytes of a page that writes neither. *body* is decoded in the charset it declares, or
    UTF-8, strictly: a charset Python does not know, or one stray byte that is not it, raises.
    """
    head = body[: end.start()] if (end := _HEAD_END.search(body)) else body[:_PRESCAN]
    declared = next((name for mark, name in _BOMS.items() if body.startswith(mark)), None)
    if declared is None and (found := _DECLARED.search(head)) is not None:
        declared = found[1].decode("latin-1")
    try:
        body.decode("utf-8" if declared is None else declared)
    except LookupError:  # no codec, or one that is no text encoding (``base64``)
        msg = f"the page declares the charset {declared!r}, which is no charset Python reads"
        raise ReadError(msg) from None
    except UnicodeDecodeError as exc:
        msg = (
            f"the page declares the charset {declared!r} and byte {exc.start} is not {declared}"
            if declared is not None
            else f"the page declares no charset and byte {exc.start} is not UTF-8"
        )
        raise ReadError(msg) from None
    return None if declared is not None else "utf-8"


def cell_text(el: Element) -> str:
    """Descendant text with each run stripped, concatenated without a separator: bs4's ``get_text(strip=True)``.

    ``ignore_tags=()`` keeps ``<script>``/``<style>`` text, which Scrapling's default drops. An element's own tail (the
    text after its closing tag) is never included, so inter-cell whitespace cannot leak into a value.
    """
    return str(el.get_all_text(separator="", strip=True, ignore_tags=()))


def _runs(el: Element) -> list[str]:
    """*el*'s text runs as :func:`cell_text` reads them: each descendant text node stripped, the blank ones left out."""
    return [run for node in el.xpath(".//text()") if (run := str(node).strip())]


def text(el: Element) -> str:
    """Descendant text verbatim, stripped only at the ends: bs4's ``get_text().strip()``.

    Inner whitespace between runs is preserved, unlike :func:`cell_text`. Used where the source text is prose rather
    than a data cell (the eFD report title, where ``"Periodic <b>Transaction</b> Report"`` must keep its spaces).
    ``valid_values=False`` keeps whitespace-only runs, which is where those spaces live. The parser has already deleted
    some such runs (``remove_blank_text``, after ``<br>`` for one; see the module docstring), so this keeps the runs
    that survive parsing.
    """
    return str(el.get_all_text(separator="", strip=False, ignore_tags=(), valid_values=False)).strip()


def cells(row: Element) -> list[Element]:
    """Every ``<td>`` / ``<th>`` under *row*, in document order: recursive, like bs4's ``find_all(["td", "th"])``.

    A nested table's cells are included. Document order (the CSS group compiles to one XPath union) is what lets a
    mixed header row zip against its columns correctly.
    """
    return list(row.css("td, th"))


def rows(table: Element) -> list[Element]:
    """Every ``<tr>`` under *table*, in document order. Callers slice ``[1:]`` to skip the header row.

    A table that holds another ``<table>`` raises :class:`~flatread.errors.ReadError`, naming the table's
    header: the inner table's rows would read as the outer one's, and the outer cell's text would run the inner table's
    cells together. No recorded page nests one (measured 2026-11-06 over every page fixture: one level deep in all).
    """
    found = list(table.css("tr"))
    if table.xpath(".//table"):
        header = [cell_text(c) for c in cells(found[0])] if found else []
        msg = f"the table under header {header!r} holds another table"
        raise ReadError(msg)
    return found


def table_header(table: Element) -> list[str] | None:
    """Return the table's header: the first ``<tr>``'s cell text, in document order.

    Returns ``None`` when the element carries no ``<tr>`` at all (nothing to key against), which callers read as
    no-data. Reads and never judges: a repeated column name or a nested table is :func:`table_frame`'s to refuse,
    because the Senate eFD decoder reads EVERY table's header to decide whether it consumes the table at all, and a
    table it does not consume must not fail the page.
    """
    first = table.css("tr").first
    if first is None:
        return None
    return [cell_text(c) for c in cells(first)]


def table_cells(table: Element, columns: list[str]) -> tuple[list[list[Element]], list[int]]:
    """Return *table*'s rows after the first as their cells under *columns* (from :func:`table_header`).

    Returns one list of cell elements per ``<tr>`` with cells, and each row's number, which an error names a row by:
    its place among the rows with cells, from 1 under the header, as the typing numbers a row. A ``<tr>`` with no
    cells is no row (Farside's live flow table carries one), nor is a ``<tfoot>``'s: a table's footer states its
    totals, which are no records. Two shapes are refused rather than read, because reading either loses data silently:

    * a row with more or fewer cells than *columns* raises :class:`~flatread.errors.RaggedRowError`, naming the row:
      the extra cells have no column to land under, and a short row's cells cannot be placed;
    * a table that holds another table raises :class:`~flatread.errors.ReadError` (:func:`rows`).
    """
    read: list[list[Element]] = []
    for tr in rows(table)[1:]:
        if not (found := cells(tr)) or tr.xpath("ancestor::tfoot"):
            continue
        if len(found) != len(columns):
            values = [cell_text(c) for c in found]
            msg = f"row {len(read) + 1} of the table under header {columns!r} has {len(values)} cells: {values!r}"
            raise RaggedRowError(msg)
        read.append(found)
    return read, list(range(1, len(read) + 1))


def table_frame(table: Element, columns: list[str]) -> tuple[pl.DataFrame, pl.Series]:
    """Read *table*'s rows after the first into a String frame under *columns*: :func:`table_cells`' cells' text.

    Returns the frame, each cell's text verbatim (:func:`cell_text`), and each row's place, as :func:`table_cells`
    numbers it, with its errors. A column name that repeats raises :class:`~flatread.errors.ReadError`:
    keyed by name, the later column would overwrite the earlier one. Checked before any row, so a header-only table is
    refused too.
    """
    _check_unrepeated(columns, columns)
    read, numbers = table_cells(table, columns)
    frame = pl.DataFrame(
        [[cell_text(c) for c in found] for found in read], schema=dict.fromkeys(columns, pl.String), orient="row"
    )
    return frame, pl.Series("row", numbers, dtype=pl.Int64)


def _check_unrepeated(header: list[str], repeated: Collection[str]) -> None:
    """Refuse a *header* naming any of the labels in *repeated* more than once."""
    repeated = set(repeated)
    if twice := sorted(name for name, count in Counter(header).items() if count > 1 and name in repeated):
        msg = f"the table's header {header!r} names {twice!r} more than once"
        raise ReadError(msg)


def anchor(cell: Element) -> Element:
    """Return *cell*'s one link: the cell itself if it is an ``<a>``, else its one ``<a>``; another count raises."""
    if cell.tag == "a":
        return cell
    found = list(cell.css("a"))
    if len(found) != 1:
        msg = f"holds {len(found)} links, not one: {cell_text(cell)!r}"
        raise ReadError(msg)
    return found[0]


def href(cell: Element) -> str:
    """Return the ``href`` of *cell*'s one link (:func:`anchor`): ``""``, no value, for a cell with no link or none."""
    if cell.tag != "a" and not cell.css("a"):
        return ""
    # Scrapling hands an attribute back as its own ``str`` subclass, which a struct keeps, so it is made a plain one.
    return str(anchor(cell).attrib.get("href") or "")


class _HtmlText[RowT: msgspec.Struct](TextFile[RowT]):
    """What the HTML readers share: a field's cell read as its text, or as its link's (``Link``).

    Fields may read one cell, each its own way (SEC's ``File`` cell: its link's text, its notes, its ``href``): each
    such field's column is keyed ``label (field)``, so every column of the frame stays one field's.
    """

    def __init__(self, row: type[RowT], *, null_values: Collection[str] = ()) -> None:
        super().__init__(row, null_values=null_values)
        self._links = frozenset(
            field.name
            for field in msgspec.structs.fields(row)
            if typing.get_origin(field.type) is typing.Annotated
            and any(isinstance(meta, Link) for meta in typing.get_args(field.type)[1:])
        )
        shared = Counter(column.label for column in self._columns)
        #: Each column's cell label (a header's, or a record's selector), by its field.
        self._under = {column.field: column.label for column in self._columns}
        self._columns = [
            column._replace(label=f"{column.label} ({column.field})") if shared[column.label] > 1 else column
            for column in self._columns
        ]
        self._labels = [column.label for column in self._columns]

    def _value(self, cell: Element, column: Column) -> str:
        """Return the text *column* reads from *cell*: its link's ``href`` for a ``Link`` field, else its text.

        The cell of a field that is no text (a number, a date or time, a boolean) is one text run: one an element or a
        comment splits into two (``5.3<sup>1</sup>``, ``1<br>25``) raises, rather than reading ``5.31`` or ``125``.
        A format reads a cell its own way by overriding it; a ``ValueError`` it raises is a ``ReadError`` naming the
        cell and its row or record.
        """
        if column.field in self._links:
            return href(cell)
        if column.scalar is str:
            return cell_text(cell)
        if len(runs := _runs(cell)) > 1:
            msg = f"holds the text runs {runs!r}, not one"
            raise ValueError(msg)
        return "".join(runs)

    def _read_cell(self, cell: Element, column: Column, where: str) -> str:
        try:
            return self._value(cell, column)
        except ValueError as e:
            label = self._under[column.field]
            msg = f"{where}: its {f'{label!r} ' if label else ''}cell {e}"
            raise ReadError(msg) from e


def _one_run(cell: Element, label: str, number: int) -> str:
    """Return *cell*'s one text run, or raise ``ReadError`` naming the cell where markup splits it (see ``_value``)."""
    if len(runs := _runs(cell)) > 1:
        msg = f"row {number}: its {label!r} cell holds the text runs {runs!r}, not one"
        raise ReadError(msg)
    return "".join(runs)


class Grid(typing.NamedTuple):
    """A table as :class:`HtmlFile` reads it: its header's labels, its rows' cells under them, each row's place.

    *stated* is the text of fields the table writes once for every row (a title row's report date), by label.
    """

    header: list[str]
    rows: list[list[Element]]
    numbers: list[int]
    stated: Mapping[str, str]


class HtmlFile[RowT: msgspec.Struct](_HtmlText[RowT]):
    """A clean HTML table: the one table ``_table_selector`` finds, its first row the struct's labels, a row a record.

    Each row is read as :func:`table_cells` reads it, a ``<tr>`` with no cells no row nor a ``<tfoot>``'s (totals),
    each numbered in errors among the rows read, from 1, each cell its text (:func:`cell_text`; one text run for a
    field that is no text, see :meth:`_value`) or, for a :class:`~flatread.fields.Link` field, its link's ``href``,
    and typed as :class:`~flatread.core.TextFile` types it: the header must be the struct's labels, in order, and a
    cell holding nothing is no value. A label the header writes twice is two fields, matched by place; one the header
    writes once may be read by several fields (each its own way, through ``_value``). A page with another count of
    tables raises, and so does a page cut short: one in which a ``<table>`` start tag has no ``</table>`` after it,
    outside comments (the end tag HTML never leaves out; a ``<table>`` written in a script's text counts too). A format
    that answers no table with no rows (``_rows_required = False``) reads a page without one as none. A header cell no
    field reads is read as text under its label, for a format to check and drop.

    A format reads a table of another shape by overriding :meth:`_grid`: rows above the header (a title stating a
    report's date, which leads every row), or a cell spanning rows carried down.
    """

    #: The CSS selector that finds the page's table; a format's subclass narrows it (``table.etf``).
    _table_selector: ClassVar[str] = "table"

    def _long_typed(self, header: Sequence[str]) -> frozenset[str]:
        """Return the header's labels a long table (:class:`~flatread.layout.LongFile`) reads as a typed value.

        Their cells are no field's own, so :meth:`_value` never sees them; each must still be one text run. A header
        the layout refuses is refused again, and named, when the cells are typed.
        """
        try:
            laid = self._layout([label for label in header if label not in {*self._unread, *self._blank_columns}])
        except ReadError:
            return frozenset[str]()
        return frozenset(
            column.label for column in laid or () if column.scalar is not str and column.field not in self._under
        )

    def _tables(self, doc: Element) -> list[Element]:
        """Return the page's tables for this file: every one ``_table_selector`` finds, unless a format overrides it."""
        return list(doc.css(self._table_selector))

    def _grid(self, table: Element) -> Grid:
        """Return *table* as its header's labels and its rows' cells: its first row the header, the rest its rows."""
        if (header := table_header(table)) is None:
            msg = "the table has no row"
            raise ReadError(msg)
        return Grid(header, *table_cells(table, header), stated={})

    @typing.override
    def _cells(self, body: bytes) -> pl.DataFrame:
        doc = parse_html(body)
        if doc is None:
            msg = "the body is empty"
            raise EmptyBodyError(msg)
        if _unclosed(body):
            msg = "the page was cut: a <table> has no </table>"
            raise ReadError(msg)
        tables = self._tables(doc)
        if not tables and not self._rows_required:
            return pl.DataFrame(schema=dict.fromkeys(self._labels, pl.String))
        if len(tables) != 1:
            msg = f"the page holds {len(tables)} of the file's tables, not one"
            raise ReadError(msg)
        header, read, numbers, stated = self._grid(tables[0])
        reading: dict[str, list[Column]] = {label: [] for label in header}
        for column in self._columns:
            reading.get(self._under[column.field], []).append(column)
        written = Counter(header)
        # A label written twice is as many fields, by place; one no field reads, or read by fewer, would collide.
        _check_unrepeated(header, [label for label in written if written[label] != len(reading[label])])
        frame: dict[str, list[str]] = {label: [text] * len(read) for label, text in stated.items()}
        typed = self._long_typed(header)
        before: Counter[str] = Counter()
        for place, label in enumerate(header):
            columns = reading[label] if written[label] == 1 else [reading[label][before[label]]]
            before[label] += 1
            if not columns and label in typed:  # a long table's value cell, typed: one text run, as any typed cell
                frame[label] = [
                    _one_run(found[place], label, number) for found, number in zip(read, numbers, strict=True)
                ]
            elif not columns:
                frame[label] = [cell_text(found[place]) for found in read]
            for column in columns:
                frame[column.label] = [
                    self._read_cell(found[place], column, f"row {number}")
                    for found, number in zip(read, numbers, strict=True)
                ]
        return pl.DataFrame(frame, schema=dict.fromkeys(frame, pl.String))


#: A table's start or end tag (the group: ``b""`` or ``b"/"``), or a comment, whose tags are none; an unclosed comment
#: runs to the end, as libxml2 reads one.
_TABLE_TAGS: Final = re.compile(rb"<!--.*?(?:-->|\Z)|<(/?)table[\s/>]", re.IGNORECASE | re.DOTALL)


def _unclosed(body: bytes) -> bool:
    """Return whether a ``<table>`` in *body* has no ``</table>`` after it: a cut page (a stray one closes none)."""
    tables = 0
    for tag in _TABLE_TAGS.finditer(body):
        if tag[1] == b"":
            tables += 1
        elif tag[1] == b"/" and tables:
            tables -= 1
    return tables > 0


class HtmlRecords[RowT: msgspec.Struct](_HtmlText[RowT]):
    """Records that are elements, not table rows: each one ``_record_selector`` finds, a field per ``Label`` inside.

    A field's ``Label`` is a CSS selector inside the record (``".fomc-meeting__month"``, ``'a[href$=".pdf" i]'``),
    ``""`` the record itself, and it must find exactly one element (or none, for a field that admits ``None``: no
    value), read as its text (one text run for a field that is no text, see :meth:`_value`) or, for a
    :class:`~flatread.fields.Link` field, its link's ``href``. A format finds its records otherwise: :meth:`_is_record`
    leaves some out, and :meth:`_records` checks the page.

    A format whose records sit under headings (a year's panel) names them: each element ``_heading_selector`` finds
    before a record heads it when its text fullmatches ``_heading_pattern`` (another is no heading: a ``p`` of prose),
    each named group the text of the field of its name, on every record it heads; such a field names no ``Label``. A
    record under no such heading, or a heading that heads no record, raises.
    """

    #: The CSS selector that finds each record.
    _record_selector: ClassVar[str]
    #: The CSS selector that finds the headings above the records, if any.
    _heading_selector: ClassVar[str | None] = None
    #: What a heading's text fullmatches: each named group the field it states.
    _heading_pattern: ClassVar[re.Pattern[str]] = re.compile("")

    @typing.override
    def _label(self, name: str) -> str | None:
        return (
            name
            if self._heading_selector is not None and name in self._heading_pattern.groupindex
            else super()._label(name)
        )

    def _records(self, doc: Element) -> list[Element]:
        """Return every element ``_record_selector`` finds, in page order: a format checks the page here."""
        return list(doc.css(self._record_selector))

    def _is_record(self, element: Element) -> bool:
        """Return whether *element*, which ``_record_selector`` finds, is a record: yes, unless a format says no."""
        del element
        return True

    def _headed(self, doc: Element, found: list[Element]) -> list[dict[str, str | None] | None]:
        """Return what each of the *found* records is stated by its heading (``None``: it sits under no heading).

        A record's heading is the last stating heading that ends before it starts (XPath's ``following::``: a record
        inside a heading is not its), found in one walk of the page in document order. Scrapling's elements have no
        identity to compare, so the walk compares the lxml elements under them.
        """
        if self._heading_selector is None:
            return [{} for _ in found]
        at = {record._root: n for n, record in enumerate(found)}  # noqa: SLF001 (the element's identity)
        stating = {
            heading._root: (heading, match)  # noqa: SLF001
            for heading in doc.css(self._heading_selector)
            if (match := self._heading_pattern.fullmatch(cell_text(heading)))
        }
        headed: list[dict[str, str | None] | None] = [None] * len(found)
        heading: tuple[Element, re.Match[str]] | None = None
        heads = 0
        for event, element in iterwalk(doc._root, events=("start", "end")):  # noqa: SLF001
            if event == "start" and element in at and heading is not None:
                headed[at[element]] = dict(heading[1].groupdict(default=None))
                heads += 1
            elif event == "end" and element in stating:
                _check_heads(heading, heads)
                heading, heads = stating[element], 0
        _check_heads(heading, heads)
        return headed

    @typing.override
    def _cells(self, body: bytes) -> pl.DataFrame:
        doc = parse_html(body)
        if doc is None:
            msg = "the body is empty"
            raise EmptyBodyError(msg)
        found = self._records(doc)
        headed = zip(found, self._headed(doc, found), strict=True) if found else ()
        records = [(record, stated) for record, stated in headed if self._is_record(record)]
        if not records and self._rows_required:
            msg = "the page holds none of the file's records"
            raise EmptyBodyError(msg)
        read: list[list[str | None]] = []
        for number, (record, stated) in enumerate(records, 1):
            if stated is None:
                msg = f"record {number} sits under no heading matching {self._heading_pattern.pattern!r}"
                raise ReadError(msg)
            values: list[str | None] = []
            for column in self._columns:
                if column.field in self._heading_pattern.groupindex and self._heading_selector is not None:
                    values.append(stated[column.field])
                    continue
                under = self._under[column.field]
                found = [record] if not under else list(record.css(under))
                if not found and column.field not in self._required:
                    values.append(None)
                    continue
                if len(found) != 1:
                    msg = f"record {number} holds {len(found)} elements {under!r} finds, not one"
                    raise ReadError(msg)
                values.append(self._read_cell(found[0], column, f"record {number}"))
            read.append(values)
        return pl.DataFrame(read, schema=dict.fromkeys(self._labels, pl.String), orient="row")


def _check_heads(heading: tuple[Element, re.Match[str]] | None, heads: int) -> None:
    """Refuse a stating *heading* that *heads* no record before the next one, or the page's end."""
    if heading is not None and not heads:
        msg = f"the headings {[cell_text(heading[0])]!r} head no record"
        raise ReadError(msg)
