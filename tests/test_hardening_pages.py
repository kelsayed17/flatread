"""The HTML and PDF readers against bodies built to misread or to exhaust them: each the adversary's repro, refused."""

import codecs
import datetime as dt
import re
import time
import typing
import zlib
from typing import Annotated

import msgspec
import pytest
from pdfminer import pdftypes

import flatread.pdf
from flatread.errors import RaggedRowError, ReadError
from flatread.fields import Flag, Format, Label
from flatread.layout import LongFile
from flatread.pages import Element, HtmlFile, HtmlRecords, cell_text, parse_html, table_frame, table_header
from flatread.pdf import text_layer
from tests.support.pdf import document, pdf

_FONT = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
_TEXT = b"BT /F1 11 Tf 72 720 Td (a) Tj ET"


class _Quote(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("Name")]
    rate: Annotated[float | None, Label("Rate")]


_QUOTES = HtmlFile(_Quote)


def _table(*rows: str, head: str = "<tr><th>Name</th><th>Rate</th></tr>") -> bytes:
    return f"<table>{head}{''.join(rows)}</table>".encode()


class TestATypedCellIsOneTextRun:
    """Until 2026-11-14 a cell's runs were joined: ``5.3<sup>1</sup>`` read 5.31 and ``1<br>25`` 125, with no error."""

    @pytest.mark.parametrize(
        ("cell", "runs"),
        [
            ("5.3<sup>1</sup>", ["5.3", "1"]),
            ("1<br>25", ["1", "25"]),
            ('<span style="display:none">9</span>5', ["9", "5"]),
            ("12<!-- c -->34", ["12", "34"]),
        ],
        ids=["footnote", "line break", "hidden span", "comment"],
    )
    def test_a_number_cell_of_two_runs_raises(self, cell: str, runs: list[str]) -> None:
        with pytest.raises(ReadError, match=rf"^row 1: its 'Rate' cell holds the text runs {re.escape(repr(runs))}"):
            _QUOTES.rows(_table(f"<tr><td>A</td><td>{cell}</td></tr>"))

    def test_a_date_or_a_flag_of_two_runs_raises(self) -> None:
        class Day(msgspec.Struct, frozen=True, gc=False):
            day: Annotated[dt.date, Label("Day"), Format("%m/%d/%Y")]
            held: Annotated[bool, Label("Held"), Flag("Y", "N")]

        head = "<tr><th>Day</th><th>Held</th></tr>"
        with pytest.raises(ReadError, match=r"^row 1: its 'Day' cell holds the text runs \['01/02', '/2026'\]"):
            HtmlFile(Day).rows(_table("<tr><td>01/02<br>/2026</td><td>Y</td></tr>", head=head))
        with pytest.raises(ReadError, match=r"^row 1: its 'Held' cell holds the text runs \['Y', '\*'\]"):
            HtmlFile(Day).rows(_table("<tr><td>01/02/2026</td><td>Y<sup>*</sup></td></tr>", head=head))

    def test_one_run_in_markup_and_a_text_cell_of_many_read(self) -> None:
        body = _table("<tr><td>A <b>Muni</b></td><td> <b>4.5</b> </td></tr>")
        assert _QUOTES.rows(body) == [_Quote("AMuni", 4.5)]

    def test_a_format_reads_a_cell_its_own_way(self) -> None:
        class Footnoted(HtmlFile[_Quote]):
            @typing.override
            def _value(self, cell: Element, column: typing.Any) -> str:
                sup = cell.css("sup").first
                return cell_text(cell).removesuffix(cell_text(sup)) if sup is not None else cell_text(cell)

        assert Footnoted(_Quote).rows(_table("<tr><td>A</td><td>5.3<sup>1</sup></td></tr>")) == [_Quote("A", 5.3)]

    def test_a_records_number_of_two_runs_raises(self) -> None:
        class Count(msgspec.Struct, frozen=True, gc=False):
            n: Annotated[int, Label(".n")]

        class Counts(HtmlRecords[Count]):
            _record_selector = "div.c"

        with pytest.raises(ReadError, match=r"^record 1: its '\.n' cell holds the text runs \['1', '2'\], not one$"):
            Counts(Count).rows(b'<div class="c"><p class="n">1<sup>2</sup></p></div>')


