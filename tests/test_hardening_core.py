"""``flatread.core`` and ``flatread.grids`` held to their promises.

Each refusal is a ``ReadError`` naming the label and the file's own row, a reader declared wrong a ``LayoutError`` when
it is built, and nothing else escapes a read.
"""

import datetime as dt
import inspect
import re
import struct
import types
import typing
from typing import Annotated, ClassVar, Literal

import msgspec
import polars as pl
import pytest

from flatread.archives import Transcoded, ZippedFile
from flatread.core import Carried, Column, File, TextFile, Thousands
from flatread.delimited import CsvFile, CsvText
from flatread.errors import EmptyBodyError, LayoutError, ReadError
from flatread.fields import Format, Joined, Label, Shape
from flatread.grids import FilledHeader, TitledGrid
from flatread.layout import LongFile
from flatread.sheets import BiffSheet, SheetText
from tests.support.xlsx import workbook


class _Count(msgspec.Struct, frozen=True):
    count: Annotated[int, Label("a")]


class _Panicking(File[_Count]):
    """A reader whose read panics in polars' Rust code: polars' CSV reader on an unclosed quote, read directly."""

    @typing.override
    def _read(self, body: bytes) -> pl.DataFrame:
        return pl.read_csv(body, schema={"a": pl.Int64}).rename({"a": "count"})


class PanicException(BaseException):
    """A stand-in for pyo3's, which no module exports: matched by its name."""


class _Raising(File[_Count]):
    raised: ClassVar[BaseException]

    @typing.override
    def _read(self, body: bytes) -> pl.DataFrame:
        del body
        raise self.raised


class TestThePanicNet:
    def test_a_rust_panic_is_a_read_error(self) -> None:
        with pytest.raises(ReadError):
            _Panicking(_Count).frame(b'a\n"')

    def test_a_panic_by_name_is_a_read_error_and_names_it(self) -> None:
        class Panics(_Raising):
            raised = PanicException("index out of bounds")

        with pytest.raises(ReadError, match=r"panic.*index out of bounds") as caught:
            Panics(_Count).rows(b"")
        assert isinstance(caught.value.__cause__, PanicException)

    def test_any_other_base_exception_passes(self) -> None:
        class Interrupted(_Raising):
            raised = KeyboardInterrupt()

        with pytest.raises(KeyboardInterrupt):
            Interrupted(_Count).frame(b"")


class _Dated(msgspec.Struct, frozen=True):
    day: Annotated[dt.date, Label("d"), Format("%m/%d/%y")]


class _Month(msgspec.Struct, frozen=True):
    day: Annotated[dt.date, Label("d"), Format("%b-%y")]


class _Stamped(msgspec.Struct, frozen=True):
    at: Annotated[dt.datetime, Label("d"), Format("%m/%d/%y %H:%M", time_zone="America/New_York")]


class TestATwoDigitYear:
    """``%y`` reads as ``strptime`` reads it, whether the format writes a day or a period."""

    @pytest.mark.parametrize("year", ["68", "69", "70"])
    def test_a_date_and_a_period_read_it_as_strptime_does(self, year: str) -> None:
        expected = dt.datetime.strptime(f"01/02/{year}", "%m/%d/%y").date()
        assert CsvFile(_Dated).rows(f"d\n01/02/{year}\n".encode()) == [_Dated(expected)]
        assert CsvFile(_Month).rows(f"d\nJan-{year}\n".encode()) == [_Month(expected.replace(day=1))]

    def test_a_time_in_a_zone_reads_it_so_too(self) -> None:
        (row,) = CsvFile(_Stamped).rows(b"d\n01/02/69 10:00\n")
        assert (row.at.year, row.at.hour, str(row.at.tzinfo)) == (1969, 10, "America/New_York")


