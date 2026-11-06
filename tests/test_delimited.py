"""Tests for ``flatread.delimited``: a file declared once by its structs, read as rows or a frame, in one order.

The threshold files and the CSVs declared on it hold the parts to real wires in their own tests; what is pinned here is
the pipeline itself: that both forms agree, the header the labels make, the order of the checks, a leap second, and that
a file declared wrongly fails when it is built.
"""

import datetime as dt
from typing import TYPE_CHECKING, Annotated, NamedTuple

import msgspec
import polars as pl
import pytest
from flatread.cells import Column
from flatread.delimited import (
    ByName,
    ColumnTotal,
    DelimitedFile,
    FromHeader,
    FromPreamble,
    FromTrailer,
    GenerationStamp,
    Labels,
    Layouts,
    NoHeader,
    NotPublishedLine,
    RowCount,
    StreamedFile,
    not_published_body,
)
from flatread.errors import (
    ControlTotalError,
    EncodingError,
    NotPublishedError,
    RaggedRowError,
    TruncatedFileError,
    UnknownLayoutError,
)
from flatread.text import Line, decode_utf8

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class _Row(msgspec.Struct, frozen=True, gc=False):
    symbol: Annotated[str, Column("Symbol")]
    price: Annotated[float | None, Column("Price", missing=("",))]


class _File(msgspec.Struct, frozen=True, gc=False):
    generated_at: Annotated[dt.datetime | None, FromTrailer(GenerationStamp())]
    rows: list[_Row]


class _Frame(NamedTuple):
    generated_at: dt.datetime | None
    rows: pl.DataFrame


_NOT_PUBLISHED = NotPublishedLine("No file today.", "the answer for a day with no file")

_SPEC = DelimitedFile.of_file(_File, frame_type=_Frame, separator="|", not_published=_NOT_PUBLISHED)

_BODY = b"Symbol|Price\nAAA|1.5\n\nBBB|\n20260102030405\n"


def test_the_file_and_its_frame_agree() -> None:
    file, frame = _SPEC.read(_BODY), _SPEC.read_frame(_BODY)

    assert file == _File(dt.datetime(2026, 1, 2, 3, 4, 5), [_Row("AAA", 1.5), _Row("BBB", None)])
    assert frame.generated_at == file.generated_at
    assert frame.rows.rows() == [("AAA", 1.5), ("BBB", None)]
    assert frame.rows.schema == pl.Schema({"symbol": pl.String, "price": pl.Float64})


def test_the_header_is_the_labels_joined() -> None:
    assert _SPEC.headers == ("Symbol|Price",)


def test_a_file_of_rows_alone_reads_as_a_list_and_a_dataframe() -> None:
    rows = DelimitedFile.of_rows(_Row, separator=",", quoted=True, line_ending=True)
    body = b'Symbol,Price\n"A,B",2\n'

    assert rows.read(body) == [_Row("A,B", 2.0)]
    assert rows.read_frame(body).rows() == [("A,B", 2.0)]


def test_a_header_and_trailer_of_no_rows_is_an_empty_file() -> None:
    assert _SPEC.read(b"Symbol|Price\n20260102030405\n").rows == []


def test_the_header_is_checked_first() -> None:
    with pytest.raises(UnknownLayoutError, match=r"^line 1 is not the header 'Symbol\|Price'"):
        _SPEC.read(b"Other|Header\nAAA\nnot a stamp\n")


def test_the_publishers_answer_is_not_published() -> None:
    with pytest.raises(NotPublishedError, match="the answer for a day with no file"):
        _SPEC.read(b"<html>\nNo file today.\n</html>\n")


def test_the_trailer_is_checked_before_the_widths() -> None:
    with pytest.raises(TruncatedFileError, match=r"^no trailer: line 3"):
        _SPEC.read(b"Symbol|Price\nAAA\nBBB|1\n")


def test_a_file_that_ends_with_its_header_has_no_trailer() -> None:
    with pytest.raises(TruncatedFileError, match=r"^no trailer: the file ends with its header, on line 1"):
        _SPEC.read(b"Symbol|Price\n")


def test_a_row_of_the_wrong_width_is_ragged() -> None:
    with pytest.raises(RaggedRowError, match=r"^line 2 should have 2 fields, not 1"):
        _SPEC.read(b"Symbol|Price\nAAA\n20260102030405\n")


