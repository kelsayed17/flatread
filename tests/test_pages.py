"""Tests for :mod:`flatread.pages`, the page decoders' shared parses: HTML leniently, XML strictly.

The page decoders were read with BeautifulSoup, then lxml (2026-08), then Scrapling (2026-10-05). These tests pin the
text-extraction semantics *bit-exactly* against what bs4 produced, because the obvious calls — lxml's
``text_content()``, Scrapling's default ``get_all_text()`` — are NOT equivalent to bs4's ``get_text(strip=True)`` and
would silently alter scraped values.
"""

import codecs
import datetime as dt
import re
import typing
from typing import TYPE_CHECKING, Annotated

import msgspec
import polars as pl
import pytest
from lxml import etree

from flatread.errors import EmptyBodyError, RaggedRowError, UnknownLayoutError
from flatread.fields import Flag, Format, Label, Link
from flatread.layout import LongFile
from flatread.markup import parse_xml
from flatread.pages import (
    Element,
    HtmlFile,
    HtmlRecords,
    anchor,
    cell_text,
    cells,
    href,
    parse_html,
    rows,
    table_frame,
    table_header,
    text,
)

if TYPE_CHECKING:
    from pathlib import Path

# libxml2 2.14 rewrote its HTML tokenizer; lxml's Windows wheels still bundle 2.11, which keeps a CDATA section's text
# and nests one level deeper before its limit. These cases state 2.14's parse.
_LIBXML_BEFORE_2_14 = pytest.mark.xfail(
    etree.LIBXML_VERSION < (2, 14), reason="libxml2 before 2.14 parses this HTML otherwise", strict=True
)


class TestParseHtml:
    @pytest.mark.parametrize(
        ("label", "markup", "expected_text"),
        [
            ("full page", "<html><body><p>x</p></body></html>", "x"),
            ("table fragment", "<table><tr><td>x</td></tr></table>", "x"),
            ("anchor fragment", '<a href="/ptr/uuid/">Doe, Jane</a>', "Doe, Jane"),
            ("bare text", "Doe, Jane", "Doe, Jane"),
            ("bytes input", b"<table><tr><td>x</td></tr></table>", "x"),
        ],
    )
    def test_parses_every_input_shape(self, label: str, markup: str | bytes, expected_text: str) -> None:
        """Every shape parses and keeps its text. Scrapling roots each one at ``<html>`` (lxml rooted a fragment at
        its own tag), so a caller selects with ``css`` and the root tag is the same for all of them.
        """
        el = parse_html(markup)
        assert el is not None, label
        assert el.tag == "html", label
        assert text(el) == expected_text, label

    @pytest.mark.parametrize(
        "markup",
        [
            "",
            "   ",
            "\n\t ",
            # Bodies with markup but no element: the lxml parse returned None for each, and Scrapling builds a
            # Selector with no root for each, which raises on ``.tag`` / ``.css`` — so ``parse_html`` must say None.
            "<!DOCTYPE html>",
            "<!-- x -->",
            "<?xml version='1.0'?>",
            "<![CDATA[x]]>",
            "</td>",
        ],
    )
    def test_empty_body_is_none(self, markup: str) -> None:
        """A body with nothing to parse — blank, or markup that holds no element — is ``None``, never a broken
        ``Selector`` (Scrapling leaves the root unset for the latter, and every later call on it raises).
        """
        assert parse_html(markup) is None

    def test_recovers_from_broken_markup(self) -> None:
        """Unclosed tags and unquoted attributes must not lose the data."""
        el = parse_html("<table><tr><td>AAPL<td>1.23<tr><td>MSFT<td>4.56</table>")
        assert el is not None
        table = el.css("table").first
        assert table is not None
        assert [[cell_text(c) for c in cells(r)] for r in rows(table)] == [
            ["AAPL", "1.23"],
            ["MSFT", "4.56"],
        ]

    def test_a_nul_byte_is_deleted(self) -> None:
        """A KNOWN difference from the lxml engine: Scrapling deletes NUL bytes before parsing, where lxml
        turned each into U+FFFD. No scraped page has carried one; pinned so the change is a decision, not a surprise.
        """
        for markup in ("<td>a\x00b</td>", b"<td>a\x00b</td>"):
            el = parse_html(markup)
            assert el is not None
            assert cell_text(el) == "ab"