class TestALiteral:
    def test_whole_numbers_read_as_ints(self) -> None:
        class Coded(msgspec.Struct, frozen=True):
            code: Annotated[Literal[1, 2] | None, Label("k")]

        assert CsvFile(Coded).frame(b"k\n1\n").schema == {"code": pl.Int64}
        assert CsvFile(Coded).rows(b"k\n1\n\n2\n") == [Coded(1), Coded(None), Coded(2)]
        with pytest.raises(ReadError, match=r"^the 'k' cell 3 is not one of its values \[1, 2\]: row 2$"):
            CsvFile(Coded).rows(b"k\n1\n3\n")

    def test_text_outside_its_values_is_refused_by_every_reader(self) -> None:
        class Lettered(msgspec.Struct, frozen=True):
            letter: Annotated[Literal["A", "B"], Label("k")]

        assert CsvFile(Lettered).rows(b"k\nA\nB\n") == [Lettered("A"), Lettered("B")]
        message = r"^the 'k' cell 'Z' is not one of its values \['A', 'B'\]: row {}$"
        with pytest.raises(ReadError, match=message.format(1)):
            CsvFile(Lettered).rows(b"k\nZ\n")
        with pytest.raises(ReadError, match=message.format(4)):
            CsvText(Lettered).rows(b"k\nA\n\nZ\n")
        with pytest.raises(ReadError, match=message.format(3)):
            SheetText(Lettered).rows(workbook({"S": [["k"], ["A"], ["Z"]]}))

    def test_an_enum_column_of_its_values_still_reads(self) -> None:
        class Lettered(msgspec.Struct, frozen=True):
            letter: Annotated[Literal["A", "B"], Label("k")]

        class Enumerated(File[Lettered]):
            @typing.override
            def _read(self, body: bytes) -> pl.DataFrame:
                return pl.DataFrame({"letter": body.decode().split()}, schema={"letter": pl.Enum(["A", "B", "C"])})

        assert Enumerated(Lettered).rows(b"A B") == [Lettered("A"), Lettered("B")]
        with pytest.raises(ReadError, match="the 'k' cell 'C' is not one of its values"):
            Enumerated(Lettered).rows(b"A C")

    @pytest.mark.parametrize(
        "annotation",
        [
            Annotated[Literal[True, False], Label("k")],
            Annotated[Literal[1, "a"], Label("k")],
            typing.cast("typing.Any", Annotated)[typing.cast("typing.Any", Literal)[1.5], Label("k")],  # no type form
        ],
        ids=["booleans", "mixed", "floats"],
    )
    def test_one_of_neither_whole_numbers_nor_text_is_refused_when_built(self, annotation: object) -> None:
        with pytest.raises(LayoutError, match=r"^the field 'x': the Literal .* is not of whole numbers alone"):
            _built(annotation)

    def test_a_union_of_two_types_is_refused_when_built(self) -> None:
        with pytest.raises(LayoutError, match="a column holds one type"):
            _built(Annotated[int | str, Label("x")])


def _built(annotation: object) -> CsvFile[msgspec.Struct]:
    return CsvFile(msgspec.defstruct("Row", [("x", annotation)]))


class TestADeclarationRefusedWhenBuilt:
    @pytest.mark.parametrize("pattern", [r"(?<!x)a+", r"a+\Z", r"[unclosed"], ids=["look-behind", "\\Z", "invalid"])
    def test_a_shape_polars_does_not_compile(self, pattern: str) -> None:
        with pytest.raises(LayoutError, match=f"Shape {re.escape(repr(pattern))} is no regex polars compiles"):
            _built(Annotated[str, Label("x"), Shape(pattern)])

    def test_a_zone_polars_does_not_know(self) -> None:
        with pytest.raises(LayoutError, match="names the zone 'Not/AZone', which is no zone polars knows"):
            _built(Annotated[dt.datetime, Label("x"), Format("%Y-%m-%d %H:%M", time_zone="Not/AZone")])

    @pytest.mark.parametrize(
        ("time_of_day", "written_as"),
        [(False, "%Y-%m-%d %H:%M"), (False, "%Y-%m-%d %I %p"), (False, "%Y-%m-%d %S"), (True, "%H:%M %Y")],
    )
    def test_a_format_writing_what_its_field_has_not(self, *, time_of_day: bool, written_as: str) -> None:
        annotation = (
            Annotated[dt.time, Label("x"), Format(written_as)]
            if time_of_day
            else Annotated[dt.date, Label("x"), Format(written_as)]
        )
        with pytest.raises(LayoutError, match=f"Format {re.escape(repr(written_as))} is no .* polars reads and writes"):
            _built(annotation)

    @pytest.mark.parametrize("written_as", ["%Y-%m-%d %H:%M%z", "%Y-%m-%d %H:%M %:z", "%Y-%m-%d %H:%M %Z"])
    @pytest.mark.parametrize("zone", [None, "UTC"])
    def test_a_format_writing_a_zone_in_the_cell(self, written_as: str, zone: str | None) -> None:
        with pytest.raises(LayoutError, match="writes a zone in its cells"):
            _built(Annotated[dt.datetime, Label("x"), Format(written_as, time_zone=zone)])

    def test_a_literal_percent_before_a_z_is_text(self) -> None:
        reader = _built(Annotated[dt.date, Label("x"), Format("%Y-%m-%d %%z")])
        assert reader.frame(b"x\n2026-01-02 %z\n")["x"].to_list() == [dt.date(2026, 1, 2)]

    def test_carried_needs_its_fields(self) -> None:
        class Row(msgspec.Struct, frozen=True):
            group: Annotated[str, Label("g")]
            code: Annotated[str, Label("c")] = "x"

        class Unset(Carried[Row], CsvFile[Row]):
            pass

        class Stray(Carried[Row], CsvFile[Row]):
            _carried = ("region",)

        class Given(Carried[Row], CsvFile[Row]):
            _carried = ("code",)
            _given = types.MappingProxyType({"code": "x"})

        with pytest.raises(LayoutError, match="names _carried, the fields it carries down"):
            Unset(Row)
        for reader in (Stray, Given):
            with pytest.raises(LayoutError, match=r"carries \['(region|code)'\] down, which are no fields of Row"):
                reader(Row)

    def test_a_titled_grid_needs_its_title(self) -> None:
        class Untitled(TitledGrid[_Count], CsvText[_Count]):
            pass

        with pytest.raises(LayoutError, match="names _title"):
            Untitled(_Count)

    def test_a_grid_header_row_counts_from_one(self) -> None:
        with pytest.raises(LayoutError, match="the header row 0 is no row of a grid"):
            CsvText(_Count, header_row=0)