class TestACutPage:
    """Until 2026-11-14 a page cut inside a number read the number's first digits: ``<td>52`` cut from ``5200``."""

    def test_a_table_with_no_end_tag_raises(self) -> None:
        body = _table("<tr><td>a</td><td>4</td></tr>")[: -len(b"</table>")] + b"<tr><td>b</td><td>52"
        with pytest.raises(ReadError, match=r"^the page was cut: a <table> has no </table>$"):
            _QUOTES.rows(body)

    def test_a_stray_end_tag_closes_no_later_table(self) -> None:
        body = b"</table>" + _table("<tr><td>a</td><td>4</td></tr>")[: -len(b"</table>")]
        with pytest.raises(ReadError, match=r"^the page was cut"):
            _QUOTES.rows(body)

    def test_a_table_in_a_comment_and_an_end_tag_in_any_case_are_read_as_written(self) -> None:
        body = b"<!-- <table> -->" + _table("<tr><td>a</td><td>4</td></tr>").replace(b"</table>", b"</TABLE >")
        assert _QUOTES.rows(body) == [_Quote("a", 4.0)]


class TestADeclaredCharset:
    """Until 2026-11-14 a byte that is not the declared charset read as U+FFFD: ``café`` as ``caf�``."""

    def test_a_byte_that_is_not_the_declared_charset_raises(self) -> None:
        body = b'<html><head><meta charset="utf-8"></head><body><td>caf\xe9</td></body></html>'
        with pytest.raises(ReadError, match=r"^the page declares the charset 'utf-8' and byte 54 is not utf-8$"):
            parse_html(body)

    def test_the_http_equiv_form_and_a_byte_order_mark_are_checked_too(self) -> None:
        head = b'<meta http-equiv="Content-Type" content="text/html; charset=shift_jis">'
        with pytest.raises(ReadError, match=r"^the page declares the charset 'shift_jis' and byte \d+ is not"):
            parse_html(head + b"<td>\x81</td>")
        with pytest.raises(ReadError, match=r"^the page declares the charset 'utf-8-sig' and byte 7 is not"):
            parse_html(codecs.BOM_UTF8 + b"<td>caf\xe9</td>")

    @pytest.mark.parametrize("charset", ["klingon", "base64", ""])
    def test_a_charset_python_does_not_read_raises(self, charset: str) -> None:
        body = f'<head><meta charset="{charset}"></head><td>x</td>'.encode()
        with pytest.raises(ReadError, match=rf"^the page declares the charset '{charset}', which is no charset Python"):
            parse_html(body)

    def test_a_page_in_its_declared_charset_reads(self) -> None:
        doc = parse_html('<head><meta charset="windows-1252"></head><td>café</td>'.encode("cp1252"))
        assert doc is not None
        assert cell_text(doc.css("td")[0]) == "café"

    def test_finding_the_declaration_is_linear(self) -> None:
        # Until 2026-11-14 ``<meta\b[^>]*`` rescanned every later ``<meta``: 240 KB of them took 26 s.
        started = time.perf_counter()
        parse_html(b"<meta " * 40_000 + b"</head><td>x</td>")
        assert time.perf_counter() - started < 2


class TestRowsAndTheirNumbers:
    """Every error names a row as the typing does: among the rows with cells, from 1 under the header."""

    def test_an_empty_tr_numbers_no_row_in_either_error(self) -> None:
        empty = "<tr></tr>"
        with pytest.raises(ReadError, match=r"^the 'Rate' cell 'x' is not a number: row 2$"):
            _QUOTES.rows(_table(empty, "<tr><td>a</td><td>1</td></tr>", "<tr><td>b</td><td>x</td></tr>"))
        with pytest.raises(RaggedRowError, match=r"^row 2 of the table under header"):
            _QUOTES.rows(_table(empty, "<tr><td>a</td><td>1</td></tr>", "<tr><td>b</td></tr>"))
        with pytest.raises(ReadError, match=r"^row 2: its 'Rate' cell holds the text runs"):
            _QUOTES.rows(_table(empty, "<tr><td>a</td><td>1</td></tr>", "<tr><td>b</td><td>1<br>2</td></tr>"))

    def test_a_footers_rows_are_no_rows(self) -> None:
        body = _table(
            "<tbody><tr><td>a</td><td>1</td></tr><tr><td>b</td><td>2</td></tr></tbody>",
            '<tfoot><tr><td colspan="2">Total 3</td></tr></tfoot>',
            head="<thead><tr><th>Name</th><th>Rate</th></tr></thead>",
        )
        assert _QUOTES.rows(body) == [_Quote("a", 1.0), _Quote("b", 2.0)]
        doc = parse_html(body)
        assert doc is not None
        table = doc.css("table")[0]
        header = table_header(table)
        assert header is not None
        assert table_frame(table, header)[0].height == 2