class TestCellText:
    """``cell_text`` is bit-exact with bs4's ``get_text(strip=True)``.

    Every expected value here was captured from BeautifulSoup before the
    port, so a regression shows up as a diff against real bs4 output.
    """

    @pytest.mark.parametrize(
        ("markup", "expected"),
        [
            ("<td>AAPL</td>", "AAPL"),
            ("<td>  AAPL  </td>", "AAPL"),
            # Inline children: runs are concatenated with NO separator.
            ("<td>A <b>B</b></td>", "AB"),
            ("<td>A <b>B</b> C</td>", "ABC"),
            ("<td><span>1,234</span><span>.56</span></td>", "1,234.56"),
            ("<td>line1<br/>line2</td>", "line1line2"),
            ("<td>x<!-- comment -->y</td>", "xy"),
            ("<td></td>", ""),
            ("<td>   </td>", ""),
            ("<td>\n  Hon. Mark\n  Alford\n</td>", "Hon. Mark\n  Alford"),
            ("<td><a href='#'> Doe , Jane </a></td>", "Doe , Jane"),
            ("<td>a<div>b</div>c</td>", "abc"),
            # Adversarial extras: script text is cell text, &nbsp; stays U+00A0,
            # a blank run between elements is dropped.
            ("<td>x<script>var q=1</script>y</td>", "xvar q=1y"),
            ("<td>A&nbsp;B</td>", "A\xa0B"),
            ("<td><b>A</b> <b>B</b></td>", "AB"),
            # Comments and CDATA are dropped but split no run: Scrapling's default parse removes them first, which
            # merges the text around them into one run ("12 34"); ``keep_comments=True`` keeps bs4's "1234".
            ("<td>12<!-- c --> 34</td>", "1234"),
            pytest.param("<td>12<![CDATA[ ]]> 34</td>", "1234", marks=_LIBXML_BEFORE_2_14),
        ],
    )
    def test_matches_bs4_get_text_strip(self, markup: str, expected: str) -> None:
        el = parse_html(markup)
        assert el is not None
        assert cell_text(el) == expected

    def test_is_not_scraplings_default_get_all_text(self) -> None:
        r"""Guard the trap: Scrapling's default ``get_all_text`` skips ``<script>``/``<style>`` and joins with ``\\n``.

        Using it would turn ``x<script>var q=1</script>y`` into ``"x\\ny"`` where bs4 (and therefore every stored
        scrape) yields ``"xvar q=1y"``.
        """
        el = parse_html("<td>x<script>var q=1</script>y</td>")
        assert el is not None
        assert str(el.get_all_text(strip=True)) == "x\ny"  # the WRONG answer
        assert cell_text(el) == "xvar q=1y"  # the bs4-equivalent one


class TestText:
    """``text`` is equivalent to bs4's ``get_text().strip()`` — no per-run strip."""

    @pytest.mark.parametrize(
        ("markup", "expected"),
        [
            ("<td>Doe, Jane</td>", "Doe, Jane"),
            ("<td> <a>Doe</a>, <b>Jane</b> </td>", "Doe, Jane"),
            ("<td>x<!--c-->y</td>", "xy"),
            ('<a href="/ptr/uuid/">  Doe, Jane  </a>', "Doe, Jane"),
            ("<td><a>Doe,</a> <b>Jane</b></td>", "Doe, Jane"),
            ("<td><a>Hon.</a> <a>Jane</a> <a>Doe</a></td>", "Hon. Jane Doe"),
            ("<td>x<style>.a{}</style>y</td>", "x.a{}y"),
        ],
    )
    def test_matches_bs4_get_text_then_strip(self, markup: str, expected: str) -> None:
        el = parse_html(markup)
        assert el is not None
        assert text(el) == expected

    @pytest.mark.parametrize(
        ("markup", "expected"),
        [
            ("<p>A<br> <b>B</b></p>", "AB"),
            ("<p>A<sup>1</sup> <b>B</b></p>", "A1B"),
            ("<p>A<sub>1</sub> <b>B</b></p>", "A1B"),
            ("<p>A<wbr> <b>B</b></p>", "AB"),
            # The control: a whitespace-only run after an inline element such as <b> survives parsing.
            ("<p>A<b>x</b> <b>B</b></p>", "Ax B"),
        ],
    )
    def test_a_blank_run_after_some_elements_is_deleted_at_parse_time(self, markup: str, expected: str) -> None:
        """A KNOWN difference from the lxml engine: Scrapling's parser always passes
        ``remove_blank_text=True`` (no ``Selector`` keyword turns it off), so libxml2 deletes a whitespace-only run
        after some elements (``<br>``, ``<sup>``, ``<sub>``, ``<wbr>``) at parse time, before ``text`` sees it. lxml
        gave ``"A B"`` for the first case. Only ``text`` can observe it (``cell_text`` drops whitespace-only runs
        anyway); pinned so the change is a decision, not a surprise.
        """
        el = parse_html(markup)
        assert el is not None
        assert text(el) == expected


class TestCells:
    def test_mixed_th_td_in_document_order(self) -> None:
        """``cells`` must preserve document order — header/row zips depend on it."""
        el = parse_html("<tr><th>A</th><td>B</td><th>C</th><td>D</td></tr>")
        assert el is not None
        assert [cell_text(c) for c in cells(el)] == ["A", "B", "C", "D"]

    def test_is_recursive_like_bs4_find_all(self) -> None:
        """bs4's ``find_all(['td','th'])`` is recursive; a nested table's cells
        are included. The port must not silently switch to direct children.
        """
        el = parse_html("<tr><td>outer<table><tr><td>inner</td></tr></table></td></tr>")
        assert el is not None
        assert [cell_text(c) for c in cells(el)] == ["outerinner", "inner"]