class _Record(msgspec.Struct, frozen=True):
    group: Annotated[str, Label("g")]
    key: str
    value: int | None


class TestCarriedWithALongTable:
    def test_carried_after_long_file_fills_the_record(self) -> None:
        class Long(LongFile[_Record], Carried[_Record], CsvFile[_Record]):
            _keys = re.compile(r"VX\d")
            _carried = ("group",)

        assert Long(_Record).rows(b"g,VX1,VX2\na,1,2\n,3,\n") == [
            _Record("a", "VX1", 1),
            _Record("a", "VX2", 2),
            _Record("a", "VX1", 3),
            _Record("a", "VX2", None),
        ]

    def test_carried_before_long_file_is_refused_when_built_naming_the_order(self) -> None:
        class Before(Carried[_Record], LongFile[_Record], CsvFile[_Record]):
            _keys = re.compile(r"VX\d")
            _carried = ("group",)

        with pytest.raises(LayoutError, match="list LongFile first, then Carried, then the reader"):
            Before(_Record)


class _Pair(msgspec.Struct, frozen=True):
    name: Annotated[str, Label("Name")]
    count: Annotated[int, Label("N")]


class _Measured(msgspec.Struct, frozen=True):
    name: Annotated[str, Label("Name"), Shape(r"[a-z]+")]
    rate: Annotated[float, Label("Rate")]


class _GridThousands(Thousands[_Pair], CsvText[_Pair]):
    pass


class TestAGridNamesTheFilesRow:
    """Every error of a grid table names the grid's row, the file's own, blank rows counted, as a sheet's does."""

    @pytest.mark.parametrize(
        ("body", "header_row", "row"),
        [
            (b"Name,N\na,1\n\nb,x\n", 1, 4),
            (b"title\n\nName,N\na,1\nb,x\n", 3, 5),
            (b'Name,N\n"a\nb",1\nc,x\n', 1, 3),  # a record over two lines is one row
        ],
        ids=["a blank row", "a title over the header", "a quoted line break"],
    )
    def test_a_mistyped_cell(self, body: bytes, header_row: int, row: int) -> None:
        with pytest.raises(ReadError, match=f"^the 'N' cell 'x' is not a whole number: row {row}$"):
            CsvText(_Pair, header_row=header_row).rows(body)

    def test_a_sheets_text_table_names_the_sheet_row(self) -> None:
        body = workbook({"S": [["title"], [None], ["Name", "N"], ["a", 1], [None], ["b", "x"]]})
        with pytest.raises(ReadError, match=r"the 'N' cell 'x' is not a whole number: row 6$"):
            SheetText(_Pair, header_row=3).rows(body)

    def test_a_biff_sheet_names_the_sheet_row(self) -> None:
        def record(kind: int, row: int, column: int, value: bytes) -> bytes:
            body = struct.pack("<HHH", row, column, 0) + value
            return struct.pack("<HH", kind, len(body)) + body

        def text(row: int, column: int, label: str) -> bytes:
            return record(0x0204, row, column, struct.pack("<H", len(label)) + label.encode("latin-1"))

        # A BIFF3 worksheet: its BOF, a header, row 2, row 3 empty, row 4 holding text in the whole-number column.
        cells = [text(0, 0, "Name"), text(0, 1, "N"), text(1, 0, "a"), record(0x0203, 1, 1, struct.pack("<d", 1))]
        bof = struct.pack("<HH", 0x0209, 6) + struct.pack("<HHH", 0, 0x10, 0)
        body = bof + b"".join([*cells, text(3, 0, "b"), text(3, 1, "x")]) + struct.pack("<HH", 0x000A, 0)
        with pytest.raises(ReadError, match=r"the 'N' cell 'x' is not a whole number: row 4$"):
            BiffSheet(_Pair).rows(body)

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            (b"Name,Rate\na,1\n\nb,\n", r"^the \['Rate'\] columns have an empty cell.*: row 4$"),
            (b"Name,Rate\na,1\n\nb,nan\n", r"^the \['Rate'\] columns hold NaN or an infinity.*: row 4$"),
            (b"Name,Rate\na,1\n\nB,2\n", r"^the 'Name' cell 'B' is not of its shape '\[a-z\]\+': row 4$"),
        ],
        ids=["empty", "NaN", "shape"],
    )
    def test_the_frames_own_checks(self, body: bytes, message: str) -> None:
        with pytest.raises(ReadError, match=message):
            CsvText(_Measured).rows(body)

    def test_a_cleaned_cell(self) -> None:
        with pytest.raises(ReadError, match=r"'1,00' is not a number grouped in thousands: row 5$"):
            _GridThousands(_Pair, header_row=3).rows(b't\n\nName,N\nx,"1,000"\ny,"1,00"\n')

    def test_a_shared_reader_keeps_no_rows_of_a_read(self) -> None:
        reader = CsvText(_Pair)
        reader.rows(b"Name,N\n\n\na,1\n")
        assert reader._row_numbers is None  # the shared reader, not a read's copy
        with pytest.raises(ReadError, match=r"row 2$"):
            reader.rows(b"Name,N\nb,x\n")