class TestNoQuadraticWalk:
    """Until 2026-11-14 8000 headings took 16 s (an XPath per heading), a 20k-cell header 3.8 s (a count per cell)."""

    def test_headings_are_found_in_one_walk(self) -> None:
        class Rec(msgspec.Struct, frozen=True, gc=False):
            year: int
            a: Annotated[str, Label("")]

        class Recs(HtmlRecords[Rec]):
            _record_selector = "p"
            _heading_selector = "h2"
            _heading_pattern = re.compile(r"(?P<year>\d+)")

        started = time.perf_counter()
        rows = Recs(Rec).rows(b"".join(b"<h2>%d</h2><p>x</p>" % n for n in range(8000)))
        assert time.perf_counter() - started < 2
        assert (rows[0], rows[-1]) == (Rec(0, "x"), Rec(7999, "x"))

    def test_a_record_inside_its_heading_is_the_previous_headings(self) -> None:
        class Rec(msgspec.Struct, frozen=True, gc=False):
            year: int
            a: Annotated[str, Label("")]

        class Recs(HtmlRecords[Rec]):
            _record_selector = "p"
            _heading_selector = "div.h"
            _heading_pattern = re.compile(r"(?P<year>\d+)\w*")

        body = b'<div class="h">1</div><p>x</p><div class="h">2<p>y</p></div><p>z</p>'
        assert Recs(Rec).rows(body) == [Rec(1, "x"), Rec(1, "y"), Rec(2, "z")]

    def test_a_wide_header_is_counted_once(self) -> None:
        class One(msgspec.Struct, frozen=True, gc=False):
            a: Annotated[str, Label("a")]

        n = 20_000
        started = time.perf_counter()
        with pytest.raises(ReadError, match=r"names \['a'\] more than once$"):
            HtmlFile(One).rows(b"<table><tr>" + b"<th>a</th>" * n + b"</tr><tr>" + b"<td>1</td>" * n + b"</tr></table>")
        assert time.perf_counter() - started < 2


def _page(content: bytes, *forms: bytes, flate: bool = False) -> bytes:
    """Return a one-page PDF drawing *content*, with *forms* as objects 6 on, each ``/X`` the next."""
    stream = zlib.compress(content) if flate else content
    filtered = b" /Filter /FlateDecode" if flate else b""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        _FONT,
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> /XObject << /X 6 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Length %d%b >>\nstream\n%b\nendstream" % (len(stream), filtered, stream),
    ]
    for n, form in enumerate(forms, start=6):
        drawn = b" /XObject << /X %d 0 R >>" % (n + 1) if n + 1 < 6 + len(forms) else b""
        objects.append(
            b"<< /Type /XObject /Subtype /Form /BBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >>%b >> "
            b"/Length %d >>\nstream\n%b\nendstream" % (drawn, len(form), form)
        )
    return document(objects)