class TestCellTextTailHandling:
    """A cell's own tail text must never leak into its value.

    ``<td>a</td>  TAILTEXT  <td>…`` — the text after a cell's closing tag belongs to its parent row, not to the cell.
    bs4's ``get_text(strip=True)`` gave exactly ``["a", "bc"]`` for this markup.
    """

    _MARKUP = "<tr><td>a</td>  TAILTEXT  <td>b<i>c</i></td> MORE </tr>"

    def test_tail_excluded(self) -> None:
        el = parse_html(self._MARKUP)
        assert el is not None
        assert [cell_text(t) for t in cells(el)] == ["a", "bc"]

    def test_plain_and_wrapped_cells_agree(self) -> None:
        """A childless cell and the same content wrapped in a span read the same."""
        plain = parse_html("<td>  1,234.5  </td>")
        wrapped = parse_html("<td><span>  1,234.5  </span></td>")
        assert plain is not None
        assert wrapped is not None
        assert cell_text(plain) == cell_text(wrapped) == "1,234.5"


class TestTableFrame:
    """``table_frame`` — the String frame of a table's rows, under its header, that the page decoders share.

    Table *selection* stays with each caller (yfinance takes the first
    ``<table>``; farside matches ``table.etf``),
    so this takes an ELEMENT, not a page.
    """

    @staticmethod
    def _table(markup: str) -> Element:
        doc = parse_html(markup)
        assert doc is not None
        table = doc.css("table").first
        assert table is not None
        return table

    def _frame(self, markup: str) -> tuple[pl.DataFrame, list[int]]:
        table = self._table(markup)
        columns = table_header(table)
        assert columns is not None
        frame, numbers = table_frame(table, columns)
        return frame, [int(number) for number in numbers]

    def test_header_row_becomes_columns_and_later_rows_become_rows(self) -> None:
        frame, numbers = self._frame(
            "<table>"
            "<tr><th>Date</th><th>IBIT</th></tr>"
            "<tr><td>05 Jan 2026</td><td>7.7</td></tr>"
            "<tr><td>06 Jan 2026</td><td>(3.1)</td></tr>"
            "</table>"
        )
        assert frame.schema == pl.Schema({"Date": pl.String, "IBIT": pl.String})
        assert frame.rows() == [("05 Jan 2026", "7.7"), ("06 Jan 2026", "(3.1)")]
        assert numbers == [1, 2]

    def test_cells_are_verbatim(self) -> None:
        # The decoders keep cell strings raw — a provider's decoder owns every
        # coercion, so the missing token and accounting parens must survive.
        frame, _ = self._frame("<table><tr><th>A</th></tr><tr><td>-</td></tr></table>")
        assert frame.rows() == [("-",)]

    def test_a_row_with_no_cells_is_no_row_and_keeps_the_others_numbers(self) -> None:
        frame, numbers = self._frame("<table><tr><th>A</th></tr><tr></tr><tr><td>1</td></tr></table>")
        assert (frame.rows(), numbers) == ([("1",)], [2])

    def test_header_only_table_yields_no_rows(self) -> None:
        frame, numbers = self._frame("<table><tr><th>A</th></tr></table>")
        assert (frame.columns, frame.height, numbers) == (["A"], 0, [])

    def test_unclosed_cells_do_not_nest(self) -> None:
        # Farside hand-writes unclosed <td>/<tr>. lxml closes them implicitly;
        # bs4's html.parser did not, which made every cell slurp the rest of
        # the table. Pinned here too, because this
        # helper is the shared row walk.
        frame, _ = self._frame("<table><tr><th>Date<th>IBIT<tr><td>05 Jan 2026<td>7.7</table>")
        assert frame.columns == ["Date", "IBIT"]
        assert frame.rows() == [("05 Jan 2026", "7.7")]

    def test_a_row_longer_than_the_header_raises(self) -> None:
        # Until 2026-10-05 zip(strict=False) dropped the third cell without a word.
        with pytest.raises(
            RaggedRowError, match=r"^row 1 of the table under header \['A', 'B'\] has 3 cells: \['1', '2', '3'\]$"
        ):
            self._frame("<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td><td>3</td></tr></table>")

    def test_a_row_shorter_than_the_header_raises(self) -> None:
        # Until 2026-11-06 a short row was each caller's to refuse, and Farside raised another error than the rest.
        with pytest.raises(
            RaggedRowError, match=r"^row 1 of the table under header \['A', 'B'\] has 1 cells: \['1'\]$"
        ):
            self._frame("<table><tr><th>A</th><th>B</th></tr><tr><td>1</td></tr></table>")

    def test_a_later_ragged_row_is_reported_by_its_place(self) -> None:
        with pytest.raises(RaggedRowError, match=r"^row 3 of the table under header \['A'\] has 2 cells"):
            self._frame("<table><tr><th>A</th></tr><tr><td>1</td></tr><tr></tr><tr><td>2</td><td>x</td></tr></table>")

    def test_a_repeated_column_name_raises(self) -> None:
        # Until 2026-10-05 the second "A" cell overwrote the first in the row dict.
        with pytest.raises(
            UnknownLayoutError, match=r"^the table's header \['A', 'A', 'B'\] names \['A'\] more than once$"
        ):
            self._frame("<table><tr><th>A</th><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td><td>3</td></tr></table>")

    def test_a_repeated_column_name_raises_with_no_data_rows(self) -> None:
        with pytest.raises(UnknownLayoutError, match=r"names \['A'\] more than once$"):
            self._frame("<table><tr><th>A</th><th>A</th></tr></table>")

    def test_a_table_inside_a_cell_raises(self) -> None:
        """Until 2026-11-06 the inner table's rows read as the outer one's."""
        with pytest.raises(UnknownLayoutError, match=r"^the table under header \['A', 'B'\] holds another table$"):
            self._frame(
                "<table><tr><th>A</th><th>B</th></tr>"
                "<tr><td>1</td><td><table><tr><td>2</td><td>3</td></tr></table></td></tr></table>"
            )

    def test_header_reads_a_repeated_name_without_raising(self) -> None:
        # Reading never judges: the Senate eFD decoder must be able to look at a header and walk away from the table.
        assert table_header(self._table("<table><tr><th>A</th><th>A</th></tr></table>")) == ["A", "A"]

    def test_header_reads_a_table_holding_another_without_raising(self) -> None:
        table = self._table("<table><tr><th>A</th></tr><tr><td><table><tr><td>x</td></tr></table></td></tr></table>")
        assert table_header(table) == ["A"]
        with pytest.raises(UnknownLayoutError, match=r"holds another table$"):
            rows(table)

    def test_header_of_a_table_without_rows_is_none(self) -> None:
        assert table_header(self._table("<table></table>")) is None


