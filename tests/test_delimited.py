"""Tests for ``flatread.delimited``: a delimited file read by ``pl.read_csv`` into its row struct's typed columns.

What is pinned here is the strictness no single wire shows whole: the header must be the declared labels, every row
its width, and a streamed file must read as the whole one does, however its chunks fall.
"""

import asyncio
import codecs
from typing import TYPE_CHECKING, Annotated, Any

import msgspec
import polars as pl
import pytest
from flatread.cells import CellError, Column
from flatread.delimited import read_csv, stream_csv
from flatread.errors import (
    EmptyBodyError,
    EncodingError,
    RaggedRowError,
    ReadError,
    TruncatedFileError,
    UnknownLayoutError,
)
from flatread.text import split_lines
from polars.testing import assert_frame_equal

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class _Row(msgspec.Struct, frozen=True, gc=False):
    symbol: Annotated[str, Column("Symbol")]
    count: Annotated[int, Column("Count")]


def _rows(frame: pl.DataFrame) -> tuple[list[str], list[tuple[Any, ...]]]:
    return frame.columns, frame.rows()


_READ = (["Symbol", "Count"], [("A", 1), ("B", 2)])


@pytest.mark.parametrize(
    "body",
    [
        b"Symbol,Count\nA,1\nB,2\n",
        b"Symbol,Count\nA,1\nB,2",
        b"Symbol,Count\r\nA,1\r\nB,2\r\n",
        codecs.BOM_UTF8 + b"Symbol,Count\nA,1\nB,2\n",
        b"\nSymbol,Count\n\nA,1\n\n\nB,2\n\n",
    ],
    ids=["LF", "no final line ending", "CRLF", "a byte-order mark", "blank lines"],
)
def test_a_file_reads_under_its_own_labels(body: bytes) -> None:
    assert _rows(read_csv(body, _Row)) == _READ


def test_a_header_alone_is_a_file_of_no_rows() -> None:
    assert _rows(read_csv(b"Symbol,Count\n", _Row)) == (["Symbol", "Count"], [])


@pytest.mark.parametrize("body", [b"", codecs.BOM_UTF8, b"\n\r\n"], ids=["nothing", "a byte-order mark", "blank"])
def test_a_body_of_no_header_is_empty(body: bytes) -> None:
    with pytest.raises(EmptyBodyError, match="the file is empty"):
        read_csv(body, _Row)


@pytest.mark.parametrize(
    "header",
    [b"Symbol,count", b"Count,Symbol", b"Symbol", b"Symbol,Count,Extra", b" Symbol,Count", b"Symbol,Count,Count"],
    ids=["case", "order", "a column short", "a column over", "padding", "a repeat"],
)
def test_a_header_that_is_not_the_labels_exactly_is_refused(header: bytes) -> None:
    with pytest.raises(UnknownLayoutError, match="is not the columns"):
        read_csv(header + b"\n" + b",".join([b"1"] * (header.count(b",") + 1)) + b"\n", _Row)


def test_any_order_reads_each_column_by_its_label_and_still_refuses_another() -> None:
    frame = read_csv(b"Count,Symbol\n1,A\n", _Row, ordered=False)
    assert frame.select("Symbol", "Count").rows() == [("A", 1)]
    with pytest.raises(UnknownLayoutError, match="is not the columns"):
        read_csv(b"Count,Name\n1,A\n", _Row, ordered=False)


class _Fillers(msgspec.Struct, frozen=True, gc=False):
    symbol: Annotated[str, Column("Symbol")]
    filler_1: Annotated[str | None, Column("Filler", missing=("",))]
    filler_2: Annotated[str | None, Column("Filler", missing=("",))]


@pytest.mark.parametrize("header", [True, False], ids=["a header", "no header"])
def test_a_repeated_label_is_named_as_polars_names_it(*, header: bool) -> None:
    body = b"Symbol|Filler|Filler\n" * header + b"A||x\n"
    frame = read_csv(body, _Fillers, separator="|", header=header)
    assert _rows(frame) == (["Symbol", "Filler", "Filler_duplicated_0"], [("A", None, "x")])


class _Versions(msgspec.Struct, frozen=True, gc=False):
    symbol: Annotated[str, Column(("Symbol", "Ticker"))]
    note: Annotated[str | None, Column("Note", missing=("",), optional=True)]
    count: Annotated[int, Column("Count")]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"Symbol,Note,Count\nA,,1\n", (["Symbol", "Note", "Count"], [("A", None, 1)])),
        (b"Ticker,Count\nA,1\n", (["Ticker", "Count"], [("A", 1)])),
    ],
    ids=["every column", "the optional left out, the alternate label"],
)
def test_a_layout_may_leave_out_an_optional_column_and_use_an_alternate_label(
    body: bytes, expected: tuple[list[str], list[tuple[Any, ...]]]
) -> None:
    assert _rows(read_csv(body, _Versions)) == expected


