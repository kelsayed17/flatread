"""``flatread.pdf``: a PDF's text layer read into positioned lines, and a table read out of them."""

import datetime as dt
import typing
from typing import TYPE_CHECKING, Annotated

import msgspec
import pytest

from flatread.errors import LayoutError, ReadError
from flatread.fields import Format
from flatread.pdf import LaidOutPdf, PdfFile, PdfLine, text_layer
from tests.support.pdf import document, pdf

if TYPE_CHECKING:
    from collections.abc import Sequence


class _Trade(msgspec.Struct, frozen=True, gc=False):
    asset: str
    shares: int
    day: Annotated[dt.date, Format("%m/%d/%Y")]
    note: str | None


class _Trades(PdfFile[_Trade]):
    """Each line ``ASSET SHARES DAY [NOTE]``: the cells by position, split on spaces."""

    @typing.override
    def _records(self, lines: Sequence[PdfLine]) -> list[list[str | None]]:
        return [[*(cells := line.words.split(" ", 3)), *[None] * (4 - len(cells))] for line in lines]


_TRADES = _Trades(_Trade)


def test_each_line_s_cells_are_typed_by_the_struct() -> None:
    body = pdf(["ACME 100 01/15/2024", "BETA 7 02/01/2024 a gift"])
    assert _TRADES.rows(body) == [
        _Trade("ACME", 100, dt.date(2024, 1, 15), None),
        _Trade("BETA", 7, dt.date(2024, 2, 1), "a gift"),
    ]
    assert _TRADES.frame(body).columns == ["asset", "shares", "day", "note"]


def test_the_lines_are_each_page_s_in_drawn_order_with_their_positions() -> None:
    lines = text_layer(pdf(["one", "two  words"], ["three"]))
    assert [(line.page, line.text) for line in lines] == [(0, "one"), (0, "two  words"), (1, "three")]
    assert lines[0].top > lines[1].top
    assert lines[1].words == "two words"
    xs = [x for x, _ in lines[1].chars]
    assert xs[0] == pytest.approx(72.0)
    assert xs == sorted(xs)


def test_a_cell_that_is_not_its_type_raises() -> None:
    with pytest.raises(ReadError, match=r"'shares' cell 'many' is not a whole number: row 1"):
        _TRADES.frame(pdf(["ACME many 01/15/2024"]))


def test_a_record_of_another_width_raises() -> None:
    class _Short(_Trades):
        @typing.override
        def _records(self, lines: Sequence[PdfLine]) -> list[list[str | None]]:
            return [[line.text] for line in lines]

    with pytest.raises(LayoutError, match=r"^record 1 has 1 cells, not 4$"):
        _Short(_Trade).frame(pdf(["ACME"]))


def test_a_pdf_of_no_records_raises_unless_the_format_reads_none() -> None:
    class _Empty(_Trades):
        @typing.override
        def _records(self, lines: Sequence[PdfLine]) -> list[list[str | None]]:
            del lines
            return []

    class _MaybeEmpty(_Empty):
        _rows_required = False

    with pytest.raises(ReadError, match="the body is a header and no rows"):
        _Empty(_Trade).frame(pdf(["a title"]))
    assert _MaybeEmpty(_Trade).rows(pdf(["a title"])) == []


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (b"", "^the body is empty$"),
        (b"not a pdf", "^the body is not a readable PDF: "),
        (pdf(["ACME 100 01/15/2024"])[:200], "^the body is not a readable PDF: "),
        (pdf([]), "^the PDF has no text layer: a scanned document$"),
    ],
    ids=["empty", "not-a-pdf", "cut", "no-text-layer"],
)
def test_a_body_with_no_readable_text_layer_raises(body: bytes, error: str) -> None:
    with pytest.raises(ReadError, match=error):
        text_layer(body)
    with pytest.raises(ReadError, match=error):
        _TRADES.frame(body)


# -- LaidOutPdf: the lines in reading order, a table's cells by their columns' places --------------------------------


class _Positions(msgspec.Struct, frozen=True, gc=False):
    position: str
    organization: str | None