class TestAPdfsCaps:
    """Until 2026-11-14 a 4 KB chain of forms took 8 s and 353 MB, and a 102 KB Flate stream inflated to 100 MB."""

    def test_forms_nested_past_the_cap_raise(self) -> None:
        body = _page(b"/X Do", *[b"/X Do"] * 17, _TEXT)
        with pytest.raises(ReadError, match=r"^the PDF nests forms \(XObjects\) more than 16 deep$"):
            text_layer(body)

    def test_forms_drawn_past_the_cap_raise(self) -> None:
        body = _page(b"/X Do", *[b"/X Do /X Do"] * 14, _TEXT)  # 2**14 draws of the last form, 12 deep
        started = time.perf_counter()
        with pytest.raises(ReadError, match=r"^the PDF draws forms and images \(XObjects\) more than 10000 times$"):
            text_layer(body)
        assert time.perf_counter() - started < 5

    def test_a_chain_within_the_caps_reads(self) -> None:
        assert [line.text for line in text_layer(_page(b"/X Do", b"/X Do /X Do", _TEXT))] == ["aa"]  # drawn twice

    def test_streams_decoded_past_the_cap_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(flatread.pdf, "_DECODED_MAX", 1 << 20)
        body = _page(_TEXT + b" " * (2 << 20), flate=True)
        assert len(body) < 4 << 10
        with pytest.raises(ReadError, match=r"^the PDF's streams decode to more than 1048576 bytes$"):
            text_layer(body)
        assert [line.text for line in text_layer(_page(_TEXT + b" " * 1000, flate=True))] == ["a"]

    def test_a_stream_with_a_damaged_checksum_inflates_in_linear_time(self) -> None:
        stream = zlib.compress(_TEXT + b" " * (16 << 20), 9)
        damaged = stream[:-1] + bytes([stream[-1] ^ 1])
        started = time.perf_counter()
        assert pdftypes.decompress_corrupted(damaged) == _TEXT + b" " * (16 << 20)  # pdfminer's own took 6 s
        assert time.perf_counter() - started < 2
        with pytest.raises(zlib.error):
            pdftypes.decompress_corrupted(b"\0" + stream[1:])  # an error before the last three bytes

    def test_outside_a_read_pdfminers_decoders_have_no_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(flatread.pdf, "_DECODED_MAX", 1)
        assert pdftypes.zlib.decompress(zlib.compress(b"abc")) == b"abc"
        with pytest.raises(zlib.error, match="incomplete or truncated stream"):
            pdftypes.zlib.decompress(zlib.compress(b"abc")[:-2])

    def test_characters_past_a_pages_cap_or_the_bodys_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(flatread.pdf, "_PAGE_CHARS_MAX", 10)
        with pytest.raises(ReadError, match=r"^a page of the PDF draws more than 10 characters$"):
            text_layer(pdf(["abcdefghijk"]))
        assert len(text_layer(pdf(["abcdefghij"], ["abcdefghij"]))) == 2
        monkeypatch.setattr(flatread.pdf, "_CHARS_MAX", 15)
        with pytest.raises(ReadError, match=r"^the PDF draws more than 15 characters$"):
            text_layer(pdf(["abcdefghij"], ["abcdefghij"]))

    @pytest.mark.parametrize(
        "damage",
        [
            (b"trailer\n<<", b"trailerRoot\n<<"),
            (b"/Root 1 0 R", b"/Root 1 0 R /Encrypt << /Filter /Standard /V 1 /R 2 /O (x) /U (y) /P 0 >>"),
        ],
        ids=["an AssertionError: a name glued to trailer", "a struct.error: an /Encrypt without an /ID"],
    )
    def test_pdfminers_other_errors_are_read_errors(self, damage: tuple[bytes, bytes]) -> None:
        with pytest.raises(ReadError, match=r"^the body is not a readable PDF: "):
            text_layer(pdf(["ACME 100 01/15/2024"]).replace(*damage))


class _Wide(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("Name")]
    key: str
    value: float


class _WideHtml(LongFile[_Wide], HtmlFile[_Wide]):
    _keys = re.compile(r"Y\d")


def _wide(cell: str) -> bytes:
    header = "<tr><th>Name</th><th>Y1</th><th>Y2</th></tr>"
    return f"<table>{header}<tr><td>a</td><td>1</td><td>{cell}</td></tr></table>".encode()


def test_a_long_tables_typed_value_cell_is_one_text_run() -> None:
    assert _WideHtml(_Wide).rows(_wide("2.5")) == [_Wide("a", "Y1", 1.0), _Wide("a", "Y2", 2.5)]
    with pytest.raises(ReadError, match=r"^row 1: its 'Y2' cell holds the text runs \['5\.3', '1'\], not one$"):
        _WideHtml(_Wide).rows(_wide("5.3<sup>1</sup>"))