@pytest.mark.parametrize(
    ("body", "quoted"),
    [(b"Symbol,Count\nA,1\nB\n", False), (b'Symbol,Count\nA,1\n"B,2"\n', True), (b"Symbol,Count\nA,1\n   \n", False)],
    ids=["unquoted", "a quoted separator does not count", "spaces are no blank line"],
)
def test_a_short_row_is_ragged(body: bytes, *, quoted: bool) -> None:
    with pytest.raises(RaggedRowError, match=r"^line 3 has 1 fields, not the header's 2"):
        read_csv(body, _Row, quoted=quoted)


@pytest.mark.parametrize("quoted", [False, True])
def test_a_long_row_is_ragged(*, quoted: bool) -> None:
    with pytest.raises(RaggedRowError, match="has 3 fields, not the header's 2"):
        read_csv(b'Symbol,Count\nA,1\nB,2,"3"\n', _Row, quoted=quoted)


def test_quoting_is_read_only_when_asked() -> None:
    body = b'Symbol,Count\n"A, ""B""",1\n'
    assert read_csv(body, _Row, quoted=True)["Symbol"].to_list() == ['A, "B"']
    assert read_csv(b'Symbol,Count\n"A",1\n', _Row)["Symbol"].to_list() == ['"A"']
    with pytest.raises(RaggedRowError):
        read_csv(body, _Row)


def test_a_trailing_separator_is_framing() -> None:
    assert _rows(read_csv(b"Symbol|Count|\r\nA|1|\nB|2|\n", _Row, separator="|", trailing_separator=True)) == _READ


@pytest.mark.parametrize(
    ("body", "error", "named"),
    [
        (b"Symbol|Count|\nA|1|\nB|2\n", RaggedRowError, "line 3 has 2 fields, not the header's 3"),
        (b"Symbol|Count|\nA|1|x\n", RaggedRowError, "line 2 does not end with the separator '|'"),
        (b"Symbol|Count\nA|1\n", UnknownLayoutError, "does not end with the separator"),
    ],
    ids=["a row without it", "a cell after it", "a header without it"],
)
def test_a_trailing_separator_must_end_every_line(body: bytes, error: type[ReadError], named: str) -> None:
    with pytest.raises(error, match=named):
        read_csv(body, _Row, separator="|", trailing_separator=True)


def test_a_file_that_always_ends_with_a_line_ending_is_cut_off_without_one() -> None:
    assert _rows(read_csv(b"Symbol,Count\nA,1\nB,2\n", _Row, line_ending=True)) == _READ
    with pytest.raises(TruncatedFileError, match="ends mid-line, with 'B,2'"):
        read_csv(b"Symbol,Count\nA,1\nB,2", _Row, line_ending=True)


def test_a_headerless_file_takes_its_declared_labels() -> None:
    assert _rows(read_csv(b"A\t1\nB\t2\n", _Row, separator="\t", header=False)) == _READ
    with pytest.raises(RaggedRowError, match="line 2 has 1 fields"):
        read_csv(b"A\t1\nB\n", _Row, separator="\t", header=False)


def test_lines_before_the_header_are_skipped() -> None:
    assert (
        _rows(read_csv(b'Report "x\nas of today\nSymbol,Count\nA,1\nB,2\n', _Row, skip_lines=2, quoted=True)) == _READ
    )
    with pytest.raises(RaggedRowError, match=r"^line 4 has 1 fields"):
        read_csv(b"Report\nSymbol,Count\nA,1\nB\n", _Row, skip_lines=1)


def test_a_cell_error_names_its_line_blank_lines_counted() -> None:
    with pytest.raises(CellError, match=r"^row 5: the 'Count' cell 'x' is not a whole number"):
        read_csv(b"Symbol,Count\n\nA,1\n\nB,x\n", _Row)


def test_the_body_must_be_utf8_unless_it_is_text_already() -> None:
    body = "Symbol,Count\ncafé,1\n".encode("latin-1")
    with pytest.raises(EncodingError, match="not UTF-8"):
        read_csv(body, _Row)
    assert read_csv(body.decode("latin-1"), _Row)["Symbol"].to_list() == ["café"]


def test_a_files_own_lines_come_off_before_its_rows_are_read() -> None:
    head, rows, tail = split_lines("As of 2026-01-02\nSymbol,Count\nA,1\nB,2\n2 rows\n", head=1, tail=1)
    assert (head, _rows(read_csv(rows, _Row)), tail) == (["As of 2026-01-02"], _READ, ["2 rows"])


# Core gaps found while rewriting this suite: each is the behaviour the read should have.


def test_a_header_of_another_width_is_an_unknown_layout_before_its_rows_are_ragged() -> None:
    with pytest.raises(UnknownLayoutError, match="is not the columns"):
        read_csv(b"Symbol,Count,Extra\nA,1\n", _Row)


def test_a_row_of_empty_cells_is_a_row() -> None:
    with pytest.raises(CellError, match="the 'Count' cell ''"):
        read_csv(b"Symbol,Count\nA,1\n,\n", _Row)


@pytest.mark.parametrize("body", [b"A\n", b"A,1,2\nB,2\n", b"\nA,1\n"], ids=["short", "long", "a blank line first"])
def test_a_headerless_file_of_the_wrong_width_is_a_read_error(body: bytes) -> None:
    try:
        read_csv(body, _Row, header=False)
    except ReadError:
        return
    except Exception as exc:  # noqa: BLE001  # the gap: polars' SchemaError escapes
        pytest.fail(f"{type(exc).__name__}: {exc}")