class TestBytesInputHonorsTheDeclaredCharset:
    """Why the page decoders read BYTES rather than ``Response.text``.

    ``Response.text`` decodes using the charset from the **HTTP header**, falling back to UTF-8. A page that declares
    its encoding only in a ``<meta>`` tag therefore mojibakes silently — httpx never looks at the markup. lxml does,
    so passing it the raw bytes is strictly more faithful.

    Measured 2026-08-11: every page read sends ``charset=utf-8`` on the header, so this is **not** a
    live bug — it is the failure mode reading bytes makes unreachable, and this test is what keeps it unreachable
    if someone "simplifies" a decoder back to reading ``str``.
    """

    _MARKUP = '<html><head><meta charset="windows-1252"></head><body><td>Café — 90%</td></body></html>'

    def test_bytes_input_uses_the_meta_charset(self) -> None:
        doc = parse_html(self._MARKUP.encode("cp1252"))
        assert doc is not None
        assert cell_text(cells(doc)[0]) == "Café — 90%"

    def test_pre_decoded_text_is_what_would_have_been_corrupted(self) -> None:
        """The negative control — what ``Response.text`` would have produced for the same page.

        Not a complaint about httpx: with no charset on the header its documented fallback is UTF-8, and decoding
        cp1252 bytes as UTF-8 with ``errors="replace"`` is lossy by construction. The point is that the loss happens
        BEFORE the parser sees the markup, so no amount of care downstream can recover it.
        """
        as_text = self._MARKUP.encode("cp1252").decode("utf-8", errors="replace")
        doc = parse_html(as_text)
        assert doc is not None
        assert cell_text(cells(doc)[0]) != "Café — 90%"
        assert "�" in cell_text(cells(doc)[0])


