"""Tests for the delimited-file steps of ``flatread.frames`` and :func:`~flatread.text.check_header`.

Each real file's tests hold these steps to its wire; what is pinned here is what no one wire shows: which row
:func:`check_widths` names when several are wrong, that a quoted separator is not counted, that :func:`read_quoted_rows`
keeps exactly the lines it is given, and that :func:`frame_rows` builds the same rows ``iter_rows`` does.
"""

import datetime as dt
from itertools import starmap

import msgspec
import polars as pl
import pytest
from flatread.cells import UtcDatetime
from flatread.errors import RaggedRowError, UnknownLayoutError
from flatread.frames import (
    check_widths,
    counted_lines,
    first_ragged,
    frame_line,
    frame_rows,
    frame_schema,
    read_quoted_rows,
    split_fields,
)
from flatread.text import Line, check_header


class _Row(msgspec.Struct, frozen=True, gc=False):
    day: dt.date
    at: UtcDatetime | None
    name: str
    value: float | None
    count: int


def test_frame_rows_builds_the_rows_iter_rows_does() -> None:
    frame = pl.DataFrame(
        {
            "day": [dt.date(2026, 1, 2), dt.date(2026, 1, 5)],
            "at": [dt.datetime(2026, 1, 2, 16, tzinfo=dt.UTC), None],
            "name": ["a", "b"],
            "value": [1.5, None],
            "count": [1, 2],
        },
        schema=frame_schema(_Row),
    )

    assert frame_rows(frame, _Row) == list(starmap(_Row, frame.iter_rows()))


def test_frame_rows_of_an_empty_frame_is_empty() -> None:
    assert frame_rows(pl.DataFrame(schema=frame_schema(_Row)), _Row) == []


def test_counted_lines_counts_fields_and_lines_alike() -> None:
    found = counted_lines("a|b|c\n\n1|2\n", "|")

    assert found.rows() == [(1, "a|b|c", 3), (3, "1|2", 2)]
    assert frame_line(found, -1) == Line(3, "1|2")


def test_a_quoted_separator_is_not_counted() -> None:
    found = counted_lines('a,"b, ""c""",d\n"open,e\n', ",", quoted=True)

    assert found.select("fields", "quotes").rows() == [(3, 0), (2, 1)]


def test_a_quote_inside_a_field_is_refused_as_polars_would_read_it_otherwise() -> None:
    rows = counted_lines('a, "x,y"\na,b"c,"d\n', ",", quoted=True)

    assert rows.select("fields", "quotes").rows() == [(3, 2), (3, 2)]
    with pytest.raises(RaggedRowError, match=r"^line 1 has a quote that does not close or sits inside a field"):
        check_widths(rows, 2)


def test_adjacent_fields_quoted_whole_are_each_removed() -> None:
    found = counted_lines('"a","b,c","d"\n', ",", quoted=True)

    assert found.select("fields", "quotes").rows() == [(3, 0)]


def test_check_widths_names_the_first_row_of_the_wrong_width() -> None:
    rows = counted_lines("1|2\n1\n1|2|3\n", "|")

    with pytest.raises(RaggedRowError, match=r"^line 2 should have 2 fields, not 1: '1'$"):
        check_widths(rows, 2)


def test_check_widths_passes_rows_of_the_header_width() -> None:
    check_widths(counted_lines("1|2\n3|4\n", "|"), 2)


def test_the_first_faulty_row_is_named_whether_its_quote_or_its_width_is_wrong() -> None:
    """A reader that keeps the rows before the first fault must not keep a ragged row before a quote's."""
    with pytest.raises(RaggedRowError, match=r"^line 1 should have 2 fields, not 1"):
        check_widths(counted_lines('1\n"2,3\n', ",", quoted=True), 2)
    with pytest.raises(RaggedRowError, match=r"^line 1 has a quote that does not close"):
        check_widths(counted_lines('"1,2\n3\n', ",", quoted=True), 2)


def test_first_ragged_gives_the_rows_position_and_its_error() -> None:
    found = first_ragged(counted_lines("a|b\nc|d\ne\n", "|"), 2)

    assert found is not None
    assert found[0] == 2
    assert str(found[1]).startswith("line 3 should have 2 fields")
    assert first_ragged(counted_lines("a|b\n", "|"), 2) is None


def test_a_long_line_is_quoted_cut_short() -> None:
    rows = counted_lines("x" * 500 + "\n", "|")

    with pytest.raises(RaggedRowError) as raised:
        check_widths(rows, 2)
    assert str(raised.value).endswith(repr("x" * 200))


def test_split_fields_keeps_every_cell_as_written() -> None:
    lines = pl.Series(["a| b|", "c|d|e"])

    assert split_fields(lines, "|", ["x", "y", "z"]).rows() == [("a", " b", ""), ("c", "d", "e")]


def test_read_quoted_rows_reads_only_the_rows_lines() -> None:
    text = 'h1,h2,h3,h4\n\na,"b, c"\nd,e\n'
    rows = counted_lines(text, ",", quoted=True).slice(1)

    assert read_quoted_rows(rows["line"], {"h1": pl.String, "h2": pl.String}).rows() == [("a", "b, c"), ("d", "e")]


def test_check_header_refuses_any_other_first_line() -> None:
    check_header(Line(1, "a|b"), "a|b")
    with pytest.raises(UnknownLayoutError, match=r"^line 1 is not the header 'a\|b': 'a\|c'$"):
        check_header(Line(1, "a|c"), "a|b")


def test_read_quoted_rows_of_no_rows_is_an_empty_frame_of_the_columns() -> None:
    frame = read_quoted_rows(pl.Series([], dtype=pl.String), {"h1": pl.String})

    assert frame.is_empty()
    assert frame.schema == pl.Schema({"h1": pl.String})
