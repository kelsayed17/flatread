"""What the page decoders share: an HTML page parsed leniently, its tables and a cell's text.

A provider whose endpoint serves an HTML page rather than JSON or a file (Farside's ETF flow table, Yahoo's earnings
calendar, the Fed's FOMC calendar, the Senate eFD search results, CFTC's swaps-report archive, Cboe's daily market
statistics, Treasury's allotment page) parses it through this module, the one place that builds a
:class:`scrapling.parser.Selector`, with the settings below.

**The native readers** are :class:`HtmlFile`, a page's table read into its row struct, and :class:`HtmlRecords`,
records that are elements a CSS selector finds (Treasury's links, the FOMC calendar's meeting rows), each field the one
element its ``Label`` selects, under headings that may state fields (a panel's year). Both read a cell as its text, or
as its link's ``href`` for a :class:`~flatread.fields.Link` field, and a format reads one its own way
through ``_value``; fields that share a cell (SEC's ``File`` cell: its link's text, its notes and its ``href``) each key
their own column. Both type the cells as :class:`~flatread.core.TextFile` does.

**The parser.** Scrapling runs over lxml, and only this module loads it.

**The errors** are :mod:`flatread.errors`'s: a table whose header names a column twice, or that holds another table,
raises :class:`~flatread.errors.ReadError`, and a row with more or fewer cells than its header
:class:`~flatread.errors.RaggedRowError` naming the row (:func:`table_frame`).

Scrapling parses with libxml2's lenient HTML parser (through lxml), which recovers from the malformed markup these
pages actually serve: unclosed ``<td>`` / ``<tr>``, unquoted attributes, stray text. An XML document is never parsed
that way: Scrapling has no XML parser, and its HTML parse of an XML index lowercases the tags, drops the namespace and
silently keeps half of a truncated body (measured 2026-10-05). :func:`~flatread.markup.parse_xml`
parses one strictly with ``lxml.etree`` (the House Clerk's FD index, the ECB's dataflow list).

**Five Scrapling defaults would change what these decoders return**, so this module sets each one explicitly. Measured
2026-10-05 against the lxml code this replaced; every expected value in ``tests/test_pages.py`` pins that code's
output. The first three are the parse's, which :func:`parse_html` does itself with Scrapling's lxml settings and hands
to ``Selector`` as its root:

* ``huge_tree=True`` switches off libxml2's depth and expansion limits. ``False`` keeps them: a document nested past
  256 levels is cut, as it always was.
* ``encoding="utf-8"`` overrides the page's ``<meta charset>``. ``None`` lets lxml detect it, which ``Selector``'s
  ``str``-typed ``encoding`` does not take. A ``bytes`` page that declares nothing (no BOM, no ``<meta>`` charset in
  its head) is read as UTF-8, strictly, rather than left to libxml2's Latin-1 fallback, which turns UTF-8 text into
  mojibake with no error.
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
from collections.abc import Collection, Mapping
from typing import ClassVar, Final

import msgspec
import polars as pl
from lxml.etree import fromstring
from lxml.html import HtmlElement, HTMLParser
from scrapling.core.translator import HTMLTranslator
from scrapling.parser import Selector

from flatread.core import TextFile, _Column
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

    A ``bytes`` page that declares no encoding, neither a byte-order mark nor a ``charset`` anywhere in it (libxml2
    honors a ``<meta charset>`` past the WHATWG prescan's first 1024 bytes, after a long ``<style>`` or ``<script>``),
    is UTF-8, which every provider read serves: libxml2 would fall back to Latin-1 and read UTF-8 text as mojibake.
    Bytes that are not UTF-8 then raise :class:`~flatread.errors.ReadError` naming the first. A declared
    charset is honored as declared.
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


#: The byte-order marks libxml2 reads an encoding from.
_BOMS: Final = (codecs.BOM_UTF8, codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
#: An encoding a page declares in its markup: a ``<meta>`` tag's ``charset``, either form (``<meta charset>`` or an
#: ``http-equiv`` Content-Type's ``charset=``). Text that only mentions ``charset=`` declares nothing. An XML
#: declaration's ``encoding`` is none, as in WHATWG's prescan: libxml2's HTML parser does not read it.
_DECLARED: Final = re.compile(rb"<meta\b[^>]*\bcharset\s*=", re.IGNORECASE)
#: Where the head ends: a declaration is read before it, as libxml2 reads one anywhere in ``<head>``.
_HEAD_END: Final = re.compile(rb"</head\b|<body\b", re.IGNORECASE)
#: How far a page without a ``</head>`` or ``<body>`` is searched, as WHATWG's prescan reads the first 1024 bytes.
_PRESCAN: Final = 2048


def _declared_or_utf8(body: bytes) -> str | None:
    """Return ``None`` when *body* declares its encoding (lxml reads it), else ``"utf-8"``, having checked it is.

    The declaration is a ``<meta>`` tag's in the head: before ``</head>`` or ``<body>``, or in the first
    :data:`_PRESCAN` bytes of a page that writes neither. A page declaring nothing is UTF-8, strictly: one stray
    byte that is not raises.
    """
    head = body[: end.start()] if (end := _HEAD_END.search(body)) else body[:_PRESCAN]
    if body.startswith(_BOMS) or _DECLARED.search(head):
        return None
    try:
        body.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = f"the page declares no charset and byte {exc.start} is not UTF-8"
        raise ReadError(msg) from None
    return "utf-8"


def cell_text(el: Element) -> str:
    """Descendant text with each run stripped, concatenated without a separator: bs4's ``get_text(strip=True)``.

    ``ignore_tags=()`` keeps ``<script>``/``<style>`` text, which Scrapling's default drops. An element's own tail (the
    text after its closing tag) is never included, so inter-cell whitespace cannot leak into a value.
    """
    return str(el.get_all_text(separator="", strip=True, ignore_tags=()))


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

    Returns one list of cell elements per ``<tr>`` with cells, and each row's 1-based place among the ``<tr>`` after
    the header, the ``numbers=`` :func:`~flatread.cells.read_table` names a row by. A ``<tr>`` with no cells is no row
    (Farside's live flow table carries one). Two shapes are refused rather than read, because reading either loses data
    silently:

    * a row with more or fewer cells than *columns* raises :class:`~flatread.errors.RaggedRowError`, naming the row:
      the extra cells have no column to land under, and a short row's cells cannot be placed;
    * a table that holds another table raises :class:`~flatread.errors.ReadError` (:func:`rows`).
    """
    read: list[list[Element]] = []
    numbers: list[int] = []
    for number, tr in enumerate(rows(table)[1:], start=1):
        found = cells(tr)
        if not found:
            continue
        if len(found) != len(columns):
            values = [cell_text(c) for c in found]
            msg = f"row {number} of the table under header {columns!r} has {len(values)} cells: {values!r}"
            raise RaggedRowError(msg)
        read.append(found)
        numbers.append(number)
    return read, numbers


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

    def __init__(self, row: type[RowT]) -> None:
        super().__init__(row)
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

    def _value(self, cell: Element, column: _Column) -> str:
        """Return the text *column* reads from *cell*: its link's ``href`` for a ``Link`` field, else its text.

        A format reads a cell its own way by overriding it; a ``ValueError`` it raises is prefixed with the cell.
        """
        return href(cell) if column.field in self._links else cell_text(cell)

    def _read_cell(self, cell: Element, column: _Column, where: str) -> str:
        try:
            return self._value(cell, column)
        except ValueError as e:
            label = self._under[column.field]
            msg = f"{where}: its {f'{label!r} ' if label else ''}cell {e}"
            raise ReadError(msg) from e