class TestBytesThatDeclareNoCharsetAreUtf8:
    """A ``bytes`` page with no BOM and no ``<meta>`` charset in its head is UTF-8, read strictly.

    Until 2026-11-06 libxml2 fell back to Latin-1 for one, so ``café`` read as ``cafÃ©``, with no error.
    """

    _CELL = "café — x"

    @pytest.mark.parametrize(
        "markup",
        [
            f"<table><tr><td>{_CELL}</td></tr></table>",
            f"<html><head><title>t</title></head><body><td>{_CELL}</td></body></html>",
        ],
        ids=["fragment", "page"],
    )
    def test_undeclared_utf8_reads_as_written(self, markup: str) -> None:
        doc = parse_html(markup.encode())
        assert doc is not None
        assert cell_text(cells(doc)[0]) == self._CELL

    def test_undeclared_bytes_that_are_not_utf8_raise(self) -> None:
        with pytest.raises(UnknownLayoutError, match=r"^the page declares no charset and byte 7 is not UTF-8$"):
            parse_html(f"<td>{self._CELL}</td>".encode("cp1252"))

    def test_a_charset_declared_past_the_first_1024_bytes_is_honored(self) -> None:
        """libxml2 reads a ``<meta charset>`` after a long ``<style>``, past the WHATWG prescan's window."""
        head = f"<head><title>t</title><style>/*{'x' * 2000}*/</style><meta charset='windows-1252'></head>"
        doc = parse_html(f"<html>{head}<body><td>{self._CELL}</td></body></html>".encode("cp1252"))
        assert doc is not None
        assert cell_text(cells(doc)[0]) == self._CELL

    def test_a_charset_declared_in_http_equiv_form_is_honored(self) -> None:
        head = '<meta http-equiv="Content-Type" content="text/html; charset=windows-1252">'
        doc = parse_html(f"{head}<td>{self._CELL}</td>".encode("cp1252"))
        assert doc is not None
        assert cell_text(cells(doc)[0]) == self._CELL

    @pytest.mark.parametrize(
        "body",
        [
            "<html><head><title>t</title></head><body><p>set charset=windows-1252 in the header</p><td>{}</td></body>",
            "<td>{}</td><p>an article on charset= headers</p>",
        ],
        ids=["in the body", "in text of a page with no head"],
    )
    def test_text_that_mentions_charset_declares_nothing(self, body: str) -> None:
        """Until the review of #216 any ``charset=`` in the bytes turned the UTF-8 default off, so an article saying it
        let libxml2 read UTF-8 text as Latin-1 again.
        """
        doc = parse_html(body.format(self._CELL).encode())
        assert doc is not None
        assert cell_text(cells(doc)[0]) == self._CELL

    def test_a_meta_tag_in_the_body_declares_nothing(self) -> None:
        markup = f"<html><head></head><body><meta charset='windows-1252'><td>{self._CELL}</td></body></html>"
        with pytest.raises(UnknownLayoutError, match=r"^the page declares no charset and byte \d+ is not UTF-8$"):
            parse_html(markup.encode("cp1252"))

    def test_a_byte_order_mark_is_a_declaration(self) -> None:
        doc = parse_html(codecs.BOM_UTF8 + f"<td>{self._CELL}</td>".encode())
        assert doc is not None
        assert cell_text(cells(doc)[0]) == self._CELL


class TestDepthLimit:
    """libxml2's 256-level nesting limit stays on (``huge_tree`` off), as it has always been here.

    Measured 2026-10-05: of 300 nested ``<div>``s both the lxml parse and Scrapling with ``huge_tree=False`` keep 254
    (256 less ``<html>`` and ``<body>``), and the innermost text is cut; with ``huge_tree=True`` all 300 survive. The
    200-deep control shows the limit, not a broken parse, is what removes the text.
    """

    @_LIBXML_BEFORE_2_14
    def test_text_past_the_limit_is_cut(self) -> None:
        doc = parse_html("<html><body>" + "<div>" * 300 + "deep" + "</div>" * 300 + "</body></html>")
        assert doc is not None
        assert len(doc.css("div")) == 254  # 256 levels less <html> and <body>
        assert "deep" not in text(doc)

    def test_text_within_the_limit_is_kept(self) -> None:
        doc = parse_html("<html><body>" + "<div>" * 200 + "deep" + "</div>" * 200 + "</body></html>")
        assert doc is not None
        assert text(doc) == "deep"


class TestParseXml:
    """The strict, hardened XML parse every XML decoder shares."""

    def test_a_document_reads_to_its_root(self) -> None:
        root = parse_xml(b'<?xml version="1.0" encoding="utf-8"?><a xmlns="urn:x"><b>1</b></a>')
        assert root.tag == "{urn:x}a"
        assert [child.text for child in root] == ["1"]

    @pytest.mark.parametrize("body", [b"", b" \r\n\t"])
    def test_an_empty_body_is_an_empty_body(self, body: bytes) -> None:
        with pytest.raises(EmptyBodyError, match=r"^the body is empty$"):
            parse_xml(body)

    @pytest.mark.parametrize(
        "body",
        [b"not xml", b"<a><b>1</b>", b"<a></b>", b'{"a": 1}', b"<a/><b/>"],
        ids=["text", "cut off", "mismatched", "json", "two roots"],
    )
    def test_a_body_that_is_not_one_well_formed_document_raises(self, body: bytes) -> None:
        """No recovery: a cut-off body raises rather than reading as the elements before the cut."""
        with pytest.raises(UnknownLayoutError, match=r"^the body is not well-formed XML: "):
            parse_xml(body)

    def test_an_internal_entity_is_not_expanded(self) -> None:
        """The billion-laughs guard: lxml would expand a declared entity into the text by default."""
        root = parse_xml(b'<!DOCTYPE a [ <!ENTITY lol "EXPANDED"> ]><a>&lol;</a>')
        assert "EXPANDED" not in etree.tostring(root).decode()

    def test_an_external_entity_is_not_fetched(self, tmp_path: Path) -> None:
        secret = tmp_path / "secret.txt"
        secret.write_text("SECRET")
        body = f'<!DOCTYPE a [ <!ENTITY x SYSTEM "{secret.as_uri()}"> ]><a>&x;</a>'.encode()
        assert "SECRET" not in etree.tostring(parse_xml(body)).decode()


