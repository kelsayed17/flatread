"""The delimited, fixed-width, found, report, long and zipped-parts readers refuse what they cannot read exactly.

Each test is an adversary's probe turned around: a body or a reader that once read wrong, or failed late or as the
wrong error, and now reads right or raises ``ReadError`` (a body) or ``LayoutError`` (a reader declared wrong).
"""

import datetime as dt
import io
import re
import zipfile
from typing import Annotated

import msgspec
import pytest

from flatread.archives import ZippedFiles
from flatread.core import Thousands
from flatread.delimited import (
    CsvFile,
    HeadersCsv,
    NamedCsv,
    PaddedCsv,
    PreambleCsv,
    SectionedCsv,
    SpilledCsv,
    ThousandsCsv,
    TitledCsv,
    TrailerCsv,
)
from flatread.errors import LayoutError, RaggedRowError, ReadError
from flatread.fields import Format, Label, Span
from flatread.fixed import FixedWidthFile
from flatread.found import FoundCsv
from flatread.layout import LongFile
from flatread.reports import LongReport, ReportFile


class _Volume(msgspec.Struct, frozen=True, gc=False):
    symbol: Annotated[str, Label("Symbol")]
    volume: Annotated[int, Label("Volume")]


class _Text(msgspec.Struct, frozen=True, gc=False):
    a: Annotated[str | None, Label("A")]
    b: Annotated[str | None, Label("B")]


class _Day(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Format("%Y%m%d")]
    symbol: Annotated[str, Label("Symbol")]
    volume: Annotated[int | None, Label("Volume")]


class _Long(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Label("date"), Format("%Y-%m-%d")]
    written: str
    contract: str
    volume: int | None


class _Keyed(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Label("date"), Format("%Y-%m-%d")]
    contract: str
    volume: int | None


_LONG_ROWS = [_Long(dt.date(2024, 10, 14), "now", "VX1", 1644), _Long(dt.date(2024, 10, 14), "now", "VX2", 1976)]