class Grid(typing.NamedTuple):
    """A table as :class:`HtmlFile` reads it: its header's labels, its rows' cells under them, each row's place.

    *stated* is the text of fields the table writes once for every row (a title row's report date), by label.
    """

    header: list[str]
    rows: list[list[Element]]
    numbers: list[int]
    stated: Mapping[str, str]


class HtmlFile[RowT: msgspec.Struct](_HtmlText[RowT]):
    """A clean HTML table: the page's one table ``_table`` finds, its first row the row struct's labels, a row a record.

    Each row is read as :func:`table_cells` reads it, a ``<tr>`` with no cells no row, each cell its text
    (:func:`cell_text`) or, for a :class:`~flatread.fields.Link` field, its link's ``href``, and typed as
    :class:`~flatread.core.TextFile` types it: the header must be the struct's labels, in order, and a
    cell holding nothing is no value. A label the header writes twice is two fields, matched by place; one the header
    writes once may be read by several fields (each its own way, through ``_value``). A page with another count of
    tables raises; a format that answers no table with no rows (``_rows_required = False``) reads a page without one
    as none. A header cell no field reads is read as text under its label, for a format to check and drop.

    A format reads a table of another shape by overriding :meth:`_grid`: rows above the header (a title stating a
    report's date, which leads every row), or a cell spanning rows carried down.
    """

    #: The CSS selector that finds the page's table; a format's subclass narrows it (``table.etf``).
    _table: ClassVar[str] = "table"

    def _tables(self, doc: Element) -> list[Element]:
        """Return the page's tables for this file: every one ``_table`` finds, unless a format finds them otherwise."""
        return list(doc.css(self._table))

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
        tables = self._tables(doc)
        if not tables and not self._rows_required:
            return pl.DataFrame(schema=dict.fromkeys(self._labels, pl.String))
        if len(tables) != 1:
            msg = f"the page holds {len(tables)} of the file's tables, not one"
            raise ReadError(msg)
        header, read, numbers, stated = self._grid(tables[0])
        reading = {label: [c for c in self._columns if self._under[c.field] == label] for label in header}
        # A label written twice is as many fields, by place; one no field reads, or read by fewer, would collide.
        _check_unrepeated(header, [label for label in header if header.count(label) != len(reading[label])])
        frame: dict[str, list[str]] = {label: [text] * len(read) for label, text in stated.items()}
        for place, label in enumerate(header):
            columns = reading[label] if header.count(label) == 1 else [reading[label][header[:place].count(label)]]
            if not columns:
                frame[label] = [cell_text(found[place]) for found in read]
            for column in columns:
                frame[column.label] = [
                    self._read_cell(found[place], column, f"row {number}")
                    for found, number in zip(read, numbers, strict=True)
                ]
        return pl.DataFrame(frame, schema=dict.fromkeys(frame, pl.String))