class _Holding(msgspec.Struct, frozen=True, gc=False):
    number: Annotated[int, Label("#")]
    day: Annotated[dt.date, Label("Date"), Format("%m/%d/%Y")]
    name: Annotated[str, Label("Asset Name")]
    rate: Annotated[float | None, Label("Rate")]
    held: Annotated[bool, Label("Held"), Flag("Y", "N")]


_HOLDINGS = HtmlFile(_Holding)
_HEADER = ("#", "Date", "Asset Name", "Rate", "Held")
_ROW = ("1", "04/24/2025", "Acme", "4.5", "Y")


def _page(*rows: tuple[str, ...], header: tuple[str, ...] = _HEADER, tables: int = 1) -> bytes:
    def tr(cells: tuple[str, ...], tag: str) -> str:
        return "<tr>" + "".join(f"<{tag}>{cell}</{tag}>" for cell in cells) + "</tr>"

    table = f"<table><thead>{tr(header, 'th')}</thead><tbody>{''.join(tr(row, 'td') for row in rows)}</tbody></table>"
    return f"<html><body><p>Holdings</p>{table * tables}</body></html>".encode()


class TestAnHtmlFile:
    """A clean HTML table read natively: its header the struct's labels, each cell's text typed by its field."""

    def test_a_clean_table_reads_typed(self) -> None:
        body = _page(
            _ROW,
            (" 2 ", "\n 04/25/2025 ", "A <b>Muni</b>\n<div>Bond</div>", "  ", "N"),
        )
        assert _HOLDINGS.rows(body) == [
            _Holding(1, dt.date(2025, 4, 24), "Acme", 4.5, held=True),
            _Holding(2, dt.date(2025, 4, 25), "AMuniBond", None, held=False),
        ]
        assert _HOLDINGS.frame(body).schema == pl.Schema(
            {"number": pl.Int64, "day": pl.Date, "name": pl.String, "rate": pl.Float64, "held": pl.Boolean}
        )

    def test_a_row_without_cells_is_no_row(self) -> None:
        body = _page(_ROW).replace(b"<tbody>", b"<tbody><tr></tr>")
        assert len(_HOLDINGS.rows(body)) == 1

    @pytest.mark.parametrize(
        ("cell", "message"),
        [
            ({0: "1.5"}, r"^the '#' cell '1.5' is not a whole number: row 2$"),
            ({1: "4/24/2025"}, r"^the 'Date' cell '4/24/2025' is not a date written %m/%d/%Y: row 2$"),
            ({3: "high"}, r"^the 'Rate' cell 'high' is not a number: row 2$"),
            ({4: "yes"}, r"^the 'Held' cell 'yes' is not 'Y' or 'N': row 2$"),
            ({2: " "}, r"^the \['name'\] columns have an empty cell, a short row or no column, .*: row 2$"),
        ],
        ids=["fraction", "unpadded date", "text in float", "another flag", "a blank required cell"],
    )
    def test_a_cell_not_its_fields_type_raises_naming_its_row(self, cell: dict[int, str], message: str) -> None:
        wrong = tuple(cell.get(i, value) for i, value in enumerate(_ROW))
        with pytest.raises(ValueError, match=message):
            _HOLDINGS.rows(_page(_ROW, wrong))

    @pytest.mark.parametrize(
        ("body", "error", "message"),
        [
            (b" \n", ValueError, r"^the body is empty$"),
            (b"<p>no table</p>", ValueError, r"^the page holds 0 of the file's tables, not one$"),
            (_page(_ROW, tables=2), ValueError, r"^the page holds 2 of the file's tables, not one$"),
            (b"<table></table>", ValueError, r"^the table has no row$"),
            (_page(header=("#", "Day")), ValueError, r"^the header \['#', 'Day'\] is not \['#', 'Date', "),
            (_page(), ValueError, r"^the body is a header and no rows$"),
            (_page(_ROW[:4]), RaggedRowError, r"row 1 of the table under header .* has 4 cells"),
            (_page(_ROW, header=("#", "#", "Asset Name", "Rate", "Held")), UnknownLayoutError, r"names \['#'\] more"),
            (
                _page(("<table><tr><td>1</td></tr></table>", *_ROW[1:])),
                ValueError,
                r"^the page holds 2 of the file's tables, not one$",
            ),
        ],
        ids=["empty", "no table", "two tables", "no row", "another header", "no rows", "ragged", "repeated", "nested"],
    )
    def test_a_page_of_another_shape_raises(self, body: bytes, error: type[Exception], message: str) -> None:
        with pytest.raises(error, match=message):
            _HOLDINGS.rows(body)

    def test_a_format_narrows_its_selector(self) -> None:
        class Etf(HtmlFile[_Holding]):
            _table = "table.etf"

        body = _page(_ROW, tables=2).replace(b"<table>", b'<table class="etf">', 1)
        assert len(Etf(_Holding).rows(body)) == 1

    def test_a_format_that_allows_no_rows_reads_no_table_as_none(self) -> None:
        class Optional(HtmlFile[_Holding]):
            _rows_required = False

        frame = Optional(_Holding).frame(b"<p>none</p>")
        assert frame.is_empty()
        assert frame.columns == list(_Holding.__struct_fields__)
        assert Optional(_Holding).rows(_page()) == []

    def test_a_wide_table_reads_long(self) -> None:
        class Rate(msgspec.Struct, frozen=True, gc=False):
            day: Annotated[dt.date, Label("Date"), Format("%m/%d/%Y")]
            fund: str
            rate: float | None

        class Rates(LongFile[Rate], HtmlFile[Rate]):
            _keys = re.compile(r"[A-Z]+")

        body = _page(("04/24/2025", "4.5", ""), header=("Date", "IBIT", "GBTC"))
        assert Rates(Rate).rows(body) == [
            Rate(dt.date(2025, 4, 24), "IBIT", 4.5),
            Rate(dt.date(2025, 4, 24), "GBTC", None),
        ]
        with pytest.raises(ValueError, match=r"^the header's columns \['ibit'\] are not keys"):
            Rates(Rate).rows(_page(("04/24/2025", "4.5"), header=("Date", "ibit")))