class TestAnErrorNamesTheLabel:
    def test_an_empty_cell_and_a_nan_name_the_header_label(self) -> None:
        class Rated(msgspec.Struct, frozen=True):
            rate: Annotated[float, Label("Rate")]
            code: Annotated[str, Label("Series Code")]

        with pytest.raises(ReadError, match=r"^the \['Series Code'\] columns have an empty cell"):
            CsvFile(Rated).rows(b"Rate,Series Code\n1,\n")
        with pytest.raises(ReadError, match=r"^the \['Rate'\] columns hold NaN"):
            CsvFile(Rated).rows(b"Rate,Series Code\nNaN,A\n")

    def test_a_joined_cell_alone_names_both_labels(self) -> None:
        class Joining(msgspec.Struct, frozen=True):
            month: Annotated[dt.date, Label("Index Month"), Joined("Index Year"), Format("%B %Y")]

        with pytest.raises(
            ReadError, match=r"^row 1 holds one of the 'Index Month' and 'Index Year' cells, which write one value"
        ):
            CsvFile(Joining).rows(b"Index Month,Index Year\nJanuary,\n")


class _Filled(FilledHeader[_Pair], CsvText[_Pair]):
    pass


class TestAFilledHeaderOverNoTable:
    def test_a_note_is_no_rows_where_rows_are_not_required(self) -> None:
        class Optional(_Filled):
            _rows_required = False

        assert Optional(_Pair).rows(b"No ETF trades\n") == []
        assert Optional(_Pair).rows(b"A title\nName,N\na,1\n") == [_Pair("a", 1)]

    def test_a_note_is_an_empty_body_where_rows_are_required(self) -> None:
        with pytest.raises(EmptyBodyError, match="the grid holds no table"):
            _Filled(_Pair).rows(b"No ETF trades\n")


class TestTheFirstRowsNumber:
    @pytest.mark.parametrize("reader", [File, Carried, LongFile, ZippedFile, Transcoded, TextFile, CsvFile])
    def test_is_no_constructor_argument(self, reader: type) -> None:
        assert "row_one" not in inspect.signature(reader.__init__).parameters


class _Words(msgspec.Struct, frozen=True):
    name: Annotated[str | None, Label("Name")]
    count: Annotated[int | None, Label("N")]
    note: Annotated[str, Label("Note")]


class _Padded(TextFile[_Words]):
    """A text table of padded cells, a line a row and ``|`` between its cells, stripped by :meth:`_clean`."""

    _as_written = ("note",)

    @typing.override
    def _cells(self, body: bytes) -> pl.DataFrame:
        lines = body.decode().splitlines()
        return pl.DataFrame([line.split("|") for line in lines[1:]], schema=lines[0].split("|"), orient="row")

    @typing.override
    def _clean(self, cells: pl.Series, column: Column) -> pl.Series:
        return super()._clean(cells, column).str.strip_chars()


class TestATextReadersWordsForNone:
    def test_a_cleaned_cell_that_is_one_holds_none(self) -> None:
        reader = _Padded(_Words, null_values=["NA", "--"])
        assert reader.rows(b"Name|N|Note\n NA |--|NA\nx| 2 |y\n") == [_Words(None, None, "NA"), _Words("x", 2, "y")]

    def test_without_them_the_word_is_its_text(self) -> None:
        with pytest.raises(ReadError, match="the 'N' cell '--' is not a whole number: row 1"):
            _Padded(_Words).rows(b"Name|N|Note\nNA|--|x\n")

    def test_a_grid_takes_them_the_same_way(self) -> None:
        assert CsvText(_Words, null_values=["NA"]).rows(b"Name,N,Note\nNA,NA,x\n") == [_Words(None, None, "x")]