def test_a_quoted_field_may_hold_a_line_ending() -> None:
    assert read_csv(b'Symbol,Count\n"A\nB",1\n', _Row, quoted=True)["Symbol"].to_list() == ["A\nB"]


# --- The stream -------------------------------------------------------------------------------------------------------


async def _chunks(body: bytes, size: int) -> AsyncIterator[bytes]:
    for at in range(0, len(body), size):
        await asyncio.sleep(0)
        yield body[at : at + size]


async def _stream(  # the body, how it arrives, then stream_csv's options
    body: bytes, size: int, batch_rows: int, into: list[pl.DataFrame], *, header: bool = True, quoted: bool = False
) -> None:
    """Keep each frame as it comes, so the ones before an error are kept."""
    async for frame in stream_csv(_chunks(body, size), _Row, batch_rows=batch_rows, header=header, quoted=quoted):
        into.append(frame)


_SIZES = pytest.mark.parametrize("size", [1, 3, 1 << 16], ids=["byte", "three", "whole"])
_BATCHES = pytest.mark.parametrize("batch_rows", [1, 2, 10])


@_SIZES
@_BATCHES
@pytest.mark.parametrize(
    "body",
    [
        b"Symbol,Count\nA,1\nB,2\nC,3\n",
        b"Symbol,Count\r\nA,1\r\nB,2\r\nC,3\r\n",
        codecs.BOM_UTF8 + "Symbol,Count\nAé,1\n\nB,2\n\n".encode(),
        b"Symbol,Count\n",
    ],
    ids=["LF", "CRLF", "a byte-order mark, a multi-byte character and blank lines", "no rows"],
)
async def test_a_streamed_file_is_the_whole_files_frame_however_the_chunks_fall(
    body: bytes, size: int, batch_rows: int
) -> None:
    frames: list[pl.DataFrame] = []
    await _stream(body, size, batch_rows, frames)
    assert all(frame.height <= batch_rows for frame in frames)
    whole = read_csv(body, _Row)
    assert_frame_equal(pl.concat(frames) if frames else whole.clear(), whole)


@_SIZES
@pytest.mark.parametrize(("batch_rows", "before"), [(1, [("A", 1), ("B", 2)]), (2, [("A", 1), ("B", 2)]), (10, [])])
async def test_a_stream_yields_every_batch_before_a_bad_cell_then_raises(
    size: int, batch_rows: int, before: list[tuple[str, int]]
) -> None:
    frames: list[pl.DataFrame] = []
    with pytest.raises(CellError, match=r"^row 4: the 'Count' cell 'x'"):
        await _stream(b"Symbol,Count\nA,1\nB,2\nC,x\nD,4\n", size, batch_rows, frames)
    assert [row for frame in frames for row in frame.rows()] == before


async def test_a_headerless_or_quoted_stream_reads_as_its_whole_file() -> None:
    frames: list[pl.DataFrame] = []
    await _stream(b'A,1\n"B,C",2\n', 2, 1, frames, header=False, quoted=True)
    assert [frame.rows() for frame in frames] == [[("A", 1)], [("B,C", 2)]]


@_SIZES
async def test_a_stream_cut_mid_line_is_truncated(size: int) -> None:
    with pytest.raises(TruncatedFileError, match="ends mid-line, with 'B,2'"):
        await _stream(b"Symbol,Count\nA,1\nB,2", size, 1, [])


@pytest.mark.parametrize("header", [True, False])
async def test_an_empty_stream_is_empty(*, header: bool) -> None:
    with pytest.raises(EmptyBodyError, match="the file is empty"):
        await _stream(b"", 1, 1, [], header=header)


async def test_a_stream_checks_its_header() -> None:
    with pytest.raises(UnknownLayoutError, match="is not the columns"):
        await _stream(b"Symbol,Total\nA,1\n", 4, 1, [])


async def test_a_batch_holds_at_least_one_line() -> None:
    with pytest.raises(ValueError, match="at least one line, not 0"):
        await _stream(b"Symbol,Count\n", 4, 0, [])


async def test_a_stream_yields_every_batch_before_a_ragged_one_then_raises() -> None:
    frames: list[pl.DataFrame] = []
    with pytest.raises(RaggedRowError, match=r"^line 4 has 1 fields"):
        await _stream(b"Symbol,Count\nA,1\nB,2\nC\n", 1 << 16, 2, frames)
    assert pl.concat(frames).rows() == [("A", 1), ("B", 2)]


async def test_a_ragged_row_in_a_later_batch_is_named_by_its_line() -> None:
    with pytest.raises(RaggedRowError, match=r"^line 4 has 1 fields"):
        await _stream(b"Symbol,Count\nA,1\nB,2\nC\n", 1 << 16, 1, [])


async def test_a_stream_of_a_wrong_header_alone_is_refused() -> None:
    with pytest.raises(UnknownLayoutError, match="is not the columns"):
        await _stream(b"Symbol,Total\n", 4, 1, [])