def _zip(**members: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class TestAPanicOfPolars:
    def test_a_body_polars_panics_on_is_a_read_error(self) -> None:
        class _One(msgspec.Struct, frozen=True, gc=False):
            a: Annotated[int, Label("a")]

        for body in (b'a\n"', b'a\n1\n"'):
            with pytest.raises(ReadError):
                CsvFile(_One).rows(body)


class TestPiecesInAnyOrder:
    def test_a_header_piece_before_a_padded_csv_strips_its_cells(self) -> None:
        class _First(HeadersCsv[_Text], PaddedCsv[_Text]):
            pass

        class _Second(PaddedCsv[_Text], HeadersCsv[_Text]):
            pass

        for reader in (_First, _Second):
            assert reader(_Text).rows(b"A,B\nx  ,  y\n") == [_Text("x", "y")]

    def test_a_header_piece_before_titles_a_preamble_or_thousands_reads_the_table(self) -> None:
        class _Titled(HeadersCsv[_Volume], TitledCsv[_Volume]):
            _titles = (re.compile("Volumes"),)

        class _Preamble(NamedCsv[_Volume], PreambleCsv[_Volume]):
            pass

        class _Grouped(HeadersCsv[_Volume], ThousandsCsv[_Volume]):
            pass

        class _Padded(HeadersCsv[_Volume], PaddedCsv[_Volume]):
            pass

        assert _Titled(_Volume).rows(b"Volumes\nSymbol,Volume\nA,1\n") == [_Volume("A", 1)]
        assert _Preamble(_Volume).rows(b"note\nSymbol,Volume\nA,1\n") == [_Volume("A", 1)]
        assert _Grouped(_Volume).rows(b'Symbol,Volume\nA,"1,000"\n') == [_Volume("A", 1000)]
        assert _Padded(_Volume).rows(b"Symbol,Volume\nA , 1\n") == [_Volume("A", 1)]

    def test_a_piece_that_cleans_cells_on_a_typed_csv_is_refused(self) -> None:
        class _Typed(Thousands[_Volume], HeadersCsv[_Volume]):
            pass

        with pytest.raises(LayoutError, match="cleans its cells, which a CsvFile reads typed"):
            _Typed(_Volume)

    def test_a_long_table_listed_after_its_csv_reader_is_refused(self) -> None:
        class _After(TrailerCsv[_Long], LongFile[_Long]):
            _keys = re.compile(r"VX[0-9]")
            _trailer = re.compile(r"written (?P<written>\S+)")

        with pytest.raises(LayoutError, match=r"lists \['LongFile'\] after its CSV reader"):
            _After(_Long)


class TestStatedFields:
    def test_a_member_name_states_a_field_for_a_header_piece(self) -> None:
        class _Headers(ZippedFiles[_Day], HeadersCsv[_Day]):
            _member = re.compile(r"f(?P<day>\d{8})\.csv")
            _headers_lacking = ((), ("volume",))

        class _Named(ZippedFiles[_Day], NamedCsv[_Day]):
            _member = re.compile(r"f(?P<day>\d{8})\.csv")

        body = _zip(**{"f20240101.csv": b"Symbol,Volume\nA,1\n", "f20240102.csv": b"Symbol\nB\n"})
        rows = [_Day(dt.date(2024, 1, 1), "A", 1), _Day(dt.date(2024, 1, 2), "B", None)]
        assert _Headers(_Day).rows(body) == rows
        assert _Named(_Day).rows(body) == rows
        with pytest.raises(ReadError, match="no field of _Day labels"):
            _Named(_Day).rows(_zip(**{"f20240101.csv": b"day,Symbol,Volume\n1,A,1\n"}))

    def test_a_trailer_or_a_title_states_a_field_of_a_long_table(self) -> None:
        class _Trailed(LongFile[_Long], TrailerCsv[_Long]):
            _keys = re.compile(r"VX[0-9]")
            _trailer = re.compile(r"written (?P<written>\S+)")

        class _Titled(LongFile[_Long], TitledCsv[_Long]):
            _keys = re.compile(r"VX[0-9]")
            _titles = (re.compile(r"written (?P<written>\S+)"),)

        assert _Trailed(_Long).rows(b"date,VX1,VX2\n2024-10-14,1644,1976\nwritten now\n") == _LONG_ROWS
        assert _Titled(_Long).rows(b"written now\ndate,VX1,VX2\n2024-10-14,1644,1976\n") == _LONG_ROWS

    def test_a_member_name_states_a_field_of_a_fixed_width_file(self) -> None:
        class _Fixed(msgspec.Struct, frozen=True, gc=False):
            day: Annotated[dt.date, Format("%Y%m%d")]
            code: Annotated[str, Span(0, 3)]

        class _Members(ZippedFiles[_Fixed], FixedWidthFile[_Fixed]):
            _member = re.compile(r"f(?P<day>\d{8})\.txt")
            _header = False

        body = _zip(**{"f20240101.txt": b"ABC\n"})
        assert _Members(_Fixed).rows(body) == [_Fixed(dt.date(2024, 1, 1), "ABC")]


class TestAGroupNamingNoField:
    def test_a_trailer_a_title_or_a_member_name_whose_group_names_no_field_is_refused(self) -> None:
        class _Trailer(TrailerCsv[_Volume]):
            _trailer = re.compile(r"Rows: (?P<count>\d+) at (?P<stamped>\S+)")

        class _Title(TitledCsv[_Volume]):
            _titles = (re.compile(r"As of (?P<as_of>\S+)"),)

        class _Member(ZippedFiles[_Volume], CsvFile[_Volume]):
            _member = re.compile(r"f(?P<day>\d+)\.csv")

        for reader, group in ((_Trailer, "stamped"), (_Title, "as_of"), (_Member, "day")):
            with pytest.raises(LayoutError, match=rf"\['{group}'\]"):
                reader(_Volume)

    def test_a_count_group_names_the_rows(self) -> None:
        class _Counted(TrailerCsv[_Volume]):
            _trailer = re.compile(r"Rows: (?P<count>[0-9,]+)")

        assert _Counted(_Volume).rows(b"Symbol,Volume\nA,1\nRows: 1\n") == [_Volume("A", 1)]
        with pytest.raises(ReadError, match=r"the trailer's count '1,000' is not a number of rows"):
            _Counted(_Volume).rows(b"Symbol,Volume\nA,1\nRows: 1,000\n")

    def test_a_count_group_that_takes_no_part_is_a_read_error(self) -> None:
        class _Maybe(TrailerCsv[_Volume]):
            _trailer = re.compile(r"Rows(?:: (?P<count>\d+))?")

        class _Titled(TitledCsv[_Volume]):
            _titles = (re.compile(r"Issues(?:: (?P<count>\d+))?"),)

        with pytest.raises(ReadError, match=r"the trailer's count None"):
            _Maybe(_Volume).rows(b"Symbol,Volume\nA,1\nRows\n")
        with pytest.raises(ReadError, match=r"the title's count None"):
            _Titled(_Volume).rows(b"Issues\nSymbol,Volume\nA,1\n")


class TestASwitchLeftUnset:
    def test_each_required_switch_unset_is_a_layout_error_when_built(self) -> None:
        class _Trailer(TrailerCsv[_Volume]):
            pass

        class _Titles(TitledCsv[_Volume]):
            pass

        class _Sections(SectionedCsv[_Volume]):
            pass

        class _Spilled(SpilledCsv[_Volume]):
            _quote_char = None

        class _Members(ZippedFiles[_Day], CsvFile[_Day]):
            pass

        class _Keys(LongFile[_Keyed], CsvFile[_Keyed]):
            pass

        class _Report(ReportFile[_Volume]):
            pass

        for build, switch in (
            (lambda: _Trailer(_Volume), "_trailer"),
            (lambda: _Titles(_Volume), "_titles"),
            (lambda: _Sections(_Volume), "_section"),
            (lambda: _Spilled(_Volume), "_spilled"),
            (lambda: _Members(_Day), "_member"),
            (lambda: _Keys(_Keyed), "_keys"),
            (lambda: _Report(_Volume), "_line"),
        ):
            with pytest.raises(LayoutError, match=f"names no {switch}"):
                build()

    def test_a_found_csv_keyed_by_no_field_is_a_layout_error(self) -> None:
        with pytest.raises(LayoutError, match="the key 'zzz' is no field of _Volume"):
            FoundCsv(_Volume, key="zzz")


class TestALongReportsPatterns:
    class _Row(msgspec.Struct, frozen=True, gc=False):
        name: str
        year: int
        value: str

    def test_a_cells_flags_are_read_with_it(self) -> None:
        class _Cased(LongReport[TestALongReportsPatterns._Row]):
            _line = re.compile(r"(?P<name>[a-z]+)\s+(?P<value>.*)")
            _framing = (re.compile(r"Name\s+(?P<year>.*)"),)
            _cell = re.compile(r"x\d+", re.IGNORECASE)
            _key_cell = re.compile(r"\d+")

        reader = _Cased(self._Row)
        rows = [self._Row("abc", 2020, "X1"), self._Row("abc", 2021, "x2")]
        assert reader.rows(b"Name 2020 2021\nabc X1 x2\n") == rows

    def test_a_pattern_polars_cannot_read_is_a_layout_error_when_built(self) -> None:
        class _Behind(LongReport[TestALongReportsPatterns._Row]):
            _line = re.compile(r"(?P<name>[a-z]+)\s+(?P<value>.*)")
            _framing = (re.compile(r"Name\s+(?P<year>.*)"),)
            _cell = re.compile(r"(?<=\s)\d+")

        class _Ascii(_Behind):
            _cell = re.compile(r"\d+", re.ASCII)

        with pytest.raises(LayoutError, match="none polars' engine reads"):
            _Behind(self._Row)
        with pytest.raises(LayoutError, match="ASCII"):
            _Ascii(self._Row)


class TestAPaddedCsvsTokens:
    def test_a_padded_token_is_no_value(self) -> None:
        assert PaddedCsv(_Text, null_values=["NA"]).rows(b"A,B\n NA ,x\nNA,y\n") == [_Text(None, "x"), _Text(None, "y")]

    def test_a_quoted_token_is_no_value_in_a_typed_csv(self) -> None:
        assert CsvFile(_Text, null_values=["NA"]).rows(b'A,B\n"NA",x\n') == [_Text(None, "x")]


class TestASectionedCsv:
    def test_a_section_starts_only_where_a_line_begins_under_any_separator(self) -> None:
        class _Sections(SectionedCsv[_Volume]):
            _section = re.compile(r"Symbol")  # a str regex, matched as its UTF-8

        body = b"Symbol|Volume\nA Symbol|1\nSymbol|Volume\nB|2\n"
        assert _Sections(_Volume, separator="|", sections=2).rows(body) == [_Volume("A Symbol", 1)]
        assert _Sections(_Volume, separator="|", section=1, sections=2).rows(body) == [_Volume("B", 2)]
        with pytest.raises(ReadError, match="the body has 1 tables"):
            _Sections(_Volume, separator="|", sections=2).rows(b"Symbol|Volume\nA Symbol|1\n")


class TestAFoundCsvsRows:
    class _Found(msgspec.Struct, frozen=True, gc=False):
        name: Annotated[str, Label("Name")]
        value: Annotated[int | None, Label("Value")]
        note: Annotated[str | None, Label("Note")]

    def test_a_line_narrower_than_the_header_is_a_ragged_row(self) -> None:
        reader = FoundCsv(self._Found, key="name")
        for body in (b"Name,Value,Note\nA,1,x\nB,2", b"Name,Value,Note\nA,1,x\nB\n"):
            with pytest.raises(RaggedRowError, match="line 3 has"):
                reader.rows(body)
        assert reader.rows(b"Name,Value,Note\n\n---\nA,1,x\n") == [self._Found("A", 1, "x")]


class TestTextAroundTheTable:
    def test_a_title_or_a_trailer_that_is_not_utf8_is_a_read_error(self) -> None:
        class _Row(msgspec.Struct, frozen=True, gc=False):
            stamp: str
            a: Annotated[str, Label("A")]

        class _Trailer(TrailerCsv[_Row]):
            _trailer = re.compile(r"at (?P<stamp>.+)")

        class _Title(TitledCsv[_Row]):
            _titles = (re.compile(r"at (?P<stamp>.+)"),)

        with pytest.raises(ReadError, match="trailer, is not UTF-8"):
            _Trailer(_Row).rows(b"A\nx\nat caf\xe9\n")
        with pytest.raises(ReadError, match="is not UTF-8"):
            _Title(_Row).rows(b"at caf\xe9\nA\nx\n")


class TestANamedCsvsUnreadColumns:
    def test_an_unread_or_blank_column_is_dropped_and_a_blank_one_holds_nothing(self) -> None:
        class _One(msgspec.Struct, frozen=True, gc=False):
            a: Annotated[str, Label("a")]

        class _Notes(NamedCsv[_One]):
            _unread = ("notes",)
            _blank_columns = ("Filler",)

        assert _Notes(_One).rows(b"notes,a,Filler\nhi,x,\n") == [_One("x")]
        with pytest.raises(ReadError, match="the 'Filler' column holds 'y'"):
            _Notes(_One).rows(b"a,Filler\nx,y\n")
        with pytest.raises(ReadError, match="names a column twice"):
            _Notes(_One).rows(b"a,Filler,Filler\nx,,\n")


class TestARepeatedLabelsCell:
    def test_a_mistyped_cell_is_named_by_its_label_and_which_of_its_repeats(self) -> None:
        class _Fillers(msgspec.Struct, frozen=True, gc=False):
            first: Annotated[str, Label("Filler")]
            n: Annotated[int, Label("n")]
            second: Annotated[int, Label("Filler")]

        with pytest.raises(ReadError, match=r"the 'Filler' cell 'x' \(the header's 'Filler' 2 of 2\)"):
            CsvFile(_Fillers).rows(b"Filler,n,Filler\na,1,x\n")


class TestAFixedWidthFilesTokens:
    def test_a_token_once_stripped_is_no_value(self) -> None:
        class _Station(msgspec.Struct, frozen=True, gc=False):
            code: Annotated[str, Span(0, 3)]
            value: Annotated[float | None, Span(3, 8)]

        class _Stations(FixedWidthFile[_Station]):
            _header = False

        rows = _Stations(_Station, null_values=["-99"]).rows(b"ABC  1.5\nDEF  -99\n")
        assert rows == [_Station("ABC", 1.5), _Station("DEF", None)]