class _Table(LaidOutPdf[_Positions]):
    """A header line ``Position  Organization``, then rows of lines no more than 12 points apart."""

    _slack = 3.0
    _row_gap = 12.0

    @typing.override
    def _records(self, lines: Sequence[PdfLine]) -> list[list[str | None]]:
        head, *rows = self._runs(lines)
        edges = self._edges(head[0], ["position", "organization"])
        assert edges is not None
        return [list(self._column_cells(row, edges)) for row in rows]


_TABLE = _Table(_Positions)


def _line(page: int, top: float, *cells: tuple[float, str]) -> PdfLine:
    """Draw a line of *cells*, each its left edge and text, every character 4 points wide."""
    return PdfLine(page, top, tuple((x + 4 * i, c) for x, text in cells for i, c in enumerate(f"{text} ")))


def test_a_laid_out_pdf_reads_top_to_bottom_and_joins_one_baseline_s_segments() -> None:
    body = pdf([(72, 600, "Trustee"), (300, 600.4, "Estate of"), (72, 700, "Position"), (300, 700, "Organization")])
    assert [line.words for line in _TABLE._lines(body)] == ["Position Organization", "Trustee Estate of"]
    assert _TABLE.rows(body) == [_Positions("Trustee", "Estate of")]


def test_a_column_s_cell_is_every_character_at_or_right_of_its_edge_less_the_slack() -> None:
    edges = [25.0, 200.0]
    lines = [_line(0, 700, (23, "Trustee"), (198, "Estate of")), _line(0, 689, (10, "and heir"), (198, "Brown"))]
    assert _TABLE._column_cells(lines, edges) == ["Trustee and heir", "Estate of Brown"]
    assert _TABLE._column_cells([_line(0, 700, (197, "Estate"))], edges) == ["", "Estate"]
    assert _TABLE._column_cells([_line(0, 700, (196, "Estate"))], edges) == ["E", "state"]


def test_the_edges_are_each_label_s_first_word_in_order_in_any_case() -> None:
    header = _line(0, 700, (25, "POSITION"), (200, "Name of Organization"))
    assert _TABLE._edges(header, ["position", "name of organization"]) == [25.0, 200.0]
    assert _TABLE._edges(header, ["name", "position"]) is None
    assert _TABLE._edges(header, ["pos"]) is None


def test_a_row_is_a_run_of_lines_no_more_than_the_gap_apart_a_page_break_ending_it() -> None:
    lines = [_line(0, 700, (25, "a")), _line(0, 688, (25, "b")), _line(0, 675, (25, "c")), _line(1, 700, (25, "d"))]
    assert [[line.words for line in run] for run in _TABLE._runs(lines)] == [["a", "b"], ["c"], ["d"]]


def test_a_figure_s_characters_are_read_where_the_page_draws_it_a_rule_skipped() -> None:
    form = b"BT /F1 11 Tf 72 700 Td (inside) Tj ET"
    page = b"BT /F1 11 Tf 72 720 Td (outside) Tj ET 72 690 m 300 690 l S /Fx Do"
    body = document(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            (
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                b"/Resources << /Font << /F1 3 0 R >> /XObject << /Fx 6 0 R >> >> /Contents 5 0 R >>"
            ),
            b"<< /Length %d >>\nstream\n%b\nendstream" % (len(page), page),
            (
                b"<< /Type /XObject /Subtype /Form /BBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
                b"/Length %d >>\nstream\n%b\nendstream" % (len(form), form)
            ),
        ]
    )
    assert [line.text for line in text_layer(body)] == ["outside", "inside"]


def test_a_space_the_grouper_inserts_between_words_sits_at_the_character_before_it() -> None:
    (line,) = text_layer(pdf([(72, 700, "ab"), (93, 700, "cd")]))
    assert line.text == "ab cd"
    assert line.chars[2][0] == line.chars[1][0]


def test_no_slack_gives_a_character_left_of_an_edge_to_the_column_before() -> None:
    class Tight(_Table):
        _slack = 0.0

    edges = [25.0, 200.0]
    assert Tight(_Positions)._column_cells([_line(0, 700, (198, "E"))], edges) == ["E", ""]
    assert Tight(_Positions)._column_cells([_line(0, 700, (200, "E"))], edges) == ["", "E"]
    assert _TABLE._column_cells([_line(0, 700, (198, "E"))], edges) == ["", "E"]