class HtmlRecords[RowT: msgspec.Struct](_HtmlText[RowT]):
    """Records that are elements, not table rows: each element ``_record`` finds, a field the one its ``Label`` finds.

    A field's ``Label`` is a CSS selector inside the record (``".fomc-meeting__month"``, ``'a[href$=".pdf" i]'``),
    ``""`` the record itself, and it must find exactly one element (or none, for a field that admits ``None``: no
    value), read as its text or, for a :class:`~flatread.fields.Link` field, its link's ``href``. A format
    finds its records otherwise: :meth:`_is_record` leaves some out, and :meth:`_records` checks the page.

    A format whose records sit under headings (a year's panel) names them: each element ``_heading`` finds before a
    record heads it when its text fullmatches ``_stated`` (another is no heading: a ``p`` of prose), each named group
    the text of the field of its name, on every record it heads; such a field names no ``Label``. A record under no
    such heading, or a heading that heads no record, raises.
    """

    #: The CSS selector that finds each record.
    _record: ClassVar[str]
    #: The CSS selector that finds the headings above the records, if any.
    _heading: ClassVar[str | None] = None
    #: What a heading's text fullmatches: each named group the field it states.
    _stated: ClassVar[re.Pattern[str]] = re.compile("")

    @typing.override
    def _label(self, name: str) -> str | None:
        return name if self._heading is not None and name in self._stated.groupindex else super()._label(name)

    def _records(self, doc: Element) -> list[Element]:
        """Return every element ``_record`` finds, in page order: a format checks the page here, keeping them all."""
        return list(doc.css(self._record))

    def _is_record(self, element: Element) -> bool:
        """Return whether *element*, which ``_record`` finds, is a record: each is, unless a format leaves some out."""
        del element
        return True

    def _headed(self, doc: Element, found: int) -> list[dict[str, str | None] | None]:
        """Return what each of the *found* elements ``_record`` finds is stated by its heading (``None``: no heading).

        Scrapling's elements have no identity to compare, so each stating heading counts the elements that follow it
        (an XPath, in C): the last heading with *n* after it heads every element from the *n*-th from the end.
        """
        if self._heading is None:
            return [{} for _ in range(found)]
        following = HTMLTranslator().css_to_xpath(self._record, prefix="following::")
        stating = [(h, match) for h in doc.css(self._heading) if (match := self._stated.fullmatch(cell_text(h)))]
        after = [len(heading.xpath(following)) for heading, _ in stating]
        headed: list[dict[str, str | None] | None] = [None] * found
        for (heading, match), count, then in zip(stating, after, [*after[1:], 0][: len(after)], strict=True):
            if count <= then:
                msg = f"the headings {[cell_text(heading)]!r} head no record"
                raise ReadError(msg)
            headed[found - count : found - then] = [dict(match.groupdict(default=None))] * (count - then)
        return headed

    @typing.override
    def _cells(self, body: bytes) -> pl.DataFrame:
        doc = parse_html(body)
        if doc is None:
            msg = "the body is empty"
            raise EmptyBodyError(msg)
        found = self._records(doc)
        headed = zip(found, self._headed(doc, len(found)), strict=True) if found else ()
        records = [(record, stated) for record, stated in headed if self._is_record(record)]
        if not records and self._rows_required:
            msg = "the page holds none of the file's records"
            raise EmptyBodyError(msg)
        read: list[list[str | None]] = []
        for number, (record, stated) in enumerate(records, 1):
            if stated is None:
                msg = f"record {number} sits under no heading matching {self._stated.pattern!r}"
                raise ReadError(msg)
            values: list[str | None] = []
            for column in self._columns:
                if column.field in self._stated.groupindex and self._heading is not None:
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