class _File(msgspec.Struct, frozen=True, gc=False):
    title: Annotated[str, Label("File")]
    path: Annotated[str, Label("File"), Link()]
    size: Annotated[int, Label("Size")]


class TestAnHtmlFilesLinks:
    """A ``Link`` field reads its cell's one link's ``href``; fields sharing a cell each key their own column."""

    _FILES = HtmlFile(_File)

    def _page(self, *cells: str) -> bytes:
        rows = "".join(f"<tr><td>{cell}</td><td>7</td></tr>" for cell in cells)
        return f"<table><tr><th>File</th><th>Size</th></tr>{rows}</table>".encode()

    def test_a_cell_reads_its_text_and_its_link(self) -> None:
        body = self._page('<a href="/a.zip">A <b>file</b></a>')
        assert self._FILES.rows(body) == [_File("Afile", "/a.zip", 7)]
        assert self._FILES.frame(body).columns == ["title", "path", "size"]

    @pytest.mark.parametrize(
        ("cell", "message"),
        [
            ("A", r"^the \['path'\] columns have an empty cell"),
            ('<a href="/a">A</a><a href="/b">B</a>', r"^row 1: its 'File' cell holds 2 links, not one: 'AB'$"),
            ("<a>A</a>", r"^the \['path'\] columns have an empty cell"),
        ],
        ids=["no link", "two links", "no href"],
    )
    def test_a_cell_without_one_link_raises(self, cell: str, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            self._FILES.rows(self._page(cell))

    def test_a_link_is_its_own_anchor(self) -> None:
        doc = parse_html(b'<p><a href="/x">X</a></p>')
        assert doc is not None
        (link,) = doc.css("a")
        assert anchor(link) is link
        assert href(link) == "/x"
        assert type(href(link)) is str


class _Meeting(msgspec.Struct, frozen=True, gc=False):
    year: int
    month: Annotated[str, Label(".month")]
    link: Annotated[str | None, Label(""), Link()]


class _Meetings(HtmlRecords[_Meeting]):
    _record = "div.meeting"
    _heading = "h4"
    _stated = re.compile(r"(?P<year>\d{4}) meetings")


def _meetings(*parts: str) -> bytes:
    return "".join(parts).encode()


def _meeting(month: str, link: str = "") -> str:
    return (
        f'<a class="meeting" href="{link}"><div class="month">{month}</div></a>'
        if link
        else (f'<div class="meeting"><div class="month">{month}</div></div>')
    )


class TestHtmlRecords:
    """Records that are elements: each field the one element its selector finds, a heading's groups stated over them."""

    def test_records_read_under_their_headings(self) -> None:
        body = _meetings(
            "<h4>2026 meetings</h4>",
            _meeting("January"),
            "<section><h4>2025 meetings</h4>",
            _meeting("March"),
            _meeting("May"),
            "</section>",
        )
        assert _Meetings(_Meeting).rows(body) == [
            _Meeting(2026, "January", None),
            _Meeting(2025, "March", None),
            _Meeting(2025, "May", None),
        ]

    def test_a_record_reads_its_own_link(self) -> None:
        class Linked(HtmlRecords[_Meeting]):
            _record = "a.meeting"
            _heading = "h4"
            _stated = re.compile(r"(?P<year>\d{4}) meetings")

        body = _meetings("<h4>2026 meetings</h4>", _meeting("June", "/june.htm"))
        assert Linked(_Meeting).rows(body) == [_Meeting(2026, "June", "/june.htm")]

    def test_records_without_headings(self) -> None:
        class Month(msgspec.Struct, frozen=True, gc=False):
            name: Annotated[str, Label(".month")]

        class Months(HtmlRecords[Month]):
            _record = "div.meeting"

        assert Months(Month).rows(_meetings(_meeting("July"), _meeting("August"))) == [Month("July"), Month("August")]

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            (b" ", r"^the body is empty$"),
            (b"<h4>2026 meetings</h4>", r"^the page holds none of the file's records$"),
            (_meetings(_meeting("May")), r"^record 1 sits under no heading matching '\(\?P<year>"),
            (_meetings("<h4>Search</h4>", _meeting("May")), r"^record 1 sits under no heading matching"),
            (
                _meetings("<h4>2026 meetings</h4>", _meeting("May"), "<h4>2025 meetings</h4>"),
                r"^the headings \['2025 meetings'\] head no record$",
            ),
            (
                _meetings("<h4>2026 meetings</h4>", '<div class="meeting"></div>'),
                r"^record 1 holds 0 elements '\.month' finds, not one$",
            ),
            (
                _meetings("<h4>2026 meetings</h4>", '<div class="meeting"><a href="/a">a</a><a href="/b">b</a></div>'),
                r"^record 1 holds 0 elements '\.month' finds, not one$",
            ),
        ],
        ids=["empty", "no record", "no heading", "another heading", "a heading of none", "no cell", "no month"],
    )
    def test_a_page_of_another_shape_raises(self, body: bytes, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            _Meetings(_Meeting).rows(body)

    def test_a_records_link_must_be_one(self) -> None:
        body = _meetings(
            "<h4>2026 meetings</h4>",
            '<div class="meeting"><div class="month">May</div><a href="/a">a</a><a href="/b">b</a></div>',
        )
        with pytest.raises(ValueError, match=r"^record 1: its cell holds 2 links, not one"):
            _Meetings(_Meeting).rows(body)

    def test_a_format_leaves_records_out(self) -> None:
        class NoMay(_Meetings):
            @typing.override
            def _is_record(self, element: Element) -> bool:
                return cell_text(element) != "May"

        body = _meetings("<h4>2026 meetings</h4>", _meeting("May"), _meeting("June"))
        assert NoMay(_Meeting).rows(body) == [_Meeting(2026, "June", None)]

    def test_an_optional_group_states_no_value(self) -> None:
        class Maybe(msgspec.Struct, frozen=True, gc=False):
            year: int | None
            month: Annotated[str, Label(".month")]

        class Panels(HtmlRecords[Maybe]):
            _record = "div.meeting"
            _heading = "h4"
            _stated = re.compile(r"(?:(?P<year>\d{4}) )?meetings")

        body = _meetings("<h4>meetings</h4>", _meeting("May"), "<h4>2026 meetings</h4>", _meeting("June"))
        assert Panels(Maybe).rows(body) == [Maybe(None, "May"), Maybe(2026, "June")]

    def test_an_empty_heading_is_found_beside_another_of_its_text(self) -> None:
        body = _meetings("<h4>2026 meetings</h4>", _meeting("May"), "<h4>2026 meetings</h4>")
        with pytest.raises(ValueError, match=r"^the headings \['2026 meetings'\] head no record$"):
            _Meetings(_Meeting).rows(body)


class _Shares(msgspec.Struct, frozen=True, gc=False):
    long: Annotated[int, Label("LONG")]
    long_pct: Annotated[float, Label("%")]
    short: Annotated[int, Label("SHORT")]
    short_pct: Annotated[float, Label("%")]


class TestAnHtmlFilesRepeatedLabels:
    """A label the header writes twice is two fields, matched by place, as every reader of ``_flat`` matches them."""

    def test_each_place_is_its_own_field(self) -> None:
        head = b"<tr><th>LONG</th><th>%</th><th>SHORT</th><th>%</th></tr>"
        body = b"<table>" + head + b"<tr><td>4</td><td>0.5</td><td>2</td><td>0.25</td></tr></table>"
        assert HtmlFile(_Shares).rows(body) == [_Shares(4, 0.5, 2, 0.25)]

    def test_a_label_written_more_often_than_its_fields_raises(self) -> None:
        body = b"<table><tr><th>LONG</th><th>%</th><th>SHORT</th><th>%</th><th>%</th></tr></table>"
        with pytest.raises(UnknownLayoutError, match=r"names \['%'\] more than once$"):
            HtmlFile(_Shares).rows(body)

    def test_an_optional_field_with_no_element_holds_no_value(self) -> None:
        class Month(msgspec.Struct, frozen=True, gc=False):
            name: Annotated[str, Label(".month")]
            pdf: Annotated[str | None, Label('a[href$=".pdf" i]'), Link()]

        class Months(HtmlRecords[Month]):
            _record = "div.meeting"

        body = _meetings(
            _meeting("July"), '<div class="meeting"><div class="month">May</div><a href="/m.PDF">p</a></div>'
        )
        assert Months(Month).rows(body) == [Month("July", None), Month("May", "/m.PDF")]