def test_a_cell_that_is_not_its_shape_fails_the_read() -> None:
    with pytest.raises(UnknownLayoutError, match=r"^line 2: the 'Price' cell 'n/a' is not a number"):
        _SPEC.read(b"Symbol|Price\nAAA|n/a\n20260102030405\n")


def test_the_body_must_be_utf8() -> None:
    with pytest.raises(EncodingError):
        _SPEC.read(b"Symbol|Price\n\xff|1\n20260102030405\n")


def test_a_file_whose_every_copy_ends_with_a_line_ending_is_cut_off_without_one() -> None:
    rows = DelimitedFile.of_rows(_Row, separator=",", line_ending=True)

    with pytest.raises(TruncatedFileError):
        rows.read(b"Symbol,Price\nAAA,1")


class _Optional(msgspec.Struct, frozen=True, gc=False):
    a: Annotated[str, Column("A")]
    b: Annotated[float | None, Column("B", optional=True)]


def test_an_optional_column_gives_the_file_a_second_header() -> None:
    rows = DelimitedFile.of_rows(_Optional, separator="|")

    assert rows.headers == ("A|B", "A")
    assert rows.read(b"A\nx\n") == [_Optional("x", None)]
    assert rows.read(b"A|B\nx|2\n") == [_Optional("x", 2.0)]


class _AsOf:
    def read(self, line: Line) -> str:
        return line.text.removeprefix("as of ")


class _Preambled(msgspec.Struct, frozen=True, gc=False):
    as_of: Annotated[str, FromPreamble(_AsOf())]
    rows: list[_Row]


class _PreambledFrame(NamedTuple):
    as_of: str
    rows: pl.DataFrame


def test_a_preamble_line_carries_its_value() -> None:
    spec = DelimitedFile.of_file(_Preambled, frame_type=_PreambledFrame, separator="|")

    assert spec.read(b"as of today\nSymbol|Price\nAAA|1\n") == _Preambled("today", [_Row("AAA", 1.0)])


def test_a_leap_second_is_the_next_minutes_first() -> None:
    assert GenerationStamp().read(Line(9, "20261231235960")) == dt.datetime(2027, 1, 1)


def test_a_stamp_that_is_no_real_time_is_refused() -> None:
    with pytest.raises(UnknownLayoutError, match=r"^line 9: the generation stamp '20261302030405' is no real"):
        GenerationStamp().read(Line(9, "20261302030405"))


class _Swapped(NamedTuple):
    rows: pl.DataFrame
    generated_at: dt.datetime | None


def test_a_frame_type_that_is_not_the_file_structs_fails_when_the_file_is_built() -> None:
    with pytest.raises(ValueError, match="are not _File's"):
        DelimitedFile.of_file(_File, frame_type=_Swapped, separator="|")


class _Unsaid(msgspec.Struct, frozen=True, gc=False):
    note: str
    rows: list[_Row]


class _UnsaidFrame(NamedTuple):
    note: str
    rows: pl.DataFrame


def test_a_file_struct_field_that_says_nothing_of_where_it_comes_from_fails_when_the_file_is_built() -> None:
    with pytest.raises(TypeError, match="says nothing of where it is written"):
        DelimitedFile.of_file(_Unsaid, frame_type=_UnsaidFrame, separator="|")


def test_a_declared_file_cannot_be_changed() -> None:
    with pytest.raises(AttributeError, match="cannot be changed"):
        _SPEC._separator = ","  # the attempt is the test


def test_the_header_is_checked_before_the_line_ending() -> None:
    """A page served in place of the file, ending without a line ending, is not the file rather than cut off."""
    rows = DelimitedFile.of_rows(_Row, separator=",", line_ending=True)

    with pytest.raises(UnknownLayoutError):
        rows.read(b"<html>")


def test_a_header_may_quote_its_labels_open_with_a_mark_and_end_with_a_separator() -> None:
    rows = DelimitedFile.of_rows(_Row, separator="|", header=Labels(quote='"', prefix="#", trailing_separator=True))

    assert rows.headers == ('#"Symbol"|"Price"|',)
    assert rows.read(b'#"Symbol"|"Price"|\nAAA|1\n') == [_Row("AAA", 1.0)]


def test_a_header_may_be_compared_with_its_labels_padding_taken_off() -> None:
    rows = DelimitedFile.of_rows(_Row, separator=",", header=Labels(strip=True))

    assert rows.read(b"Symbol  , Price\nAAA,1\n") == [_Row("AAA", 1.0)]


class _Renamed(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[str, Column(("Report_Date_as_YYYY-MM-DD", "Report_Date_as_MM_DD_YYYY"))]


def test_a_column_may_go_by_either_of_its_labels() -> None:
    rows = DelimitedFile.of_rows(_Renamed, separator=",")

    assert rows.headers == ("Report_Date_as_YYYY-MM-DD", "Report_Date_as_MM_DD_YYYY")
    assert rows.read(b"Report_Date_as_MM_DD_YYYY\nx\n") == [_Renamed("x")]


class _Tenors(msgspec.Struct, frozen=True, gc=False):
    date: Annotated[str, Column("Date")]
    short: Annotated[float | None, Column("1 Mo", optional=True)]
    long: Annotated[float | None, Column("30 Yr", optional=True)]


class _TenorFile(msgspec.Struct, frozen=True, gc=False):
    tenors: Annotated[tuple[str, ...], FromHeader()]
    rows: list[_Tenors]


class _TenorFrame(NamedTuple):
    tenors: tuple[str, ...]
    rows: pl.DataFrame


def test_an_enumerated_layout_is_one_of_its_headers_and_the_file_states_which() -> None:
    spec = DelimitedFile.of_file(
        _TenorFile, frame_type=_TenorFrame, separator=",", header=Layouts((("date", "long"), ("date", "short", "long")))
    )

    assert spec.headers == ("Date,30 Yr", "Date,1 Mo,30 Yr")
    file = spec.read(b"Date,30 Yr\n01/02/2026,4.5\n")
    assert file == _TenorFile(("Date", "30 Yr"), [_Tenors("01/02/2026", None, 4.5)])
    with pytest.raises(UnknownLayoutError):
        spec.read(b"Date,1 Mo\n01/02/2026,4.5\n")


def test_a_file_without_a_header_is_every_line_a_row_and_may_refuse_one() -> None:
    rows = DelimitedFile.of_rows(_Row, separator=",", header=NoHeader(refuse=Labels()))

    assert rows.read(b"AAA,1\nBBB,\n") == [_Row("AAA", 1.0), _Row("BBB", None)]
    with pytest.raises(UnknownLayoutError, match="which this file never has"):
        rows.read(b"Symbol,Price\nAAA,1\n")


class _Count:
    def read(self, line: Line) -> int:
        return int(line.text.removeprefix("count "))


class _Total:
    def read(self, line: Line) -> int:
        return int(line.text.removeprefix("total "))


class _Counted(msgspec.Struct, frozen=True, gc=False):
    symbol: Annotated[str, Column("Symbol")]
    quantity: Annotated[int, Column("Quantity")]


class _Totalled(msgspec.Struct, frozen=True, gc=False):
    rows: list[_Counted]
    count: Annotated[int, FromTrailer(_Count())]
    total: Annotated[int, FromTrailer(_Total())]


class _TotalledFrame(NamedTuple):
    rows: pl.DataFrame
    count: int
    total: int


_TOTALLED = DelimitedFile.of_file(
    _Totalled,
    frame_type=_TotalledFrame,
    separator="|",
    checks=(RowCount("count"), ColumnTotal("quantity", "total")),
)


def test_two_trailer_lines_carry_a_count_and_a_total_the_rows_must_match() -> None:
    assert _TOTALLED.read(b"Symbol|Quantity\nA|1\nB|2\ncount 2\ntotal 3\n").count == 2
    with pytest.raises(ControlTotalError, match="declares 3 rows, but 2 were read"):
        _TOTALLED.read(b"Symbol|Quantity\nA|1\nB|2\ncount 3\ntotal 3\n")
    with pytest.raises(ControlTotalError, match="total quantity of 4, but the rows sum to 3"):
        _TOTALLED.read(b"Symbol|Quantity\nA|1\nB|2\ncount 2\ntotal 4\n")


@pytest.mark.parametrize("column", ["qq", "symbol"])
def test_a_total_over_no_column_of_whole_numbers_fails_when_the_file_is_built(column: str) -> None:
    with pytest.raises(ValueError, match="not a column of whole numbers that is never None"):
        DelimitedFile.of_file(
            _Totalled, frame_type=_TotalledFrame, separator="|", checks=(ColumnTotal(column, "total"),)
        )


def test_a_check_that_reads_a_field_the_file_does_not_state_fails_when_the_file_is_built() -> None:
    with pytest.raises(ValueError, match="does not state"):
        DelimitedFile.of_file(_Totalled, frame_type=_TotalledFrame, separator="|", checks=(RowCount("rows_read"),))


def test_a_trailing_separator_on_every_row_is_framing() -> None:
    rows = DelimitedFile.of_rows(_Row, separator="|", header=Labels(trailing_separator=True), trailing_separator=True)

    assert rows.read(b"Symbol|Price|\nAAA|1|\n") == [_Row("AAA", 1.0)]
    with pytest.raises(RaggedRowError, match=r"does not end with '\|', as every row does"):
        rows.read(b"Symbol|Price|\nAAA|1\n")


def test_a_body_that_is_the_publishers_answer_is_not_published_before_it_is_read() -> None:
    rows = DelimitedFile.of_rows(
        _Row,
        separator=",",
        line_ending=True,
        text=not_published_body(decode_utf8, exactly=b"File requested does not exist.", meaning="OCC's answer"),
    )

    with pytest.raises(NotPublishedError, match="not published: the body is OCC's answer"):
        rows.read(b"File requested does not exist.")


def test_the_publishers_line_is_its_answer_when_the_preamble_fails() -> None:
    class Title:
        def read(self, line: Line) -> str:
            if line.text != "THE TITLE":
                msg = f"line {line.number} is not the title"
                raise UnknownLayoutError(msg)
            return line.text

    class File(msgspec.Struct, frozen=True, gc=False):
        title: Annotated[str, FromPreamble(Title())]
        rows: list[_Row]

    class Frame(NamedTuple):
        title: str
        rows: pl.DataFrame

    spec = DelimitedFile.of_file(File, frame_type=Frame, separator="|", not_published=_NOT_PUBLISHED)

    with pytest.raises(NotPublishedError):
        spec.read(b"<html>\nNo file today.\n</html>\n")


class _First:
    def read(self, line: Line) -> str:
        return f"first {line.text}"


class _Second:
    def read(self, line: Line) -> str:
        return f"second {line.text}"


class _OneFile(msgspec.Struct, frozen=True, gc=False):
    note: Annotated[str, FromTrailer(_First())]
    rows: list[_Row]


class _OtherFile(msgspec.Struct, frozen=True, gc=False):
    note: Annotated[str, FromTrailer(_Second())]
    rows: list[_Row]


class _NoteFrame(NamedTuple):
    note: str
    rows: pl.DataFrame


def test_two_files_whose_parts_compare_equal_keep_each_its_own_part() -> None:
    """``typing`` caches ``Annotated`` by how its metadata compares: equal parts must not share a cache entry."""
    one = DelimitedFile.of_file(_OneFile, frame_type=_NoteFrame, separator="|")
    other = DelimitedFile.of_file(_OtherFile, frame_type=_NoteFrame, separator="|")
    body = b"Symbol|Price\nA|1\nend\n"

    assert (one.read(body).note, other.read(body).note) == ("first end", "second end")


class _QuotedTenorFile(msgspec.Struct, frozen=True, gc=False):
    tenors: Annotated[tuple[str, ...], FromHeader(optional=True)]
    rows: list[_Tenors]


def test_a_header_may_quote_every_label_but_its_bare_fields_and_state_its_optional_columns() -> None:
    spec = DelimitedFile.of_file(
        _QuotedTenorFile,
        frame_type=_TenorFrame,
        separator=",",
        header=Layouts((("date", "long"), ("date", "short", "long")), quote='"', bare=("date",)),
    )

    assert spec.headers == ('Date,"30 Yr"', 'Date,"1 Mo","30 Yr"')
    assert spec.read(b'Date,"1 Mo","30 Yr"\n01/02/2026,4.1,4.5\n').tenors == ("1 Mo", "30 Yr")
    with pytest.raises(UnknownLayoutError):
        spec.read(b'"Date","1 Mo","30 Yr"\n01/02/2026,4.1,4.5\n')


def test_a_file_without_a_header_refuses_one_written_in_any_style() -> None:
    rows = DelimitedFile.of_rows(_Row, separator=",", header=NoHeader(refuse=Labels(quote='"')))

    with pytest.raises(UnknownLayoutError, match="which this file never has"):
        rows.read(b'"Symbol","Price"\nAAA,1\n')


class _Named(msgspec.Struct, frozen=True, gc=False):
    a: Annotated[str, Column("A")]
    b: Annotated[int, Column("B")]
    c: Annotated[float | None, Column("C", optional=True)]


_BY_NAME = DelimitedFile.of_rows(_Named, separator=",", header=ByName(quote='"', strip=True))


def test_a_header_by_name_reads_the_columns_in_its_own_order() -> None:
    assert _BY_NAME.read(b'"B", "A"\n2,x\n') == [_Named("x", 2, None)]
    assert _BY_NAME.read(b"C,A,B\n1.5,x,2\n") == [_Named("x", 2, 1.5)]


@pytest.mark.parametrize(
    ("header", "named"),
    [("A,B,D", "the rows do not have"), ("A,B,A", "more than once"), ("A,C", "leaves out the columns")],
)
def test_a_header_by_name_refuses_unknown_repeated_and_missing_columns(header: str, named: str) -> None:
    with pytest.raises(UnknownLayoutError, match=named):
        _BY_NAME.read(header.encode() + b"\nx,2,1\n")


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


_STREAMED = StreamedFile(_Row, separator=",")


async def test_a_streamed_file_yields_its_rows_in_batches() -> None:
    body = (b"Symbol,Price\nA,1\nB,\n", b"C,3\n")

    frames = [frame async for frame in _STREAMED.stream_frames(_chunks(*body), batch_rows=2)]
    rows = [row async for row in _STREAMED.stream(_chunks(*body), batch_rows=2)]

    assert [frame.height for frame in frames] == [2, 1]
    assert rows == [_Row("A", 1.0), _Row("B", None), _Row("C", 3.0)]


@pytest.mark.parametrize(
    ("bad", "error"),
    [(b"C\n", RaggedRowError), (b"C,n/a\n", UnknownLayoutError)],
)
async def test_a_stream_yields_every_row_before_the_first_faulty_line_then_raises(
    bad: bytes, error: type[Exception]
) -> None:
    read: list[_Row] = []
    rows = _STREAMED.stream(_chunks(b"Symbol,Price\nA,1\nB,2\n" + bad + b"D,4\n"), batch_rows=10)
    with pytest.raises(error, match=r"^line 4"):
        await _collect(rows, read)

    assert read == [_Row("A", 1.0), _Row("B", 2.0)]


async def _collect(rows: AsyncIterator[_Row], into: list[_Row]) -> None:
    """Gather *rows* into *into*, so the rows read before an error stay for the test."""
    async for row in rows:
        into.append(row)  # kept through the error, which a comprehension would not


async def test_a_stream_checks_its_header() -> None:
    with pytest.raises(UnknownLayoutError, match="is not the header"):
        [row async for row in _STREAMED.stream(_chunks(b"Other\nA,1\n"), batch_rows=10)]


class _Renamed2(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[str, Column(("Old Name", "New Name"))]
    b: Annotated[str, Column("B")]


def test_a_by_name_header_and_an_enumerated_layout_take_every_label_a_column_has() -> None:
    by_name = DelimitedFile.of_rows(_Renamed2, separator=",", header=ByName())
    layouts = DelimitedFile.of_rows(_Renamed2, separator=",", header=Layouts((("day", "b"),)))

    assert by_name.read(b"B,Old Name\ny,x\n") == [_Renamed2("x", "y")]
    assert layouts.read(b"New Name,B\nx,y\n") == [_Renamed2("x", "y")]
    with pytest.raises(UnknownLayoutError, match="more than once"):
        by_name.read(b"Old Name,New Name,B\nx,x,y\n")


def test_a_by_name_header_reads_a_quoted_label_that_holds_the_separator() -> None:
    class Row(msgspec.Struct, frozen=True, gc=False):
        a: Annotated[str, Column("A, the first")]
        b: Annotated[str, Column("B")]

    rows = DelimitedFile.of_rows(Row, separator=",", header=ByName(quote='"'), quoted=True)

    assert rows.read(b'B,"A, the first"\ny,x\n') == [Row("x", "y")]
